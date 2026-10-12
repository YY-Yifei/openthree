"""内置 functions：种子工具（「二」）。

它们少而必需，且**每一个都在为自我演化铺路**：

    看世界      read_file / list_dir / write_file / run_shell
    改自己      write_function / reload_functions / delete_function
    改提示词    read_prompts / write_system_prompt / write_user_prompt
    管上下文    compact_context / note
    收尾        finish

真正的魔法在于：模型用 write_function 写出的新 py 文件，
reload_functions 之后就成了它手上的新工具 —— 能力从此自生长。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from .kernel import KERNEL

# ---------------------------------------------------------------- 看世界


def read_file(path: str, offset: int = 0, limit: int = 2000) -> str:
    """读取文本文件内容。

    Args:
        path: 文件路径（绝对路径，或相对当前工作目录）
        offset: 起始行号（从 0 开始）
        limit: 最多读取行数
    """
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path.cwd() / p
    if not p.exists():
        return f"[错误] 文件不存在: {p}"
    if p.is_dir():
        return f"[错误] 这是一个目录，请用 list_dir: {p}"
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as exc:  # noqa: BLE001
        return f"[错误] 读取失败: {type(exc).__name__}: {exc}"
    chunk = lines[offset: offset + limit] if limit else lines[offset:]
    body = "\n".join(f"{i + offset + 1}\t{ln}" for i, ln in enumerate(chunk))
    head = f"# {p} （共 {len(lines)} 行，显示 {offset + 1}-{offset + len(chunk)}）\n"
    return head + body


def list_dir(path: str = ".", show_hidden: bool = False, max_items: int = 200) -> str:
    """列出目录内容。

    Args:
        path: 目录路径，默认当前目录
        show_hidden: 是否显示以点开头的隐藏项
        max_items: 最多显示条目数
    """
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path.cwd() / p
    if not p.exists():
        return f"[错误] 目录不存在: {p}"
    if not p.is_dir():
        return f"[错误] 不是目录: {p}"
    rows: List[str] = []
    entries = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
    for e in entries:
        if not show_hidden and e.name.startswith("."):
            continue
        if e.is_dir():
            rows.append(f"  {e.name}/")
        else:
            try:
                size = e.stat().st_size
            except OSError:
                size = 0
            rows.append(f"  {e.name}  ({size}B)")
        if len(rows) >= max_items:
            rows.append("  ...")
            break
    return f"# {p}\n" + ("\n".join(rows) if rows else "  （空）")


def write_file(path: str, content: str) -> str:
    """写入文本文件（必要时创建父目录）。

    Args:
        path: 目标文件路径
        content: 要写入的完整内容
    """
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path.cwd() / p
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        return f"[错误] 写入失败: {type(exc).__name__}: {exc}"
    return f"[ok] 已写入 {p} （{len(content)} 字符）"


def run_shell(command: str, cwd: str = "", timeout: int = 60) -> str:
    """执行 shell 命令并返回输出。

    Args:
        command: 要执行的命令
        cwd: 工作目录（默认当前目录）
        timeout: 超时秒数
    """
    work = cwd or str(Path.cwd())
    try:
        proc = subprocess.run(
            command, shell=True, cwd=work, capture_output=True,
            text=True, timeout=timeout, encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return f"[错误] 命令超时（{timeout}s）: {command}"
    except Exception as exc:  # noqa: BLE001
        return f"[错误] 执行失败: {type(exc).__name__}: {exc}"
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    parts = [f"$ {command}", f"exit={proc.returncode}"]
    if out:
        parts.append("--- stdout ---\n" + out[:20000])
    if err:
        parts.append("--- stderr ---\n" + err[:8000])
    return "\n".join(parts)


# ---------------------------------------------------------------- 改自己


def write_function(name: str, code: str, overwrite: bool = True) -> str:
    """写出一个新的 function：把 python 代码存为 .openthree/functions/<name>.py。

    写完后必须调用 reload_functions，新工具才会生效。

    文件里所有公开函数（不以 _ 开头）都会注册为工具。规范：
      * 写类型注解 —— 它会变成 JSON Schema
      * 写 docstring（Args 段写参数说明）—— 它会变成工具描述
      * 首个参数若命名为 kernel，会由运行期注入，模型看不到
      * 可 `from openthree import tool, KERNEL`

    Args:
        name: 文件名（不含 .py），也是这次演化的名字
        code: 完整 python 源码
        overwrite: 已存在时是否覆盖
    """
    ws = KERNEL.workspace
    if ws is None:
        return "[错误] 内核未就绪"
    path = ws.function_path(name)
    if path.exists() and not overwrite:
        return f"[错误] {path.name} 已存在（overwrite=false）"
    existed = path.exists()
    path.write_text(code, encoding="utf-8")

    if KERNEL.recorder is not None:
        KERNEL.recorder.record(
            "evolution",
            summary=f"{'改写' if existed else '新造'} function: {path.name}",
            data={"action": "write_function", "name": name, "file": str(path),
                  "code": code, "overwrite": existed},
        )
    return f"[ok] 已写入 {path}\n下一步：调用 reload_functions 让它变成可用的工具。"


def reload_functions() -> str:
    """重新扫描 .openthree/functions/ 并注册全部工具。写完新 function 后调用。"""
    reg = KERNEL.registry
    if reg is None:
        return "[错误] 内核未就绪"
    before = set(reg.tools)
    reg.reload()
    after = set(reg.tools)
    added = sorted(after - before)
    removed = sorted(before - after)

    lines = [f"[ok] 重新加载完成，当前共 {len(reg.tools)} 个工具。"]
    if added:
        lines.append("新增：" + ", ".join(added))
    if removed:
        lines.append("移除：" + ", ".join(removed))
    if reg.load_errors:
        lines.append(f"⚠ {len(reg.load_errors)} 个文件有问题：")
        for e in reg.load_errors:
            lines.append(f"  - {Path(e.file).name}: {e.error}")
    else:
        lines.append("全部文件加载正常。")
    if KERNEL.recorder is not None:
        KERNEL.recorder.record(
            "evolution",
            summary=f"reload_functions: +{len(added)}/-{len(removed)}，共 {len(reg.tools)} 工具",
            data={"action": "reload_functions", "added": added, "removed": removed,
                  "errors": [{"file": e.file, "error": e.error} for e in reg.load_errors]},
        )
    return "\n".join(lines)


def delete_function(name: str) -> str:
    """删除一个 function 文件（会让同名工具消失，reload 后生效）。

    Args:
        name: 文件名（不含 .py）
    """
    ws = KERNEL.workspace
    if ws is None:
        return "[错误] 内核未就绪"
    path = ws.function_path(name)
    if not path.exists():
        return f"[错误] 不存在: {path.name}"
    path.unlink()
    if KERNEL.recorder is not None:
        KERNEL.recorder.record("evolution", summary=f"删除 function: {path.name}",
                               data={"action": "delete_function", "name": name})
    return f"[ok] 已删除 {path.name}，调用 reload_functions 生效。"


def list_functions() -> str:
    """列出当前所有可用工具及其来源文件。"""
    reg = KERNEL.registry
    if reg is None:
        return "[错误] 内核未就绪"
    rows = ["# 当前工具清单"]
    builtin = [t for t in reg.tools.values() if t.origin == "builtin"]
    user = [t for t in reg.tools.values() if t.origin == "user"]
    rows.append(f"\n## 内置（{len(builtin)}）")
    for t in sorted(builtin, key=lambda x: x.name):
        rows.append(f"- `{t.signature_hint()}`")
    rows.append(f"\n## 自造（{len(user)}）—— 你的演化成果")
    if user:
        for t in sorted(user, key=lambda x: x.name):
            rows.append(f"- `{t.signature_hint()}`  ← {Path(t.source_file or '').name}")
    else:
        rows.append("- （还没有。当内置工具不够用时，用 write_function 长出新的）")
    if reg.load_errors:
        rows.append(f"\n## 加载异常（{len(reg.load_errors)}）")
        for e in reg.load_errors:
            rows.append(f"- {Path(e.file).name}: {e.error}")
    return "\n".join(rows)


# ---------------------------------------------------------------- 改提示词


def read_prompts() -> str:
    """读取当前的系统提示词与用户提示词。"""
    ws = KERNEL.workspace
    if ws is None:
        return "[错误] 内核未就绪"
    return (f"# 系统提示词 ({ws.system_prompt_file})\n\n{ws.read_system_prompt()}\n\n"
            f"# 用户提示词 ({ws.user_prompt_file})\n\n{ws.read_user_prompt()}")


def write_system_prompt(content: str) -> str:
    """改写系统提示词 —— 你对「我是谁、我该怎么做」的定义，下一轮即刻生效。

    Args:
        content: 完整的系统提示词 markdown
    """
    ws = KERNEL.workspace
    if ws is None:
        return "[错误] 内核未就绪"
    ws.write_system_prompt(content)
    if KERNEL.recorder is not None:
        KERNEL.recorder.record("evolution", summary="改写系统提示词",
                               data={"action": "write_system_prompt", "content": content})
    return f"[ok] 系统提示词已更新（{len(content)} 字符），下一轮生效。"


def write_user_prompt(content: str) -> str:
    """改写当前任务（user-prompts.md）—— 把目标、计划、待办写在这里指引自己。

    Args:
        content: 完整的任务描述 markdown
    """
    ws = KERNEL.workspace
    if ws is None:
        return "[错误] 内核未就绪"
    ws.write_user_prompt(content)
    if KERNEL.recorder is not None:
        KERNEL.recorder.record("evolution", summary="改写用户提示词（任务）",
                               data={"action": "write_user_prompt", "content": content})
    return f"[ok] 任务已更新（{len(content)} 字符），下一轮生效。"


# ---------------------------------------------------------------- 管上下文


def note(thought: str, tags: str = "") -> str:
    """把你的思考显式写进工作记录（会出现在 three-report.html 里）。

    Args:
        thought: 你的推理、判断或计划
        tags: 逗号分隔的标签，便于检索
    """
    if KERNEL.recorder is not None:
        KERNEL.recorder.record("note", summary=thought[:200],
                               data={"thought": thought,
                                     "tags": [t.strip() for t in tags.split(",") if t.strip()]})
    return "[ok] 已记录。"


def remember(content: str, tags: str = "") -> str:
    """写入一条长期记忆到 .openthree/memory/，供以后检索。

    Args:
        content: 要记住的内容
        tags: 逗号分隔的标签
    """
    ws = KERNEL.workspace
    if ws is None:
        return "[错误] 内核未就绪"
    from datetime import datetime
    ws.memory_dir.mkdir(parents=True, exist_ok=True)
    entry = {"ts": datetime.now().isoformat(timespec="seconds"), "content": content,
             "tags": [t.strip() for t in tags.split(",") if t.strip()]}
    path = ws.memory_dir / "memory.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    if KERNEL.recorder is not None:
        KERNEL.recorder.record("evolution", summary=f"写入记忆：{content[:60]}",
                               data={"action": "remember", "entry": entry})
    return "[ok] 已记住。"


def compact_context(start: int, end: int, summary: str) -> str:
    """折叠一段对话历史为一条摘要，释放上下文空间。

    Args:
        start: 起始步骤号（含）
        end: 结束步骤号（含）
        summary: 摘要，必须保留关键事实、数值、结论与未完成任务
    """
    if KERNEL.agent is not None:
        KERNEL.agent.compact(start, end, summary)
    if KERNEL.recorder is not None:
        KERNEL.recorder.record("evolution",
                               summary=f"压缩上下文 step {start}-{end}",
                               data={"action": "compact_context", "start": start,
                                     "end": end, "summary": summary})
    return f"[ok] 已折叠 step {start}-{end}，下一轮生效。"


# ---------------------------------------------------------------- 收尾


def finish(answer: str) -> str:
    """任务完成，给出最终结论并结束循环。

    Args:
        answer: 面向用户的最终答复
    """
    if KERNEL.agent is not None:
        KERNEL.agent.finish(answer)
    if KERNEL.recorder is not None:
        KERNEL.recorder.record("finish", summary=answer[:200], data={"answer": answer})
    return "[ok] 已提交最终答复。"


def get_time() -> str:
    """获取当前时间。"""
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


BUILTIN_FUNCTIONS: Dict[str, Any] = {
    "read_file": read_file,
    "write_file": write_file,
    "list_dir": list_dir,
    "run_shell": run_shell,
    "write_function": write_function,
    "reload_functions": reload_functions,
    "delete_function": delete_function,
    "list_functions": list_functions,
    "read_prompts": read_prompts,
    "write_system_prompt": write_system_prompt,
    "write_user_prompt": write_user_prompt,
    "note": note,
    "remember": remember,
    "compact_context": compact_context,
    "finish": finish,
    "get_time": get_time,
}
