"""内置 functions：种子工具（「二」）。

它们少而必需，且**每一个都在为自我演化铺路**：

    看世界      read_file / list_dir / search_files / view_image / write_file / run_shell
    改自己      write_function / reload_functions / delete_function
    改提示词    read_prompts / write_system_prompt / write_user_prompt
    长任务      read_plan / write_plan（待办清单）
                list_prompt_versions / restore_system_prompt（提示词回滚）
    管记忆      remember（写下）/ recall_memory（读回）
    管上下文    compact_context / note
    收尾        finish

真正的魔法在于：模型用 write_function 写出的新 py 文件，
reload_functions 之后就成了它手上的新工具 —— 能力从此自生长。

多模态：view_image 返回 ImageResult，Agent 会把图片注入上下文，
模型因此能「看见」图；自造工具只要返回 `openthree.ImageResult`（或
`images=[...]`）也能回传图片。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from .kernel import KERNEL
from .multimodal import ImageResult

# ---------------------------------------------------------------- 看世界


def view_image(path: str, detail: str = "auto") -> ImageResult:
    """查看一张图片（多模态）。返回的图片会被放进上下文，你就能「看见」它。

    本地路径（相对/绝对）或 http(s) 链接均可。图片进入上下文后即可描述、
    识别、做 OCR 判断等。一次只看一张，避免上下文被图片塞满。

    Args:
        path: 图片路径或 http(s) 链接
        detail: 传给模型的清晰度 auto / low / high；low 更省 token
    """
    src = (path or "").strip()
    if not src:
        return ImageResult(text="[错误] path 为空")
    return ImageResult(text=f"已加载图片：{src}", images=[src],
                       detail=detail or "auto")


def read_file(path: str, offset: int = 0, limit: int = 2000) -> str:
    """读取文本文件内容。

    Args:
        path: 文件路径（绝对路径，或相对当前工作目录）
        offset: 起始行号（从 0 开始）
        limit: 最多读取行数；<= 0 表示读到文件末尾
    """
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path.cwd() / p
    if not p.exists():
        return f"[错误] 文件不存在: {p}"
    if p.is_dir():
        return f"[错误] 这是一个目录，请用 list_dir: {p}"
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        offset = 0
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 2000
    offset = max(0, offset)      # 负数行号无意义，夹到 0
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as exc:  # noqa: BLE001
        return f"[错误] 读取失败: {type(exc).__name__}: {exc}"
    chunk = lines[offset: offset + limit] if limit > 0 else lines[offset:]
    body = "\n".join(f"{i + offset + 1}\t{ln}" for i, ln in enumerate(chunk))
    head = f"# {p} （共 {len(lines)} 行，显示 {offset + 1}-{offset + len(chunk)}）\n"
    return head + body


def list_dir(path: str = ".", show_hidden: bool = False, max_items: int = 200) -> str:
    """列出目录内容。

    Args:
        path: 目录路径，默认当前目录
        show_hidden: 是否显示以点开头的隐藏项
        max_items: 最多显示条目数（至少 1）
    """
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path.cwd() / p
    if not p.exists():
        return f"[错误] 目录不存在: {p}"
    if not p.is_dir():
        return f"[错误] 不是目录: {p}"
    try:
        max_items = int(max_items)
    except (TypeError, ValueError):
        max_items = 200
    max_items = max(1, min(max_items, 100_000))
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


# 检索时跳过的目录：版本库 / 缓存 / 依赖 —— 真正的搜索几乎从不关心它们。
_SEARCH_SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".idea", ".vscode",
    ".cache", ".next", "dist", "build", ".eggs",
})
# 以点开头、但**值得搜**的目录：agent 自己的 .openthree（functions/prompts/memory
# 正是最该被检索的地方），以及 CI / 编辑器配置。
_SEARCH_ALLOW_DOT = frozenset({
    ".openthree", ".github", ".gitlab", ".circleci", ".claude", ".cursor",
})
_SEARCH_MAX_FILE_BYTES = 2_000_000   # 单文件超过此大小不逐行搜（防止拖死循环）
_SEARCH_MAX_FILES = 20_000           # 单次检索最多扫描这么多文件
_SEARCH_MAX_LINE_CHARS = 400         # 命中行过长时只保留前 400 字符
# .openthree 下属于「运行时churn」的子目录：日志与资产每次运行都变、体量大，
# 纯属噪声。functions / prompts / memory 不在此列 —— 它们才是该被搜的。
_SEARCH_SKIP_WS_SUBDIRS = frozenset({"logs", "assets"})


def _should_prune_dir(parent: Path, name: str) -> bool:
    """给定「父目录 + 子目录名」，判断检索时是否该跳过它。"""
    if name in _SEARCH_SKIP_DIRS:
        return True
    if parent.name == ".openthree" and name in _SEARCH_SKIP_WS_SUBDIRS:
        return True
    return name.startswith(".") and name not in _SEARCH_ALLOW_DOT


def _iter_search_files(root: Path, glob_pat: str):
    """遍历待检索的文件；跳过噪声目录、按 glob 过滤、带上限。"""
    import fnmatch
    if root.is_file():
        yield root
        return
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        dirnames[:] = sorted(d for d in dirnames if not _should_prune_dir(here, d))
        for fname in sorted(filenames):
            if fname.startswith("."):
                continue
            if glob_pat and not fnmatch.fnmatch(fname, glob_pat):
                continue
            scanned += 1
            if scanned > _SEARCH_MAX_FILES:
                return
            yield here / fname


def _looks_binary(path: Path) -> bool:
    """按前 4KB 里有没有 NUL 字节判断二进制（简单、够用）。"""
    try:
        with path.open("rb") as f:
            chunk = f.read(4096)
    except Exception:  # noqa: BLE001
        return True
    return b"\x00" in chunk


def search_files(pattern: str, path: str = ".", glob: str = "",
                 regex: bool = False, ignore_case: bool = False,
                 max_results: int = 100, context: int = 0) -> str:
    """在文件里检索文本（grep 式），返回「文件:行号: 内容」清单 —— 这是你的感知带宽。

    比「读整份文件」精准得多：先 search_files 定位，再 read_file(offset=行号) 精读。
    `glob` 可按文件名过滤（如 `*.py`）；`regex=true` 时 pattern 按正则解释。
    自动跳过 .git / node_modules / __pycache__ / logs 等噪声，跳过二进制与超大文件；
    `.openthree` 下的 functions / prompts / memory 仍会被搜到（那正是最该翻的地方）。

    Args:
        pattern: 要找的文本（regex=true 时为正则）
        path: 检索起点：文件或目录，默认当前目录
        glob: 只搜匹配此通配符的文件名，如 *.py / test_*.py
        regex: pattern 是否按正则解释，默认 false（纯文本子串）
        ignore_case: 是否忽略大小写，默认 false
        max_results: 最多返回多少条命中，默认 100
        context: 每条命中额外显示前后各几行，默认 0
    """
    src = (path or ".").strip() or "."
    root = Path(src).expanduser()
    if not root.is_absolute():
        root = Path.cwd() / root
    if not root.exists():
        return f"[错误] 路径不存在: {root}"
    if not (pattern if isinstance(pattern, str) else ""):
        return "[错误] pattern 为空，请给出要检索的文本"
    pattern = str(pattern)

    max_results = max(1, min(int(max_results or 100), 1000))
    context = max(0, min(int(context or 0), 5))

    if regex:
        try:
            rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        except re.error as exc:
            return f"[错误] 正则非法: {exc}"
        def hit(line: str) -> bool:
            return rx.search(line) is not None
    else:
        needle = pattern.lower() if ignore_case else pattern
        def hit(line: str) -> bool:
            return needle in (line.lower() if ignore_case else line)

    matches: List[str] = []
    hits = 0
    scanned = 0
    truncated = False
    for fp in _iter_search_files(root, (glob or "").strip()):
        try:
            if fp.stat().st_size > _SEARCH_MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        if _looks_binary(fp):
            continue
        scanned += 1
        try:
            lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:  # noqa: BLE001
            continue
        for i, line in enumerate(lines):
            if not hit(line):
                continue
            hits += 1
            if len(matches) >= max_results:
                truncated = True
                break
            matches.append(f"{fp}:{i + 1}: {line.strip()[:_SEARCH_MAX_LINE_CHARS]}")
            for j in range(max(0, i - context), min(len(lines), i + context + 1)):
                if j == i:
                    continue
                matches.append(f"{fp}:{j + 1}- {lines[j].strip()[:_SEARCH_MAX_LINE_CHARS]}")
        if truncated:
            break

    head = (f"# 检索 {pattern!r} 于 {root}（命中 {hits} 处"
            f"{'+' if truncated else ''} / 扫描 {scanned} 个文件）")
    if not matches:
        return head + "\n（无命中）"
    body = "\n".join(matches)
    if truncated:
        body += f"\n... [已达 max_results={max_results}，仅显示前 {len(matches)} 条；请缩小范围或提高 max_results]"
    return head + "\n" + body


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


_KILL_GRACE = 5.0           # 超时后强杀进程树，再给管道收尾的宽限秒数
_SHELL_TIMEOUT_MAX = 3600   # 单条命令超时上限，防止模型传入离谱数值


def _popen_kwargs(work: str) -> Dict[str, Any]:
    """构造一个「能整树杀死」的 Popen 参数。

    两个关键点，缺一都会导致挂死：
      * stdin 接 DEVNULL —— 交互式命令（等输入）立刻拿到 EOF 退出，
        而不是继承终端读走用户输入、把自己的 stdin 挂死在这里。
      * Windows 起新进程组、POSIX 起新会话 —— 之后才能把整棵进程树
        （cmd.exe 及其孙进程）一次性清理干净。
    """
    kwargs: Dict[str, Any] = {
        "shell": True,
        "cwd": work,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:  # pragma: no cover - Windows 环境下不覆盖
        kwargs["start_new_session"] = True
    return kwargs


def _kill_tree(proc: Any) -> str:
    """强杀进程及其全部后代，返回所用手段（便于排障）。"""
    if proc.poll() is not None:
        return "already-exited"
    if os.name == "nt":
        try:
            r = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=10,
            )
            if r.returncode == 0 or proc.poll() is not None:
                return "taskkill"
        except Exception:  # noqa: BLE001
            pass
    else:  # pragma: no cover - Windows 环境下不覆盖
        try:
            import signal
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return "killpg"
        except Exception:  # noqa: BLE001
            pass
    try:
        proc.kill()
        return "kill"
    except Exception as exc:  # noqa: BLE001
        return f"kill-failed({type(exc).__name__})"


def run_shell(command: str, cwd: str = "", timeout: int = 60) -> str:
    """执行 shell 命令并返回输出。

    超时后会**连同子孙进程一起强杀**，所以绝不会因为某个后台子进程仍攥着
    输出管道而永久挂住（旧实现 subprocess.run 的缺陷）。stdin 不继承终端，
    交互式命令会立刻得到 EOF 而退出。

    Args:
        command: 要执行的命令
        cwd: 工作目录（默认当前目录）
        timeout: 超时秒数（1~3600，越界会被截到边界）
    """
    work = cwd or str(Path.cwd())
    try:
        limit = int(timeout or 60)
    except (TypeError, ValueError):
        limit = 60
    limit = max(1, min(limit, _SHELL_TIMEOUT_MAX))

    try:
        proc = subprocess.Popen(command, **_popen_kwargs(work))
    except Exception as exc:  # noqa: BLE001
        return f"[错误] 执行失败: {type(exc).__name__}: {exc}"

    timed_out = False
    killed = ""
    stream_cut = False
    try:
        out_b, err_b = proc.communicate(timeout=limit)
    except subprocess.TimeoutExpired:
        timed_out = True
        killed = _kill_tree(proc)
        try:
            out_b, err_b = proc.communicate(timeout=_KILL_GRACE)
        except subprocess.TimeoutExpired:
            # 极端兜底：仍有进程攥着管道。放弃读取、立刻返回，绝不无限等待。
            stream_cut = True
            out_b, err_b = b"", b""
            for stream in (proc.stdout, proc.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except Exception:  # noqa: BLE001
                    pass
    except Exception as exc:  # noqa: BLE001
        _kill_tree(proc)
        return f"[错误] 执行失败: {type(exc).__name__}: {exc}"

    code = proc.returncode if proc.returncode is not None else proc.poll()
    out = _decode(out_b).strip()
    err = _decode(err_b).strip()
    parts = [f"$ {command}", f"exit={code}"]
    if timed_out:
        tail = f"[错误] 命令超时（{limit}s），已强杀进程树（{killed}）"
        if stream_cut:
            tail += "；输出管道未能收尾，已放弃读取"
        parts.append(tail)
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

    syntax_error = _syntax_error(code)
    if syntax_error:
        if KERNEL.recorder is not None:
            KERNEL.recorder.record(
                "error", summary=f"写 function 语法错误: {path.name}",
                data={"action": "write_function", "name": name, "error": syntax_error},
            )
        return (f"[错误] {path} 已写入，但语法检查未通过（尚未生效）。"
                f"请修正后重新 write_function：\n{syntax_error}")

    if KERNEL.recorder is not None:
        KERNEL.recorder.record(
            "evolution",
            summary=f"{'改写' if existed else '新造'} function: {path.name}",
            data={"action": "write_function", "name": name, "file": str(path),
                  "code": code, "overwrite": existed},
        )
    warning = _public_function_warning(code)
    if warning:
        return (f"[ok] 已写入 {path}\n⚠ {warning}"
                f"\n下一步：调用 reload_functions 让它变成可用的工具。")
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

    覆写前会自动把旧稿存进 `prompts/versions/`，因此改坏了可以用
    `restore_system_prompt` 回退，不必怕失手。

    Args:
        content: 完整的系统提示词 markdown
    """
    ws = KERNEL.workspace
    if ws is None:
        return "[错误] 内核未就绪"
    snapshot = ws.snapshot_system_prompt(reason="write_system_prompt")
    ws.write_system_prompt(content, snapshot=False)
    if KERNEL.recorder is not None:
        KERNEL.recorder.record("evolution", summary="改写系统提示词",
                               data={"action": "write_system_prompt", "content": content,
                                     "snapshot": snapshot.name if snapshot else ""})
    saved = f"，旧稿存档 {snapshot.name}" if snapshot else ""
    return f"[ok] 系统提示词已更新（{len(content)} 字符{saved}），下一轮生效。"


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


def read_plan() -> str:
    """读取你自己的计划（`.openthree/prompts/plan.md`）—— 长任务的待办清单。"""
    ws = KERNEL.workspace
    if ws is None:
        return "[错误] 内核未就绪"
    text = ws.read_plan().strip()
    items = ws.plan_open_items()
    if not text:
        return "（还没有计划。用 write_plan 写下待办。）"
    return f"# 计划（{ws.plan_file}）\n\n{text}\n\n未完成条目：{items}"


def write_plan(content: str) -> str:
    """写下/更新你的计划——长任务的推进清单，它会每轮自动出现在你眼前。

    用 markdown 勾选框逐条推进：`- [ ] 未完成`、`- [x] 已完成`。
    长任务请把「已完成什么、还剩什么」写在这里，别只留在上下文里
    （上下文会被折叠，这个文件不会）。收尾时运行期就靠未勾选项判断是否续跑。

    Args:
        content: 完整的计划 markdown（含勾选框）
    """
    ws = KERNEL.workspace
    if ws is None:
        return "[错误] 内核未就绪"
    ws.write_plan(content)
    open_items = ws.plan_open_items()
    if KERNEL.recorder is not None:
        KERNEL.recorder.record("evolution", summary=f"更新计划（未完成 {open_items} 项）",
                               data={"action": "write_plan", "content": content,
                                     "open_items": open_items})
    return f"[ok] 计划已更新（{len(content)} 字符，未完成 {open_items} 项），下一轮生效。"


def list_prompt_versions() -> str:
    """列出系统提示词的历史版本——写坏了可以回退（自省的前提是可后悔）。"""
    ws = KERNEL.workspace
    if ws is None:
        return "[错误] 内核未就绪"
    versions = ws.list_prompt_versions()
    if not versions:
        return "（还没有历史版本。每次 write_system_prompt 都会自动存一版。）"
    rows = [f"# 系统提示词历史版本（{len(versions)}，新 → 旧）", ""]
    for i, p in enumerate(versions, 1):
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        rows.append(f"- {i}. `{p.name}`（{size}B）")
    rows.append("\n用 restore_system_prompt(<序号或文件名>) 回退到某一版。")
    return "\n".join(rows)


def restore_system_prompt(version: str) -> str:
    """把系统提示词回退到某个历史版本（恢复前也会先把现稿存一版）。

    Args:
        version: 版本序号（1 = 最新）或文件名，见 list_prompt_versions
    """
    ws = KERNEL.workspace
    if ws is None:
        return "[错误] 内核未就绪"
    name = ws.restore_system_prompt(version)
    if name is None:
        return (f"[错误] 找不到版本：{version}。"
                "用 list_prompt_versions 看有哪些。")
    if KERNEL.recorder is not None:
        KERNEL.recorder.record("evolution", summary=f"回退系统提示词到 {name}",
                               data={"action": "restore_system_prompt", "version": version})
    return f"[ok] 系统提示词已回退到 {name}，下一轮生效。"


# ---------------------------------------------------------------- 管上下文


def _split_tags(tags) -> list:
    """把标签归一成字符串列表。容忍 None / list / "a, b" 三种输入。"""
    if tags is None:
        return []
    if isinstance(tags, (list, tuple, set)):
        items = list(tags)
    else:
        items = str(tags).replace("，", ",").split(",")
    return [str(t).strip() for t in items if str(t).strip()]


def note(thought: str, tags: str = "") -> str:
    """把你的思考显式写进工作记录（会出现在 three-report.html 里）。

    Args:
        thought: 你的推理、判断或计划
        tags: 逗号分隔的标签，便于检索
    """
    if KERNEL.recorder is not None:
        KERNEL.recorder.record("note", summary=thought[:200],
                               data={"thought": thought,
                                     "tags": _split_tags(tags)})
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
             "tags": _split_tags(tags)}
    path = ws.memory_file
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    if KERNEL.recorder is not None:
        KERNEL.recorder.record("evolution", summary=f"写入记忆：{content[:60]}",
                               data={"action": "remember", "entry": entry})
    return "[ok] 已记住。"


def recall_memory(query: str = "", limit: int = 20) -> str:
    """检索你自己的长期记忆（`.openthree/memory/memory.jsonl`）—— 把写下的经验读回来。

    `remember` 只负责写下；这条才是读回。跨会话、跨上下文折叠都还在。
    不传 query 就是「最近 limit 条」；传了就在 content 与 tags 里做不区分
    大小写的子串匹配。想找得更细（跨文件）可用 search_files 直接搜 memory 目录。

    Args:
        query: 关键词；空 = 不过滤，返回最近的
        limit: 最多返回多少条（从最近的往回取），默认 20
    """
    ws = KERNEL.workspace
    if ws is None:
        return "[错误] 内核未就绪"
    try:
        n = int(limit) if limit else 20
    except (TypeError, ValueError):
        n = 20
    n = max(1, min(n, 200))
    total = ws.count_memory()
    entries = ws.read_memory(limit=n, query=query)
    label = f"（最近 {len(entries)}/{total} 条）" if not query \
        else f"（匹配 {len(entries)}/{total} 条）"
    if not entries:
        return (f"# 长期记忆 {label}\n（空 —— 还没有记忆。"
                + ("没匹配到，换个关键词，或不传 query 看最近几条。）" if query
                   else "值得记住的结论可用 remember 写下。）"))
    lines = [f"# 长期记忆 {label}"]
    for e in entries:
        tags = ("  [" + ", ".join(e["tags"]) + "]") if e.get("tags") else ""
        ts = f"{e['ts']} " if e.get("ts") else ""
        lines.append(f"- {ts}{e['content']}{tags}")
    return "\n".join(lines)


def compact_context(start: int, end: int, summary: str) -> str:
    """折叠一段对话历史为一条摘要，释放上下文空间。

    Args:
        start: 起始步骤号（含）
        end: 结束步骤号（含）
        summary: 摘要，必须保留关键事实、数值、结论与未完成任务
    """
    ok, msg = False, "[错误] 内核未就绪"
    if KERNEL.agent is not None:
        ok, msg = KERNEL.agent.compact(start, end, summary)
    if KERNEL.recorder is not None:
        KERNEL.recorder.record(
            "evolution" if ok else "error",
            summary=(f"压缩上下文 step {start}-{end}" if ok else msg[:200]),
            data={"action": "compact_context", "start": start, "end": end,
                  "summary": summary, "ok": ok},
        )
    return msg


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


# ---------------------------------------------------------------- 内部辅助


def _candidate_encodings() -> List[str]:
    """依次尝试的解码方案：utf-8 优先，再用系统首选（Windows 下多为 GBK）。"""
    encs = ["utf-8"]
    try:
        import locale
        pref = locale.getpreferredencoding(False)
        if pref and pref.lower() not in ("utf-8", "utf8"):
            encs.append(pref)
    except Exception:  # noqa: BLE001
        pass
    if sys.platform == "win32":
        for e in ("gbk", "cp936"):
            if e not in encs:
                encs.append(e)
    encs.append("latin-1")   # 最后兜底，不会报错
    return encs


def _decode(data: Any) -> str:
    """将子进程输出（bytes）按候选编码逐级解码，bad case 不会丢字。"""
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    for enc in _candidate_encodings():
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _syntax_error(code: str) -> str:
    """返回语法错误描述；合法则返回空串。"""
    import ast
    try:
        ast.parse(code)
    except SyntaxError as exc:
        return f"{type(exc).__name__}: {exc.msg} (line {exc.lineno})"
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"
    return ""


def _public_function_warning(code: str) -> str:
    """语法合法但**没有一个公开函数**时给出提醒 —— 否则这是一次静默的失败。

    注册器只收本文件定义、且不以 `_` 开头的函数（类与 import 进来的都不算）。
    若不提醒，模型会以为 reload 后会多出工具，结果一个也没有。
    """
    import ast
    try:
        tree = ast.parse(code)
    except Exception:  # noqa: BLE001
        return ""            # 语法问题由 _syntax_error 负责
    public = [n.name for n in tree.body
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and not n.name.startswith("_")]
    if public:
        return ""
    names = [n.name for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    hint = ("（你定义了 " + ", ".join(f"`{n}`" for n in names)
            + "，但都以下划线开头，会被当作私有函数忽略）") if names else ""
    return ("这个文件里没有可注册的公开函数，reload 后不会出现任何新工具。"
            "请把要暴露给模型的函数改成不以 `_` 开头的顶层函数。" + hint)


BUILTIN_FUNCTIONS: Dict[str, Any] = {
    "view_image": view_image,
    "read_file": read_file,
    "write_file": write_file,
    "list_dir": list_dir,
    "search_files": search_files,
    "run_shell": run_shell,
    "write_function": write_function,
    "reload_functions": reload_functions,
    "delete_function": delete_function,
    "list_functions": list_functions,
    "read_prompts": read_prompts,
    "write_system_prompt": write_system_prompt,
    "write_user_prompt": write_user_prompt,
    "read_plan": read_plan,
    "write_plan": write_plan,
    "list_prompt_versions": list_prompt_versions,
    "restore_system_prompt": restore_system_prompt,
    "note": note,
    "remember": remember,
    "recall_memory": recall_memory,
    "compact_context": compact_context,
    "finish": finish,
    "get_time": get_time,
}
