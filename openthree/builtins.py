"""内置 functions：种子工具（「二」）。

它们少而必需，且**每一个都在为自我演化铺路**：

    看世界      read_file / list_dir / search_files / code_outline
                view_image / write_file / run_shell
    改自己      write_function（写入即静态审计）
                verify_tool（子进程验收：先证明它能跑，再 reload）
                reload_functions / delete_function
    改提示词    read_prompts / write_system_prompt / write_user_prompt
    自省        list_prompt_versions / read_prompt_version / restore_system_prompt
    长任务      read_plan / write_plan（待办清单）
    管记忆      remember（写下）/ recall_memory（读回）
    管上下文    context_usage（用量读数）/ context_budget（改预算与自动折叠）
                compact_context（折叠/丢弃历史）
    收尾        finish

真正的魔法在于：模型用 write_function 写出的新 py 文件，
reload_functions 之后就成了它手上的新工具 —— 能力从此自生长。

「验收闭环」是「能力自生长」的护栏：write_function 落盘时先做**纯静态**审计
（语法 / 公开函数 / 是否覆盖内置 / 危险调用），verify_tool 再在**子进程**里做一次
与主进程隔离的 import + 签名检查。于是「写错了」在写入那一刻就被告知，
而不是等 reload 把内核弄脏之后。

多模态：view_image 返回 ImageResult，Agent 会把图片注入上下文，
模型因此能「看见」图；自造工具只要返回 `openthree.ImageResult`（或
`images=[...]`）也能回传图片。
"""
from __future__ import annotations

import ast
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


def code_outline(path: str, include_private: bool = True, docstrings: bool = True,
                 max_items: int = 400) -> str:
    """用 ast 提取一个 .py 文件的类与函数签名大纲（含 docstring 首行、行号范围）。

    只解析、不导入、不执行，所以对任意大文件都安全且快。
    输出里带行号，可直接配合 read_file(offset=..., limit=...) 精读。

    Args:
        path: .py 文件路径（绝对，或相对当前工作目录）
        include_private: 是否包含 _ 开头的私有函数/方法（双下划线 dunder 始终包含）
        docstrings: 是否显示 docstring 首行
        max_items: 最多列出多少个节点，防止超大文件刷屏
    """
    try:
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = Path.cwd() / p
        if not p.exists():
            return f"[错误] 文件不存在：{p}"
        if p.is_dir():
            return f"[错误] 这是目录不是文件：{p}（试试 list_dir）"
        src = p.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(src, filename=str(p))
    except SyntaxError as exc:
        return f"[错误] 语法解析失败 {path}:{exc.lineno}: {exc.msg}"
    except Exception as exc:  # noqa: BLE001
        return f"[错误] {type(exc).__name__}: {exc}"

    n_lines = src.count("\n") + 1
    out: List[str] = [f"# {p.name} — {n_lines} 行  (ast 大纲)"]

    mod_doc = _outline_first_doc(tree)
    if mod_doc:
        out.append(f'"""{mod_doc}"""')

    mods: List[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            mods.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            mods.append(node.module or ".")
    if mods:
        out.append("# imports: " + ", ".join(sorted(set(mods))))

    counter = {"class": 0, "func": 0}
    lines: List[str] = []

    def emit(node: ast.AST, depth: int) -> None:
        pad = "    " * depth
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not _outline_keep(node.name, include_private):
                return
            counter["func"] += 1
            if counter["func"] + counter["class"] > max_items:
                return
            kind = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            deco = _outline_decorators(node)
            prefix = (deco + " ") if deco else ""
            span = f"L{node.lineno}-{node.end_lineno}"
            lines.append(f"{pad}{prefix}{kind} {node.name}({_outline_args(node.args)})"
                         f"{_outline_returns(node)}  @{span}")
            doc = _outline_first_doc(node) if docstrings else ""
            if doc:
                lines.append(f'{pad}    """{doc}"""')
        elif isinstance(node, ast.ClassDef):
            if not _outline_keep(node.name, include_private):
                return
            counter["class"] += 1
            if counter["func"] + counter["class"] > max_items:
                return
            bases = ", ".join(_outline_unparse(b) for b in node.bases)
            deco = _outline_decorators(node)
            prefix = (deco + " ") if deco else ""
            span = f"L{node.lineno}-{node.end_lineno}"
            head = f"{pad}{prefix}class {node.name}"
            if bases:
                head += f"({bases})"
            lines.append(f"{head}  @{span}")
            doc = _outline_first_doc(node) if docstrings else ""
            if doc:
                lines.append(f'{pad}    """{doc}"""')
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    emit(child, depth + 1)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            emit(node, 0)

    out.append(f"# 类 {counter['class']} 个 / 函数 {counter['func']} 个")
    out.append("")
    out.extend(lines)

    truncated = counter["func"] + counter["class"] > max_items
    if truncated:
        out.append(f"... [超过 max_items={max_items}，已截断]")
    if not lines:
        out.append("（没有类或函数定义）")
    return "\n".join(out)


# code_outline 的私有辅助（原名带下划线，注册器会自动忽略）
def _outline_keep(name: str, include_private: bool) -> bool:
    if include_private:
        return True
    if name.startswith("__") and name.endswith("__"):
        return True  # dunder 始终保留
    return not name.startswith("_")


def _outline_first_doc(node: ast.AST) -> str:
    try:
        doc = ast.get_docstring(node) or ""
    except Exception:  # noqa: BLE001
        return ""
    for line in doc.strip().splitlines():
        line = line.strip()
        if line:
            return line[:160]
    return ""


def _outline_decorators(node: ast.AST) -> str:
    decos = getattr(node, "decorator_list", None) or []
    parts = []
    for d in decos:
        try:
            parts.append("@" + ast.unparse(d))
        except Exception:  # noqa: BLE001
            parts.append("@?")
    return " ".join(parts)


def _outline_args(a: ast.arguments) -> str:
    parts: List[str] = []
    pos = list(getattr(a, "posonlyargs", [])) + list(a.args)
    defaults = list(a.defaults)
    pad = len(pos) - len(defaults)
    for i, arg in enumerate(pos):
        s = arg.arg + _outline_ann(arg.annotation)
        if i >= pad:
            s += " = " + _outline_unparse(defaults[i - pad])
        parts.append(s)
        if getattr(a, "posonlyargs", None) and i == len(a.posonlyargs) - 1:
            parts.append("/")
    if a.vararg is not None:
        parts.append("*" + a.vararg.arg + _outline_ann(a.vararg.annotation))
    elif a.kwonlyargs:
        parts.append("*")
    for arg, default in zip(a.kwonlyargs, a.kw_defaults):
        s = arg.arg + _outline_ann(arg.annotation)
        if default is not None:
            s += " = " + _outline_unparse(default)
        parts.append(s)
    if a.kwarg is not None:
        parts.append("**" + a.kwarg.arg + _outline_ann(a.kwarg.annotation))
    return ", ".join(parts)


def _outline_ann(annotation: Any) -> str:
    if annotation is None:
        return ""
    return ": " + _outline_unparse(annotation)


def _outline_returns(node: Any) -> str:
    r = getattr(node, "returns", None)
    return (" -> " + _outline_unparse(r)) if r is not None else ""


def _outline_unparse(node: Any) -> str:
    try:
        return ast.unparse(node)
    except Exception:  # noqa: BLE001
        return "?"


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

    落盘时会先做一次**纯静态审计**（不执行代码）：语法、有没有可注册的公开函数、
    是否覆盖了内置工具、有没有高危调用。通过后**必须**调用 reload_functions
    才会生效，并**务必用 list_functions 确认它真的出现在工具清单里**——
    文件写成功 ≠ 工具注册成功。

    文件里所有公开函数（不以 _ 开头）都会注册为工具。规范：
      * 写类型注解 —— 它会变成 JSON Schema
      * 写 docstring（Args 段写参数说明）—— 它会变成工具描述
      * 首个参数若命名为 kernel，会由运行期注入，模型看不到
      * 可 `from openthree import tool, KERNEL`
      * 模块级变量是**暂存**而非长存：reload 会重载模块，别拿它当缓存
        （要长存就用 KERNEL.state）

    写完想更稳，可以再调 `verify_tool(name)`：它在子进程里真正 import 一次，
    能提前发现「语法没错但一 import 就炸」的问题。

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

    builtin_names = set(KERNEL.registry.builtins) if KERNEL.registry is not None else None
    audit = _static_audit(code, builtin_names=builtin_names)
    if audit["syntax_error"]:
        if KERNEL.recorder is not None:
            KERNEL.recorder.record(
                "error", summary=f"写 function 语法错误: {path.name}",
                data={"action": "write_function", "name": name, "error": audit["syntax_error"]},
            )
        return (f"[错误] {path} 已写入，但语法检查未通过（尚未生效）。"
                f"请修正后重新 write_function：\n{audit['syntax_error']}")

    if KERNEL.recorder is not None:
        KERNEL.recorder.record(
            "evolution",
            summary=f"{'改写' if existed else '新造'} function: {path.name}",
            data={"action": "write_function", "name": name, "file": str(path),
                  "code": code, "overwrite": existed,
                  "tools": audit["functions"], "shadows": audit["shadows"]},
        )

    notes = _audit_notes(audit)
    head = f"[ok] 已写入 {path}"
    body = ("\n" + "\n".join(notes)) if notes else ""
    return (f"{head}{body}"
            f"\n下一步：reload_functions() 生效，然后 list_functions() **确认它真的在清单里**"
            f"（或用 verify_tool('{name}') 先在子进程里验收 import 与签名）。")


def verify_tool(name: str) -> str:
    """在**子进程**里验收一个自造 function —— 先证明它能跑，再让它上岗。

    与主进程隔离地真正 import 一次目标文件，然后按注册器**同一条构造路径**
    （openthree.tool.build_tool）检查每个公开函数能不能被建成工具、签名长什么样。

    为什么需要它：write_function 的静态审计只保证「语法没错」，而注册器用的是
    exec_module —— 模块级 import 失败、顶层代码抛错、签名与 Schema 不合约（默认
    参数取不到默认值就会 TypeError）都要等到 reload 才炸，而那时内核已被污染。
    本工具把这个错误提前到 reload 之前，且**零污染**（子进程死掉不影响内核）。

    Args:
        name: 文件名（不含 .py），或任意 .py 路径
    """
    ws = KERNEL.workspace
    if ws is None:
        return "[错误] 内核未就绪"
    path = _resolve_function_path(ws, name)
    if path is None:
        try:
            files = [p.name for p in ws.list_function_files()]
        except Exception:  # noqa: BLE001
            files = []
        listing = "、".join(files) if files else "（空）"
        return f"[错误] 找不到 {name}。.openthree/functions/ 下现有：{listing}"

    env = dict(os.environ)
    prev = env.get("PYTHONPATH", "")
    # 让子进程无论如何都能 import 到 openthree（未安装、只在源码树里跑的场景）
    pkg_parent = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = os.pathsep.join(
        [x for x in (str(ws.root), pkg_parent, prev) if x]
    )
    code, out, err = _run_capture(
        [sys.executable, "-c", _VERIFY_SCRIPT, str(path)],
        work=str(ws.root), timeout=60, env=env,
    )
    if code is None:                       # 连进程都起不来
        return f"[错误] 验收进程无法启动：{out or err}"

    report = _parse_verify_report(out)
    rel = _rel_to(ws.root, path)
    if report is None:
        detail = (err or out).strip()[:1500]
        return (f"[错误] 验收输出无法解析（进程 exit={code}）。原始输出：\n{detail}\n"
                f"这通常意味着它连 import 都过不去。")

    if not report.get("ok"):
        return (f"[错误] 验收未通过：{rel}\n  import 阶段失败：{report.get('error')}\n"
                f"  → 常见原因：模块顶部 import 了不存在的包、顶层代码抛异常、"
                f"或 `from openthree import ...` 写错。修正后再 write_function。")

    tools = report.get("tools") or []
    lines = [f"[ok] 验收通过：{rel}（已能 import）"]
    if report.get("classes"):
        lines.append(f"  （另有类 {', '.join(report['classes'])} —— 类不会被注册为工具）")
    if tools:
        lines.append(f"  reload 后会得到 {len(tools)} 个工具：")
        for t in tools:
            mark = "  ⚠ " if t.get("error") else "    - "
            sig = t.get("signature") or t.get("name")
            kernel_note = "（首参 kernel：运行期注入）" if t.get("needs_kernel") else ""
            lines.append(f"{mark}`{sig}`{kernel_note}"
                         + (f"  构造失败：{t['error']}" if t.get("error") else ""))
    else:
        lines.append("  ⚠ 没有任何可注册的公开函数（_ 开头的会被忽略）。")
    for w in report.get("warnings") or []:
        lines.append(f"  ⚠ {w}")
    if err.strip():
        lines.append(f"  （stderr：{err.strip()[:300]}）")
    tainted = bool(tools) and all(t.get("error") for t in tools)
    if not tainted:
        lines.append("  下一步：reload_functions() 生效，再 list_functions() 确认清单。")
    return "\n".join(lines)


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
    lines.append("当前工具清单：" + ", ".join(reg.names()))
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
    """读取当前的系统提示词与用户提示词。

    开头先给出系统提示词自带的**章节索引** —— 自省时先看结构一眼看清全貌，
    要用编辑器式操作改哪一段再决定；全文紧随其后。
    """
    ws = KERNEL.workspace
    if ws is None:
        return "[错误] 内核未就绪"
    sys_text = ws.read_system_prompt()
    sections = ws.analysis_sections(sys_text)
    idx = ("\n".join("- " + s for s in sections)) if sections else "（无章标题）"
    return (f"# 系统提示词 ({ws.system_prompt_file})\n\n"
            f"## 章节索引（{len(sections)} 节）\n{idx}\n\n"
            f"## 全文\n\n{sys_text}\n\n"
            f"# 用户提示词 ({ws.user_prompt_file})\n\n{ws.read_user_prompt()}")


def write_system_prompt(content: str) -> str:
    """改写系统提示词 —— 你对「我是谁、我该怎么做」的定义，下一步即刻生效。

    覆写前会自动把旧稿存进 prompts/ 的历史目录，因此改坏了可以用
    `restore_system_prompt` 回退，不必怕失手。想回看自己改过哪些版本，
    用 `list_prompt_versions`（列清单）、`read_prompt_version("-5")`（回看最近 5 版）。

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
    return f"[ok] 系统提示词已更新（{len(content)} 字符{saved}），下一步生效。"


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
    return f"[ok] 任务已更新（{len(content)} 字符），下一步生效。"


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
    """写下/更新你的计划——长任务的推进清单，它会每一步自动出现在你眼前。

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
    return f"[ok] 计划已更新（{len(content)} 字符，未完成 {open_items} 项），下一步生效。"


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
    rows.append("\n看某一版全文：`read_prompt_version(<序号>)`；"
                "回看概览：`read_prompt_version(\"-5\")`；回退：`restore_system_prompt(<序号或文件名>)`。")
    return "\n".join(rows)


def read_prompt_version(version: str = "1", chars: int = 6000) -> str:
    """读回**你自己**的历史版本 —— 自省就是能看见「我从前把自己定义成什么样」。

    每次 write_system_prompt 都会把旧稿自动存进 prompts/ 的历史目录，这里把它读回来。

    两种用法：
      * 传序号 / 文件名（如 "1"、`system-20261012-174900.md`）→ 完整读回那一版。
      * 传负数（如 "-5"）→ 回看最近 5 版的**概览**（名字 / 存档时间 / 首行），
        用来快速看清自己的提示词是怎么一步步长成现在这样的。

    Args:
        version: 版本序号（1 = 最新）/ 文件名 / 负数表示「回看最近 |N| 版概览」
        chars: 单版最多显示多少字符（超出截断；完整内容可用 read_file 打开原文件）
    """
    ws = KERNEL.workspace
    if ws is None:
        return "[错误] 内核未就绪"
    versions = ws.list_prompt_versions()
    raw = str(version if version is not None else "1").strip() or "1"

    # 负数（或 "-N"）→ 概览模式：回看自己最近的几步（没有历史时也给一句话）
    if raw.startswith("-") and raw[1:].isdigit():
        n = max(1, int(raw[1:]))
        if not versions:
            return ("（还没有历史版本。每次 write_system_prompt 覆写前都会自动存一版，"
                    "改一次自己的提示词就能在这里回看。）")
        shown = versions[:n]
        rows = [f"# 回看最近 {len(shown)} 个版本（共 {len(versions)} 版，新 → 旧）", ""]
        for i, p in enumerate(shown, 1):
            rows.append(f"{i}. `{p.name}`  （{_fmt_size(p)}）")
            head = _version_headline(p)
            if head:
                rows.append(f"   {head}")
        rows.append("")
        rows.append("看某一版全文：read_prompt_version(\"<序号>\")；回退：restore_system_prompt(\"<序号>\")。")
        return "\n".join(rows)

    if not versions:
        return ("（还没有历史版本。每次 write_system_prompt 覆写前都会自动存一版，"
                "改一次自己的提示词就能在这里回看。）")
    text = ws.read_prompt_version(raw)
    if text is None:
        return (f"[错误] 找不到版本：{version}。"
                "用 list_prompt_versions 看有哪些，或 read_prompt_version(\"-5\") 回看概览。")
    limit = max(200, int(chars or 6000))
    body = text if len(text) <= limit else (
        text[:limit] + f"\n... [已截断，共 {len(text)} 字符；完整内容见 {ws.versions_dir}/]")
    return body


def _fmt_size(p: Path) -> str:
    try:
        return f"{p.stat().st_size}B"
    except OSError:
        return "?B"


def _version_headline(p: Path) -> str:
    """取一个历史版本的「存档时间 + 首行」，用作概览。"""
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    stamp = ""
    first = ""
    for line in lines:
        s = line.strip()
        if not s:
            continue
        if not stamp and s.startswith("<!--"):
            stamp = s.strip("<!-> ").strip()
            continue
        if not first and not s.startswith("<!--"):
            first = s
            break
    parts = []
    if stamp:
        parts.append(stamp)
    if first:
        parts.append(first[:120])
    return " · ".join(parts)


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
    return f"[ok] 系统提示词已回退到 {name}，下一步生效。"


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


def compact_context(start: int, end: int, summary: str = "") -> str:
    """折叠一段对话历史，释放上下文空间。

    `summary` 给了 → 折叠成一条保留关键信息的摘要；`summary` 留空 → **直接丢弃**
    （最省，适合早期已无用的步骤）。折叠会**立即**生效，并如实返回省下多少字符。
    折到哪里、要不要折，由你判断：先用 `context_usage` 看用量与可折范围。

    Args:
        start: 起始步骤号（含）
        end: 结束步骤号（含）
        summary: 摘要（保留关键事实/数值/结论/未完成任务）；留空 = 直接丢弃
    """
    ok, msg = False, "[错误] 内核未就绪"
    if KERNEL.agent is not None:
        ok, msg = KERNEL.agent.compact(start, end, summary)
    if KERNEL.recorder is not None:
        KERNEL.recorder.record(
            "evolution" if ok else "error",
            summary=(f"压缩上下文 step {start}-{end}" if ok else msg[:200]),
            data={"action": "compact_context", "start": start, "end": end,
                  "summary": summary, "ok": ok, "dropped": not (summary or "").strip()},
        )
    return msg


def context_usage() -> str:
    """看一眼「上下文用量」——你正在用多少、还剩多少、能折哪一段。

    自管上下文的前提是看得见。它返回：已用 / 预算字符数、占比、当前 step 数、
    可折叠的范围、自动折叠是否开启、以及已经折叠过几次。想调整预算或开关自动
    折叠，用 `context_budget`。
    """
    if KERNEL.agent is None:
        return "[错误] 内核未就绪"
    s = KERNEL.agent.history_stats()
    budget = s["budget_chars"]
    if budget > 0:
        head = f"已用 {s['used_chars']} / {budget} 字符（{s['ratio'] * 100:.1f}%）"
    else:
        head = f"已用 {s['used_chars']} 字符（预算 0 = 已关闭自动折叠）"
    lines = [
        head,
        f"当前 {s['steps']} 个 step；可折叠范围：{s['fold_range'] or '（暂无，至少走完一个工具回合）'}",
        f"自动折叠：{'开' if s['auto_fold'] else '关'}"
        + ("（被环境变量 OPENTHREE_CONTEXT_AUTOFOLD 锁定，改不了）"
           if s["auto_fold_env_locked"] else ""),
        f"已折叠 {s['compactions']} 次",
    ]
    return "\n".join(lines)


def context_budget(budget_chars: Optional[int] = None,
                   auto_fold: Optional[bool] = None) -> str:
    """调整「上下文策略」——改预算、开关自动折叠。下一步即生效。

    这是你自管上下文的旋钮：预算给多大、要不要内核兜底自动折叠，都由你决定。
    `context_usage` 是它的读数。两个参数至少给一个。

    Args:
        budget_chars: 历史字符预算；0 = 关闭自动折叠。不传 = 不改预算。
        auto_fold: 是否让内核在爆仓时兜底折叠；不传 = 不改。
    """
    if KERNEL.agent is None:
        return "[错误] 内核未就绪"
    ok, msg = KERNEL.agent.set_context_budget(budget_chars=budget_chars,
                                              auto_fold=auto_fold)
    if KERNEL.recorder is not None:
        KERNEL.recorder.record(
            "evolution" if ok else "error",
            summary=f"调整上下文策略：{msg}" if ok else msg[:200],
            data={"action": "context_budget", "budget_chars": budget_chars,
                  "auto_fold": auto_fold, "ok": ok},
        )
    return msg


def limits_usage() -> str:
    """看一眼「运行边界」——你能走多少步、最多几轮、生成多长、单条工具结果留多宽。

    自管边界的前提是看得见。返回：每轮步数上限 / 最多轮数 / max_tokens /
    工具结果字符上限 / temperature，以及每个值「谁定的」（内核默认 / 模型自管 /
    显式锁定）。想调整它们，用 `set_limits`。

    **步与轮是两层**：`max_iterations` 只管**一轮**；一轮没做完会由运行期开下一轮，
    整场最多跑 `max_rounds` 轮。长任务做不完时，先想「还差几轮」，而不是一味堆步数。
    """
    if KERNEL.agent is None:
        return "[错误] 内核未就绪"
    s = KERNEL.agent.limits_stats()
    src = s["source"]
    lines = [
        f"每轮步数上限：{s['max_iterations']} 步（{src['max_iterations']}）",
        f"整场最多轮数 max_rounds：{s['max_rounds']} 轮（{src['max_rounds']}）",
        f"生成上限 max_tokens：{s['max_tokens']}（{src['max_tokens']}）",
        f"工具结果上限：{s['tool_result_limit']} 字符（{src['tool_result_limit']}）",
        f"temperature：{s['temperature']}（{src['temperature']}）",
    ]
    if s["locked"]:
        lines.append("（运行边界被环境变量 OPENTHREE_LIMITS_LOCK 冻结，模型改不动）")
    return "\n".join(lines)


def set_limits(max_iterations: Optional[int] = None,
               max_tokens: Optional[int] = None,
               tool_result_limit: Optional[int] = None,
               temperature: Optional[float] = None,
               max_rounds: Optional[int] = None) -> str:
    """调整「运行边界」——每轮步数 / 最多轮数 / 生成长度 / 工具结果宽度 / 温度。
    下一步即生效。

    这是你自管「运行边界」的旋钮：一轮能走多远、整场能跑几轮、一次输出多长、
    单条工具结果留多宽，都由你决定。`limits_usage` 是它的读数。被启动参数显式锁定、
    或被环境变量 `OPENTHREE_LIMITS_LOCK` 冻结时，这些旋钮改不动（会如实告诉你）。

    Args:
        max_iterations: **每轮** ReAct 步数上限；不传 = 不改。
        max_tokens: 单次生成 token 上限（`config.yaml` 的 llm.max_tokens）；不传 = 不改。
        tool_result_limit: 单个工具结果进 history 的字符上限，超出落盘；不传 = 不改。
        temperature: 采样温度（0~2）；不传 = 不改。
        max_rounds: **整场任务**最多自动接力几轮（一轮=一次完整 ReAct 循环）；不传 = 不改。
    """
    if KERNEL.agent is None:
        return "[错误] 内核未就绪"
    ok, msg = KERNEL.agent.set_limits(max_iterations=max_iterations,
                                      max_tokens=max_tokens,
                                      tool_result_limit=tool_result_limit,
                                      temperature=temperature,
                                      max_rounds=max_rounds)
    if KERNEL.recorder is not None:
        KERNEL.recorder.record(
            "evolution" if ok else "error",
            summary=f"调整运行边界：{msg}" if ok else msg[:200],
            data={"action": "set_limits", "max_iterations": max_iterations,
                  "max_tokens": max_tokens, "tool_result_limit": tool_result_limit,
                  "temperature": temperature, "max_rounds": max_rounds, "ok": ok},
        )
    return msg


# ---------------------------------------------------------------- 收尾


def next_round(summary: str = "") -> str:
    """宣告「这一阶段做完了，把剩下的留到下一轮」——收本轮，不算整件事完成。

    与 `finish` 的区别：`finish` 是「整件事做完了」；`next_round` 是「这一轮先到这，
    剩下的下一轮接着做」。一轮 = 一次完整的 ReAct 循环，**每轮有独立的步数预算**。

    - 处在**自动接力**里（处境会告诉你 `next` 是否为真）：本消息收尾后，运行期会新开一轮，
      再把 `summary` 当作交接说明交给你下一轮。这正是「长任务分轮推进」的正规出口。
    - 处在**单次任务**里（没有下一轮）：它只是把发言权交还，运行期会如实告诉你没有下一轮 ——
      如果你其实还没做完，应该在 `finish` 里把「还剩什么」说清楚，或用 `set_limits(max_rounds=…)`
      申请更多轮（若启动时开了自动接力）。

    Args:
        summary: 给「下一轮的你」的交接说明（已完成什么、还剩什么、下一步做什么）。
    """
    if KERNEL.agent is None:
        return "[错误] 内核未就绪"
    r = KERNEL.agent.round_stats()
    KERNEL.agent.next_round(summary)
    if KERNEL.recorder is not None:
        KERNEL.recorder.record(
            "finish" if not r.get("next") else "note",
            summary=f"第 {r['index']} 轮收尾，交由下一轮：{summary[:160]}",
            data={"action": "next_round", "round": r["index"],
                  "max_rounds": r["max"], "remaining": r["remaining"],
                  "summary": summary},
        )
    if r.get("next"):
        return (f"[ok] 已结束第 {r['index']} 轮（本场共 {r['max']} 轮，还剩 {r['remaining']} 轮）。"
                f"运行期将开下一轮；已把你的交接说明记入工作记录。")
    return ("[ok] 已结束本轮 —— 但这是**单次任务，没有下一轮**：进程到此收尾。"
            "若事情没做完，请在 finish 里写清「还剩什么」；"
            "想要真正的多轮接力，需要用自动接力启动（CLI `--auto N`）。")


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


# 「高危调用」清单：只是**提醒**（很多合法的自造工具确实要起子进程），
# 但值得让模型知道「你写的这行会动到系统」——写出来时就说清，胜过事后排查。
_DANGEROUS_CALLS = frozenset({
    "eval", "exec", "compile", "__import__",
    "os.system", "os.popen", "os.remove", "os.unlink", "os.rmdir", "os.kill",
    "os._exit", "shutil.rmtree", "subprocess.run", "subprocess.Popen",
    "subprocess.call", "subprocess.check_output", "socket.socket", "ctypes.CDLL",
})


def _static_audit(code: str, builtin_names: Optional[set] = None) -> Dict[str, Any]:
    """对一份待落盘的 function 源码做**纯静态**体检（只解析、不执行）。

    返回 dict：``syntax_error`` / ``functions``（可注册的公开函数名）/
    ``all_functions`` / ``shadows``（覆盖了同名内置工具的公开函数）/
    ``dangerous``（高危调用，字符串形式）。

    注册器最终会 ``exec_module``、并按「本文件定义 + 不由 _ 开头」收函数，
    所以这里用**同一条规则**预先算一遍 —— 把「写错了」提前到写入那一刻。
    """
    result: Dict[str, Any] = {
        "syntax_error": "", "functions": [], "all_functions": [],
        "shadows": [], "dangerous": [],
    }
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        result["syntax_error"] = f"{type(exc).__name__}: {exc.msg} (line {exc.lineno})"
        return result
    except Exception as exc:  # noqa: BLE001
        result["syntax_error"] = f"{type(exc).__name__}: {exc}"
        return result

    public: List[str] = []
    all_names: List[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            all_names.append(node.name)
            if not node.name.startswith("_"):
                public.append(node.name)
    result["functions"] = public
    result["all_functions"] = all_names
    if builtin_names:
        result["shadows"] = sorted(n for n in public if n in builtin_names)

    found: List[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        label = _call_label(node.func)
        if label in _DANGEROUS_CALLS and label not in found:
            found.append(label)
    result["dangerous"] = found
    return result


def _call_label(func: ast.AST) -> str:
    """把 ast 的调用目标还原成 `os.system` / `exec` 这样的字符串。"""
    parts: List[str] = []
    node: Any = func
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    else:
        return ""
    return ".".join(reversed(parts))


def _audit_notes(audit: Dict[str, Any]) -> List[str]:
    """把静态审计结果翻译成给模型看的提示行（没有问题时返回空列表）。"""
    notes: List[str] = []
    if audit.get("functions"):
        notes.append(f"可注册工具：{', '.join(audit['functions'])}")
    else:
        warn = _public_function_warning_from_audit(audit)
        if warn:
            notes.append("⚠ " + warn)
    if audit.get("shadows"):
        notes.append("⚠ 这些公开函数与**内置工具同名**，reload 后会覆盖内置："
                     + ", ".join(audit["shadows"])
                     + "。若非本意，请改名（覆盖内置会砍掉你自己的感知通道）。")
    if audit.get("dangerous"):
        notes.append("⚠ 检测到高危调用：" + ", ".join(audit["dangerous"])
                     + "（确认这是你想要的；它们会动到系统）")
    return notes


def _public_function_warning_from_audit(audit: Dict[str, Any]) -> str:
    if audit.get("syntax_error") or audit.get("functions"):
        return ""
    names = audit.get("all_functions") or []
    hint = ("（你定义了 " + ", ".join(f"`{n}`" for n in names)
            + "，但都以下划线开头，会被当作私有函数忽略）") if names else ""
    return ("这个文件里没有可注册的公开函数，reload 后不会出现任何新工具。"
            "请把要暴露给模型的函数改成不以 `_` 开头的顶层函数。" + hint)


def _public_function_warning(code: str) -> str:
    """语法合法但**没有一个公开函数**时给出提醒 —— 否则这是一次静默的失败。"""
    return _public_function_warning_from_audit(_static_audit(code))


# ---------------------------------------------------------------- 验收（子进程）

# 验收脚本：以「文件路径」为 argv（子进程里 argv[0] 会被吃掉，路径必须显式传），
# 用与注册器完全相同的 build_tool 构造工具，把结果以一行 JSON 吐回主进程。
# 绝不 import openthree.builtins —— 避免把整个内核拖进子进程。
_VERIFY_SCRIPT = r"""
import sys, json, importlib.util, traceback

def _load(path):
    spec = importlib.util.spec_from_file_location("openthree_verify_probe", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["openthree_verify_probe"] = mod
    spec.loader.exec_module(mod)
    return mod

def _first_doc(obj):
    d = getattr(obj, "__doc__", None) or ""
    for line in d.strip().splitlines():
        line = line.strip()
        if line:
            return line[:160]
    return ""

def main():
    path = sys.argv[1]
    rep = {"ok": False, "tools": [], "classes": [], "warnings": []}
    try:
        mod = _load(path)
    except BaseException as e:
        rep["error"] = f"{type(e).__name__}: {e}"
        print("OPENTHREE_VERIFY_JSON=" + json.dumps(rep, ensure_ascii=False))
        return
    rep["ok"] = True
    try:
        from openthree.tool import build_tool
    except BaseException as e:
        rep["warnings"].append(f"无法 import openthree.tool（{type(e).__name__}: {e}）")
        build_tool = None
    import inspect
    for nm in dir(mod):
        if nm.startswith("_"):
            continue
        obj = getattr(mod, nm)
        if isinstance(obj, type):
            rep["classes"].append(nm)
            continue
        if not callable(obj):
            continue
        if getattr(obj, "__module__", None) != mod.__name__:
            continue
        item = {"name": nm, "signature": nm, "error": "", "brief": _first_doc(obj),
                "needs_kernel": False}
        try:
            sig = inspect.signature(obj)
            item["signature"] = str(sig)
            if build_tool is not None:
                t = build_tool(obj, origin="user", source_file=path)
                item["needs_kernel"] = bool(t.needs_kernel)
                item["signature"] = t.signature_hint()
        except BaseException as e:
            item["error"] = f"{type(e).__name__}: {e}"
        rep["tools"].append(item)
    print("OPENTHREE_VERIFY_JSON=" + json.dumps(rep, ensure_ascii=False))

main()
"""


def _resolve_function_path(ws: Any, name: str) -> Optional[Path]:
    """把「函数名」或「.py 路径」统一解析成一个存在的文件路径。"""
    raw = str(name or "").strip().strip('"').strip("'")
    if not raw:
        return None
    cand = Path(raw).expanduser()
    tries: List[Path] = []
    looks_like_path = cand.suffix == ".py" or "/" in raw or "\\" in raw
    if looks_like_path:
        for base in (Path.cwd(), ws.root, ws.functions_dir):
            tries.append(cand if cand.is_absolute() else base / cand)
    tries.append(ws.functions_dir / f"{Path(raw).stem}.py")
    for p in tries:
        try:
            if p.exists() and p.is_file():
                return p.resolve()
        except OSError:
            continue
    return None


def _rel_to(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root)).replace("\\", "/")
    except ValueError:
        return str(path)


def _run_capture(cmd: List[str], work: str, timeout: int,
                 env: Optional[Dict[str, str]] = None) -> tuple:
    """跑一个短命子进程并把输出收干净；返回 (returncode|None, stdout, stderr)。

    返回码为 None 表示**进程根本没起来**（可执行文件缺失等）。超时会被强杀，
    绝不无限等待（与 run_shell 同一套原则，只是这里够简单，不必杀整棵树）。
    """
    try:
        proc = subprocess.Popen(
            cmd, cwd=work, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        )
    except Exception as exc:  # noqa: BLE001
        return None, "", f"{type(exc).__name__}: {exc}"
    try:
        out_b, err_b = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        try:
            out_b, err_b = proc.communicate(timeout=5)
        except Exception:  # noqa: BLE001
            out_b, err_b = b"", b""
        return proc.returncode, _decode(out_b), _decode(err_b) + f"\n[验收超时 {timeout}s，已强杀]"
    except Exception as exc:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        return proc.returncode, "", f"{type(exc).__name__}: {exc}"
    return proc.returncode, _decode(out_b), _decode(err_b)


_VERIFY_MARKER = "OPENTHREE_VERIFY_JSON="


def _parse_verify_report(text: str) -> Optional[Dict[str, Any]]:
    """从子进程 stdout 里取出验收 JSON（取最后一行标记，容错多余输出）。"""
    if not text:
        return None
    for line in reversed(text.splitlines()):
        idx = line.find(_VERIFY_MARKER)
        if idx >= 0:
            payload = line[idx + len(_VERIFY_MARKER):].strip()
            try:
                data = json.loads(payload)
                return data if isinstance(data, dict) else None
            except Exception:  # noqa: BLE001
                return None
    return None


BUILTIN_FUNCTIONS: Dict[str, Any] = {
    "view_image": view_image,
    "read_file": read_file,
    "write_file": write_file,
    "list_dir": list_dir,
    "search_files": search_files,
    "code_outline": code_outline,
    "run_shell": run_shell,
    "write_function": write_function,
    "verify_tool": verify_tool,
    "reload_functions": reload_functions,
    "delete_function": delete_function,
    "list_functions": list_functions,
    "read_prompts": read_prompts,
    "write_system_prompt": write_system_prompt,
    "write_user_prompt": write_user_prompt,
    "read_plan": read_plan,
    "write_plan": write_plan,
    "list_prompt_versions": list_prompt_versions,
    "read_prompt_version": read_prompt_version,
    "restore_system_prompt": restore_system_prompt,
    "note": note,
    "remember": remember,
    "recall_memory": recall_memory,
    "compact_context": compact_context,
    "context_usage": context_usage,
    "context_budget": context_budget,
    "limits_usage": limits_usage,
    "set_limits": set_limits,
    "next_round": next_round,
    "finish": finish,
    "get_time": get_time,
}
