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
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, NamedTuple, Optional

from .kernel import KERNEL
from .multimodal import ImageResult
from .workspace import (REGISTERED_FUNCTION_DISTANCE, USER_FUNCTION_DISTANCE,
                        canon_path, read_source, sha256_file)

# ---------------------------------------------------------------- 看世界


def view_image(path: str, detail: str = "auto") -> ImageResult:
    """查看一张图片（多模态）。返回的图片会被放进上下文，你就能「看见」它。

    本地路径（相对/绝对）或 http(s) 链接均可。图片进入上下文后即可描述、
    识别、做 OCR 判断等。一次只看一张，避免上下文被图片塞满。

    Args:
        path: 图片路径或 http(s) 链接
        detail: 传给模型的清晰度 auto / low / high；low 更省 token

    Returns:
        `ImageResult`：`text` 是给人看的一句话，`images` 是图片路径（Agent 会注入上下文）；出错时返回 `[错误] …` 文本。
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

    Returns:
        文件正文（带行号，超出 limit 会标注显示范围）；失败返回 `[错误] …`。
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

    Returns:
        目录条目清单；失败返回 `[错误] …`。
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

    Returns:
        「文件:行号: 内容」命中清单（无命中会说「无命中」）；失败返回 `[错误] …`。
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

    Returns:
        类 / 函数签名大纲（含行号范围）；失败返回 `[错误] …`。
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

    Returns:
        `[ok] 已写入 …`；失败返回 `[错误] …`。
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
    输出管道而永久挂住。stdin 不继承终端，交互式命令会立刻得到 EOF 而退出。

    最坏阻塞时长是 `timeout + 15s`（强杀进程树 ≤10s，管道收尾宽限 5s）。
    越过这条线的唯一形态是「管道写端被一个已脱离的孙进程攥住」：此时直接
    子进程已无可杀的 PID，EOF 永不到来 —— 函数放弃读取并立刻返回，报
    「输出管道未能收尾」，而不是停在原地等。

    Args:
        command: 要执行的命令
        cwd: 工作目录（默认当前目录）
        timeout: 超时秒数（1~3600，越界会被截到边界）

    Returns:
        命令输出，末尾带 `exit=N`；进程起不来时返回 `[错误] …`。
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
            # 极端兜底：管道 EOF 等不到 —— 写端攥在一个**脱离的孙进程**手里
            # （它继承了 stdout/stderr，而直接子进程已退出、没有 PID 可杀）。
            # 此刻唯一正确的动作是**放弃读取、立刻返回**。
            #
            # 绝不在这里 close() 那两个管道对象：communicate() 起的读线程正阻塞
            # 在同一个缓冲区上，close() 要先拿到同一把锁 —— 会和读线程互锁，把
            # 「超时兜底」变成永久挂死（实测）。读线程是 daemon，随本进程退出即
            # 消失，那两根管道一并交还操作系统，不必在这里收。
            stream_cut = True
            out_b, err_b = b"", b""
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


def write_function(name: str, code: str, overwrite: bool = True,
                   target: str = "") -> str:
    """写出一个新的 function：把 python 代码存为 .openthree/functions/<name>.py。

    落盘时会先做一次**纯静态审计**（不执行代码）：语法、有没有可注册的公开函数、
    是否覆盖了内置工具、有没有高危调用。通过后**必须**调用 reload_functions
    才会生效，并**务必用 list_functions 确认它真的出现在工具清单里**——
    文件写成功 ≠ 工具注册成功。

    文件里所有公开函数（不以 _ 开头）都会注册为工具。规范：
      * 写类型注解 —— 它会变成 JSON Schema
      * 写 docstring（Args 段写参数说明）—— 它会变成工具描述
      * **function 也可以只返回文字**（一段做法 / 规范 / 判据）：它不只是动作，
        也可以是知识，调它就是把那段指导读进上下文。首行写成「什么时候该调我」，
        因为那就是模型唯一的索引；正文纯文本、无副作用、可反复调
      * 首个参数若命名为 kernel，会由运行期注入，模型看不到
      * 可 `from openthree import tool, KERNEL`
      * 模块级变量是**暂存**而非长存：reload 会重载模块，别拿它当缓存
        （要长存就用 KERNEL.state）
      * **文件末尾加命令行尾钩**，于是这个文件既是你手上的工具，也是人能直接
        跑的命令行程序（`python 本文件.py --help`）：
            def __main__(argv=None) -> int:
                \"\"\"命令行入口。\"\"\"
                from openthree.cli_tool import run_cli
                return run_cli(globals(), argv=argv)

            if __name__ == "__main__":
                raise SystemExit(__main__())
        入口名必须是 `__main__`：dunder 不会被注册成工具；叫 main/cli 会凭空
        多出一个空参数工具。没加尾钩也仍可用
        `python -m openthree.tools --file <本文件> <函数> ...` 调用。

    写完想更稳，可以再调 `verify_tool(name)`：它在子进程里真正 import 一次，
    能提前发现「语法没错但一 import 就炸」的问题。

    Args:
        name: 文件名（不含 .py），也是这次演化的名字
        code: 完整 python 源码
        overwrite: 已存在时是否覆盖
        target: 写到哪一层。**留空 = 本项目**（默认，随手实验不污染共享库）；
            也可以是一个**已挂载的 profile 目录**（就是 `-profile` 给过的那个路径，
            `list_functions` 会把它们列出来）。只收路径、不收序号 —— 「哪一层」
            只该有一个真源。

    Returns:
        `[ok] 已写入 …` 加静态审计提示；语法错误等失败返回 `[错误] …`。
        落到 profile 时还会说明**它在本项目里会不会生效** —— 共享层常被本项目
        的同名工具盖住，不说清楚就是静默失败。
    """
    ws = KERNEL.workspace
    if ws is None:
        return "[错误] 内核未就绪"
    target_dir, terr = _resolve_write_target(ws, target)
    if target_dir is None:
        return terr
    path = ws.function_path(name, target_dir)
    if path.exists() and not overwrite:
        return f"[错误] {path.name} 已存在（overwrite=false）"
    existed = path.exists()
    if not target_dir.is_dir():      # profile 的 functions 目录可能还没建
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return f"[错误] 建不了 {target_dir}：{exc}"
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
    shadow = _shadow_note(ws, audit["functions"], path) if (target or "").strip() else ""
    return (f"{head}{shadow}{body}"
            f"\n下一步：reload_functions() 生效，然后 list_functions() **确认它真的在清单里**"
            f"（或用 verify_tool('{name}') 先在子进程里验收 import 与签名）。")


def _resolve_write_target(ws, target: str) -> tuple[Optional[Path], str]:
    """把 `write_function(target=…)` 解析成一个 functions 目录。

    只接受**已挂载**的 profile。写到没挂载的地方必然是「写了不生效」——那正是
    这里要防的坑，所以直接拒绝，并把可写的位置全列出来让人挑。

    Returns:
        `(目录, 错误信息)`；目录为 None 时错误信息非空。
    """
    raw = (target or "").strip()
    if not raw:
        return ws.functions_dir, ""
    try:
        want = canon_path(Path(raw).expanduser())
    except (OSError, RuntimeError):      # pragma: no cover - 路径异常
        return None, f"[错误] target 不是有效路径：{target}"
    roots: List[str] = []
    for src in ws.function_sources(include_missing=True):
        # 只有 profile 档能被 target 指到：project 是默认（留空即可），
        # user 档要 register_function 才生效，写它等于写一个没人加载的文件。
        if src.distance == 0 or src.distance >= USER_FUNCTION_DISTANCE:
            continue
        root = src.path.parents[1]       # <p>/.openthree/functions → <p>
        if canon_path(root) == want:
            return src.path, ""
        roots.append(str(root))
    listing = ("\n".join(f"  - {r}" for r in roots)
               or "  （一个 profile 都没挂：先用 -profile 点名）")
    return None, (f"[错误] target={target!r} 不是任何**已挂载的** profile。\n"
                  f"可写的位置只有这些：\n"
                  f"  - 留空 → 本项目 {ws.functions_dir}\n"
                  f"{listing}\n"
                  f"不允许写到没挂载的地方 —— 那必然是「写了不生效」。")


def _shadow_note(ws, tool_names: List[str], path: Path) -> str:
    """写到共享层时，逐个工具名回答「那它在本项目里会不会生效」。

    判据是**工具名**而不是文件名：一个文件可以提供多个工具，按文件名查会漏掉
    「另一个文件提供了同名工具」这种最常见的影子 —— 而那正是要防的静默失败。

    不拒绝、也不加 force：「更新共享版、而本项目保留自己那份」是合法的演化姿势
    （先在项目里长熟，再提升上去），拒绝它就得再造一个开关。
    """
    reg = KERNEL.registry
    if reg is None or not tool_names:
        return ""
    mine = canon_path(path)
    mine_d = ws.function_distance(path)
    losers: List[str] = []
    for n in tool_names:
        win = reg.get(n)
        if win is None or not win.source_file or win.origin == "builtin":
            continue
        if canon_path(win.source_file) == mine:     # 就是这份：覆盖自己，不算影子
            continue
        if win.source_distance >= mine_d:           # 比这份更远：这份会赢
            continue
        losers.append(f"  - `{n}` 现在由 {Path(win.source_file).name}"
                      f"（d={win.source_distance}）提供，比这份 d={mine_d} 更近")
    if not losers:
        return ""
    return ("\n注意：以下工具在本项目里**不会**用刚写的这份，因为有更近的同名工具：\n"
            + "\n".join(losers)
            + "\n  → 想改本项目的：target 留空"
              "\n  → 想让本项目也用共享这份：删掉或改名项目里提供它的那个文件")


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

    Returns:
        验收结论（能否 import、会得到哪些工具）；失败返回 `[错误] …`。
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
    """重新扫描 .openthree/functions/ 并注册全部工具。写完新 function 后调用。

    Returns:
        加载结果（新增 / 移除 / 异常文件）与当前工具清单；失败返回 `[错误] …`。
    """
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
    if reg.overrides:
        lines.append(f"同名覆盖（按距离裁决，近者生效）{len(reg.overrides)} 条：")
        for o in reg.overrides:
            win = Path(o.winner).name if o.winner != "内置" else "内置"
            lose = Path(o.loser).name if o.loser != "内置" else "内置"
            lines.append(f"  - {o.tool}：{win} [{o.winner_kind}] 盖住 {lose} [{o.loser_kind}]")
    if reg.missing_registered:
        lines.append(f"⚠ {len(reg.missing_registered)} 个已登记的文件不在了（条目保留，没自动清）：")
        for p in reg.missing_registered:
            lines.append(f"  - {p}")
    if reg.changed_registered:
        lines.append(f"⚠ {len(reg.changed_registered)} 个已登记文件内容已变"
                     f"（按引用加载，用的就是新内容）：")
        for c in reg.changed_registered:
            lines.append(f"  - {c['path']}  {c['registered_sha256'][:12]} → {c['current_sha256'][:12]}")
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

    Returns:
        `[ok] 已删除 …`；失败返回 `[错误] …`。
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


def register_function(path: str, dry_run: bool = False, note: str = "") -> str:
    """把**已有的** function 文件登记为本项目的工具 —— 只登记路径，不复制文件。

    什么时候该调我：`lookup_functions` 列出某个「可注册」的文件，而你判断它能用。
    内核自动加载本项目的 function 与命令行 `-profile` 点名的目录；用户目录
    （`~/.openthree/functions`）里写好的工具一直存在，但没被你授权就不生效 ——
    这个工具就是那次授权。

    它按**引用**工作，不复制：源文件修好 bug，下次 `reload_functions` 就吃到。
    代价是不可复现 —— 源被改、被移走，行为就跟着变。所以登记时记下 sha256，
    之后每次 reload 拿它比对，变了就**如实报告**（只报告，不拦截、不回滚）。

    接受的路径**必须**落在 `.openthree/functions` 目录族内（本项目 / 命令行点名的
    profile / 用户目录）。不接受任意 `.py`：那等于给自己开一个静默的任意代码入口。
    `lookup_functions` 给出的绝对路径正好都在这个范围里。

    登记成功 **≠** 工具生效：同名工具按**距离**裁决（越近越优先）。你登记的那份
    若被更近的同名工具盖住，这里会明说「未生效」并告诉你是谁盖的 —— 不假装成功，
    否则你会反复登记同一个文件、却一直拿不到那个能力。

    Args:
        path: function 文件的**绝对路径**（来自 lookup_functions 的输出）
        dry_run: 只算不写 —— 先看哪些会生效、哪些会被盖住，再决定要不要落笔
        note: 给自己留的一句话，记进登记簿（例如「用它抽 PDF」）

    Returns:
        登记结果（逐工具「生效 / 未生效 + 出路」）与 reload 增量；失败返回 `[错误] …`。
    """
    ws = KERNEL.workspace
    if ws is None:
        return "[错误] 内核未就绪"
    reg = KERNEL.registry

    raw = str(path or "").strip().strip('"').strip("'")
    if not raw:
        return "[错误] 需要一个 function 文件的绝对路径（先用 lookup_functions() 找）"
    cand = Path(raw).expanduser()
    if not cand.is_absolute():
        return (f"[错误] 只接受绝对路径：{raw}\n"
                f"lookup_functions() 给出的就是绝对路径，直接把它传进来。")
    try:
        cand = cand.resolve()
    except (OSError, RuntimeError):  # pragma: no cover - 路径异常
        pass
    if cand.suffix.lower() != ".py":
        return f"[错误] 只接受 .py 文件：{cand}"
    if not cand.is_file():
        return f"[错误] 不是一个可读文件（不存在 / 是目录）：{cand}"

    src = ws.function_source_for(cand)
    if src is None:
        scope = "\n".join(f"  - {s.label}：{s.path}"
                          for s in ws.function_sources(include_missing=True))
        return (f"[错误] {cand}\n"
                f"不在 function 的源目录族内 —— 只接受这些位置下的文件：\n{scope}\n"
                f"（不接受任意 .py：那等于给自己开一个静默的任意代码入口。）")

    try:
        # 按 PEP 263 编码声明解码：手写 function 在中文 Windows 上常是 GBK，
        # 硬按 UTF-8 读会抛 UnicodeDecodeError —— 那是 ValueError，不是 OSError，
        # 于是异常逃出工具、打断调用链，而不是回一句 `[错误] …`。
        code = read_source(cand)
    except (OSError, ValueError, SyntaxError) as exc:
        return f"[错误] 读不了 {cand}：{type(exc).__name__}: {exc}"
    builtin_names = set(reg.builtins) if reg is not None else None
    audit = _static_audit(code, builtin_names=builtin_names)
    if audit["syntax_error"]:
        return f"[错误] {cand.name} 语法检查未通过，未登记：\n{audit['syntax_error']}"
    if not audit["functions"]:
        return (f"[错误] {cand.name} 没有可注册的公开函数，未登记。\n"
                f"{_public_function_warning_from_audit(audit)}")

    if src.distance < USER_FUNCTION_DISTANCE:
        # 判据只有距离：d=0 是本项目，0<d<USER 是命令行点名的 profile —— 两档都
        # 自动加载，登记纯属多余。对 profile 还有第二重理由：路径一旦写进
        # functions.json，下次不挂这个 profile 运行时它就在源目录族外，每次启动
        # 都报一条 load_error。所以劝阻的判据是「会不会自动加载」，不是「是不是本项目」。
        if src.distance == 0:
            where = f"它就在本项目的 functions 目录里（{src.path}）"
            why = "这一档的文件自动加载，登记是多余的。"
            tail = "改完直接 reload_functions() 就生效。"
        else:
            where = f"它在一个已挂载的 profile 里（{src.path}）"
            why = ("-profile 点名的目录自动加载，登记是多余的；\n"
                   "  而且登记项一旦写进 functions.json，下次不挂这个 profile 运行时\n"
                   "  它就成了族外路径，每次启动都会报一条 load_error。")
            tail = "改完直接 reload_functions() 就生效；下次运行记得照旧挂上它。"
        return f"[无需登记] {cand}\n{where} —— {why}\n{tail}"

    names: List[str] = list(audit["functions"])
    try:
        sha = sha256_file(cand)
    except OSError as exc:      # 文件锁 / 权限：别让异常逃出工具打断调用链
        return f"[错误] 读不了 {cand}（算 sha256 失败）：{type(exc).__name__}: {exc}"
    meta = ws.read_functions_meta()
    regs = meta["registered"]
    key = canon_path(cand)
    existed = None
    for rec in regs:
        try:
            if canon_path(rec.get("path")) == key:
                existed = rec
                break
        except (OSError, RuntimeError):  # pragma: no cover - 脏记录
            continue

    if not dry_run:
        stamp = datetime.now().isoformat(timespec="seconds")
        if existed is not None:
            existed.update({"sha256": sha, "registered_at": stamp})
            if note:
                existed["note"] = note
        else:
            regs.append({"path": str(cand), "sha256": sha,
                         "registered_at": stamp, "note": note})
        ws.write_functions_meta(meta)

    before = set(reg.tools) if reg is not None else set()
    if not dry_run and reg is not None:
        reg.reload()
    after = set(reg.tools) if reg is not None else set()

    head = "预演（未写入）" if dry_run else ("已更新登记" if existed else "已登记")
    lines = [f"[{head}] {cand}",
             f"  来源：{src.label}（d={src.distance}）｜sha256 {sha[:12]}"]
    ok: List[str] = []
    blocked: List[tuple] = []
    warned: List[tuple] = []
    beats: List[tuple] = []
    for n in names:
        st = (_lookup_status(ws, reg, cand, n) if reg is not None
              else _LookupStatus(_STATUS_UNREGISTERED, "未注册"))
        if st.code == _STATUS_WOULD_LOSE:
            blocked.append((n, st.text))
        elif st.code == _STATUS_BEATS_BUILTIN:
            # 顶掉内置也是「生效」，但它是**后果最重**的那种生效：
            # `read_file` / `run_shell` 这些是模型的感知通道，被顶掉就没了。
            beats.append((n, st.text))
        elif st.code == _STATUS_SAME_DISTANCE:
            warned.append((n, st.text))
        else:
            ok.append(n)

    if ok:
        lines.append("  ✓ 生效：" + ", ".join(ok))
    for n, st in beats:
        lines.append(f"  ⚠ 会顶掉内置：{n} ← {st}")
    for n, st in warned:
        lines.append(f"  ⚠ 会同距冲突：{n} ← {st}")
    for n, st in blocked:
        lines.append(f"  ✗ 未生效：{n} ← {st}")
    if beats:
        lines.append("     外部这份比内置近，所以内置先被顶掉 —— 这是本意吗？"
                     "顶掉内置会砍掉你自己的一条感知通道；要两全只能给其中一个改名。")
    if blocked:
        lines.append("     同名按距离裁决，近者生效。三条出路："
                     "给更近的那个改名 / 用 delete_function 删掉它 / 给本文件的工具换个名字。")
    if warned:
        lines.append("     同距时距离裁决不了：按加载顺序后加载者生效，同时会记一条 load_error。"
                     "趁早把其中一个改名。")
    # 静态审计里其余的 ⚠（lambda / 保留名 / 高危调用）同样适用于「外部登记」这条通道：
    # 登记即生效，事后没人再回头看它 —— 所以要把警告摆在它生效的这一刻。
    # 内置覆盖已由上面对「会顶掉内置」逐条报过，这里不重复。
    lines.extend("  " + n for n in _audit_notes(audit, shadows=not beats)
                 if n.startswith("⚠"))

    if not dry_run:
        added = sorted(after - before)
        removed = sorted(before - after)
        delta = f"已重新加载：+{len(added)}/-{len(removed)}，当前 {len(after)} 个工具"
        if added:
            delta += f"（新增 {', '.join(added)}）"
        lines.append(delta)
        if KERNEL.recorder is not None:
            KERNEL.recorder.record(
                "evolution",
                summary=f"登记外部 function: {cand.name}",
                data={"action": "register_function", "path": str(cand),
                      "sha256": sha, "source": src.label, "distance": src.distance,
                      "tools": names, "blocked": [n for n, _ in blocked],
                      "note": note},
            )
    else:
        lines.append("  预演结束：没有写登记簿。确认无误后把 dry_run 去掉再调一次。")
    return "\n".join(lines)


def unregister_function(path: str) -> str:
    """撤销一次 `register_function` 的授权 —— 只删登记项，**不删文件**。

    什么时候该调我：某个外部工具不再需要 / 已经过期 / 你要改用更近的那一份。
    文件可能还是别的项目共用的库，所以这里绝不动它；要删本项目自己的文件，
    用 `delete_function`。

    Args:
        path: 登记时用的那个绝对路径

    Returns:
        `[ok] 已撤销登记 …` 与随之失效的工具；没登记过则返回 `[错误] …` 并列出现有登记。
    """
    ws = KERNEL.workspace
    if ws is None:
        return "[错误] 内核未就绪"
    reg = KERNEL.registry
    raw = str(path or "").strip().strip('"').strip("'")
    if not raw:
        return "[错误] 需要登记时的那个绝对路径"
    cand = Path(raw).expanduser()
    if cand.is_absolute():
        try:
            cand = cand.resolve()
        except (OSError, RuntimeError):  # pragma: no cover
            pass
    key = canon_path(cand)

    meta = ws.read_functions_meta()
    regs = meta["registered"]
    keep: List[Dict[str, Any]] = []
    hit = None
    for rec in regs:
        try:
            same = canon_path(rec.get("path")) == key
        except (OSError, RuntimeError):  # pragma: no cover
            same = False
        if same:
            if hit is None:
                hit = rec
            continue        # 同一路径重复登记时**全部**摘掉：只删第一条的话，剩下的仍会被加载
        keep.append(rec)
    if hit is None:
        listing = "\n".join(f"  - {r.get('path')}" for r in regs) or "  （没有登记任何外部 function）"
        return f"[错误] 没登记过这个路径：{cand}\n当前登记：\n{listing}"

    meta["registered"] = keep
    ws.write_functions_meta(meta)

    gone = [t.name for t in (reg.user_tools() if reg is not None else [])
            if t.source_file and canon_path(t.source_file) == key]
    before = set(reg.tools) if reg is not None else set()
    if reg is not None:
        reg.reload()
    after = set(reg.tools) if reg is not None else set()
    removed = sorted(before - after)

    lines = [f"[ok] 已撤销登记：{cand}（文件没动）"]
    if gone:
        lines.append("  它带来的工具：" + ", ".join(gone))
    if removed:
        lines.append("  随之失效：" + ", ".join(removed))
    elif gone:
        lines.append("  这些工具仍可用 —— 说明还有更近的那一份在提供它们（同名按距离裁决）")
    src = ws.function_source_for(cand)
    # 「撤销登记后工具还在」有两种原因，必须分清：文件在**自动加载**的档里
    # （project d=0 / profile），或者还有更近的一份在提供同名工具。
    if src is not None and src.distance < USER_FUNCTION_DISTANCE:
        where = ("本项目的 functions 目录" if src.distance == 0
                 else f"{src.label} 这一档（命令行点名的 profile）")
        lines.append(f"  注意：它就在{where}里，会继续被自动加载 —— "
                     "撤销登记只删登记项，不删文件；要连文件一起删，用 delete_function。")
    lines.append(f"已重新加载，当前 {len(after)} 个工具。")
    if KERNEL.recorder is not None:
        KERNEL.recorder.record(
            "evolution", summary=f"撤销登记: {cand.name}",
            data={"action": "unregister_function", "path": str(cand),
                  "removed": removed},
        )
    return "\n".join(lines)


def list_functions() -> str:
    """列出当前所有可用工具及其来源文件。

    Returns:
        工具清单（内置 + 自造 + 加载异常）；失败返回 `[错误] …`。
    """
    reg = KERNEL.registry
    if reg is None:
        return "[错误] 内核未就绪"
    rows = ["# 当前工具清单"]
    builtin = [t for t in reg.tools.values() if t.origin == "builtin"]
    user = [t for t in reg.tools.values() if t.origin == "user"]
    rows.append(f"\n## 内置（{len(builtin)}）")
    for t in sorted(builtin, key=lambda x: x.name):
        rows.append(f"- `{t.signature_hint()}`")
    # 「自己写的」= 距离 0（本项目 functions 目录）：判据只有 distance，
    # `source_kind` 是给人看的标签，别拿它的字符串做相等比较。
    # 三桶仍然只比较数字：profile 档夹在 project（0）与 user（USER_FUNCTION_DISTANCE）之间。
    local = [t for t in user if t.source_distance == 0]
    from_profile = [t for t in user
                    if 0 < t.source_distance < USER_FUNCTION_DISTANCE]
    external = [t for t in user if t.source_distance >= USER_FUNCTION_DISTANCE]
    rows.append(f"\n## 自造（{len(local)}）—— 你的演化成果")
    if local:
        for t in sorted(local, key=lambda x: x.name):
            rows.append(f"- `{t.signature_hint()}`  ← {Path(t.source_file or '').name}")
    else:
        rows.append("- （还没有。当内置工具不够用时，用 write_function 长出新的）")
    if from_profile:
        rows.append(f"\n## 来自 profile（{len(from_profile)}）"
                    f"—— `-profile` 点名的目录，自动加载")
        for t in sorted(from_profile, key=lambda x: x.name):
            rows.append(f"- `{t.signature_hint()}`  ← {t.source_file}"
                        f"  [{t.source_kind}, d={t.source_distance}]")
    if external:
        rows.append(f"\n## 外部登记（{len(external)}）—— lookup_functions + register_function 拿来的")
        for t in sorted(external, key=lambda x: x.name):
            rows.append(f"- `{t.signature_hint()}`  ← {t.source_file}"
                        f"  [{t.source_kind}, d={t.source_distance}]")
    if reg.overrides:
        rows.append(f"\n## 同名覆盖（{len(reg.overrides)}）—— 按距离裁决，近者生效（正常现象）")
        for o in reg.overrides:
            win = Path(o.winner).name if o.winner != "内置" else "内置"
            lose = Path(o.loser).name if o.loser != "内置" else "内置"
            rows.append(f"- `{o.tool}`：{win} [{o.winner_kind}] 盖住 {lose} [{o.loser_kind}]")
    if reg.missing_registered:
        rows.append(f"\n## 已登记但文件不在了（{len(reg.missing_registered)}）—— 条目保留，没自动清")
        for p in reg.missing_registered:
            rows.append(f"- {p}")
    if reg.changed_registered:
        rows.append(f"\n## 已登记文件内容已变（{len(reg.changed_registered)}）"
                    f"—— 按引用加载，用的就是新内容")
        for c in reg.changed_registered:
            rows.append(f"- {c['path']}  {c['registered_sha256'][:12]} → {c['current_sha256'][:12]}")
    if reg.load_errors:
        rows.append(f"\n## 加载异常（{len(reg.load_errors)}）")
        for e in reg.load_errors:
            rows.append(f"- {Path(e.file).name}: {e.error}")
    return "\n".join(rows)


# `_lookup_status` 的状态码 —— 给**代码**判定用，别在给模型看的文案上做子串匹配。
# 反例：`会被更近的 X 盖住`（会输）与 `注册后会盖住 X`（会赢）都含「盖住」，
# 按子串判就会把「会赢」误判成「被覆盖」，于是 lookup 把一个更近、本会生效的工具
# 藏起来 —— 恰好抹掉这个功能存在的意义。
_STATUS_ACTIVE = "active"                # 已经生效（就是这份文件）
_STATUS_UNREGISTERED = "unregistered"    # 注册表里没人占这个名字
_STATUS_BEATS_BUILTIN = "beats_builtin"  # 注册后顶掉内置
_STATUS_WOULD_WIN = "would_win"          # 注册后盖住更远的那份
_STATUS_SAME_DISTANCE = "same_distance"  # 同距：距离裁决不了，按加载顺序
_STATUS_WOULD_LOSE = "would_lose"        # 注册后被更近的那份盖住


class _LookupStatus(NamedTuple):
    """一个（文件, 工具名）此刻的状态：`code` 给代码判，`text` 给人 / 模型看。"""

    code: str
    text: str
    winner: str = ""            # 当前占着这个名字的文件名（内置为「内置」）
    winner_distance: int = -1


def _lookup_status(ws: Any, reg: Any, path: Path, name: str,
                   dist_of: Optional[Callable[[Any], int]] = None) -> _LookupStatus:
    """一个（文件, 工具名）**此刻**的状态 —— 只查内存里的注册表，不执行任何代码。

    状态是「要不要 register」的唯一依据，所以要能分清五件事：
    已经生效了 / 注册后会盖住谁 / 注册后会被谁盖住 / 同距裁决不了 / 内置占着会被你顶掉。

    `dist_of` 让调用方把距离表算一次：`lookup_functions` 要对「每个文件 × 每个
    函数」都问一遍，而 `ws.function_distance` 每次调用都会重建整条来源表。
    """
    dist = dist_of or ws.function_distance
    win = reg.get(name) if reg is not None else None
    if win is None:
        return _LookupStatus(_STATUS_UNREGISTERED, "未注册")
    if win.source_file and canon_path(win.source_file) == canon_path(path):
        return _LookupStatus(_STATUS_ACTIVE, "已生效", Path(win.source_file).name)
    if win.origin == "builtin":
        return _LookupStatus(_STATUS_BEATS_BUILTIN, "未注册·注册后覆盖内置", "内置")
    d_new = dist(path)
    d_win = dist(win.source_file) if win.source_file else 10 ** 9
    winner = Path(win.source_file).name if win.source_file else "内置"
    shown = f"{winner} (d={d_win})"
    if d_win < d_new:
        return _LookupStatus(_STATUS_WOULD_LOSE, f"未注册·会被更近的 {shown} 盖住",
                             winner, d_win)
    if d_win == d_new:
        # 与 registry._note_conflict 同一口径：同距不是「谁更近」，而是裁决不了。
        return _LookupStatus(
            _STATUS_SAME_DISTANCE,
            f"未注册·与 {shown} 距离相同 —— 距离裁决不了，按加载顺序（后加载者生效）",
            winner, d_win)
    return _LookupStatus(_STATUS_WOULD_WIN, f"未注册·注册后会盖住 {shown}",
                         winner, d_win)


def lookup_functions(query: str = "", include_shadowed: bool = True,
                     limit: int = 20) -> str:
    """内置工具不够用时，先看看周围有什么现成 function —— **纯发现：不注册、不执行、不写盘**。

    它沿项目逐级向上找 `<dir>/.openthree/functions`，再加上 `~/.openthree/functions`，
    用 ast 静态解析出每个文件的工具名 / 签名 / docstring 首行，让你判断「这个工具
    我要不要用」——**而不是**替你把代码跑起来。有没有语法错、会不会被别人盖住，
    也在这里一次看清。

    为什么需要它：内核只自动注册**本项目**的 function。上级目录、用户目录里写好的
    工具一直存在，但你看不见，于是很容易重写一个本来已经有的东西。发现之后，
    用 `register_function('<绝对路径>')` 才真正拿到那个能力。

    发现与授权是两件事：本工具**永不留痕**（不写 functions.json、不改
    sys.modules、不 import 任何文件），所以可以反复调、随便调。

    Args:
        query: 只显示文件名 / 工具名 / docstring 首行包含它的（不区分大小写，空 = 全部）
        include_shadowed: 是否显示「会被更近的同名工具盖住」的文件（默认显示 —— 那正是判断依据）
        limit: 每个来源最多列几个文件（默认 20；超出的会告诉你还有多少个没显示）

    Returns:
        来源清单（由近到远，带绝对路径与每个工具的状态）；失败返回 `[错误] …`。
    """
    ws = KERNEL.workspace
    if ws is None:
        return "[错误] 内核未就绪"
    reg = KERNEL.registry
    q = (query or "").strip().lower()
    cap = max(1, int(limit or 20))

    # 距离表只算一次：`ws.function_distance` 每次调用都会重建整条来源表，
    # 而下面要对「每个文件 × 每个函数」都问一遍状态 —— 不缓存就是 O(n·m·depth) 次系统调用。
    all_sources = ws.function_sources(include_missing=True)
    dist_by_dir = {canon_path(s.path): s.distance for s in all_sources}

    def dist_of(p: Any) -> int:
        return dist_by_dir.get(canon_path(Path(p).parent), REGISTERED_FUNCTION_DISTANCE)

    rows: List[str] = ["# function 来源（由近到远｜同名时：近者生效）"]
    found_any = False
    hidden_total = 0

    for src in [s for s in all_sources if s.path.is_dir()]:
        try:
            files = [p for p in sorted(src.path.glob("*.py")) if not p.name.startswith("_")]
        except OSError:  # pragma: no cover - 权限异常
            files = []
        blocks: List[str] = []
        for path in files:
            rep = _audit_module(path)
            fns = rep.get("functions") or []
            hay = " ".join([path.stem] + [f["name"] for f in fns]
                           + [f.get("doc_first", "") for f in fns]).lower()
            if q and q not in hay:
                continue

            statuses = [(f, _lookup_status(ws, reg, path, f["name"], dist_of=dist_of))
                        for f in fns]
            # 只有「会被更近的盖住」才是真的没戏；「会同距」和「会盖住别人」都还能生效。
            shadowed = bool(statuses) and all(st.code == _STATUS_WOULD_LOSE
                                              for _, st in statuses)
            if shadowed and not include_shadowed:
                continue

            if rep.get("read_error"):
                level = f"读不了：{rep['read_error']}"
            elif rep.get("syntax_error"):
                level = f"解析失败：{rep['syntax_error']}"
            elif not fns:
                level = "无公开函数（会静默不生效）"
            elif any(st.code == _STATUS_ACTIVE for _, st in statuses):
                level = "已注册"
            elif shadowed:
                level = "被覆盖（注册了也不会生效）"
            else:
                level = "可注册"

            line = [f"- [{level}] {path.name}  →  {path}"]
            for f, st in statuses:
                doc = f.get("doc_first") or "（无 docstring）"
                line.append(f"    - `{f['signature']}` — {doc}    ｜ {st.text}")
            blocks.append("\n".join(line))

        if not blocks:
            continue
        found_any = True
        shown = blocks[:cap]
        hidden_total += max(0, len(blocks) - cap)
        rows.append(f"\n## {src.label} · d={src.distance} · {src.path}")
        rows.extend(shown)
        if len(blocks) > cap:
            rows.append(f"- …… 还有 {len(blocks) - cap} 个文件没显示（调小 query 或加大 limit）")

    if not found_any:
        rows.append("\n（搜索范围内没有匹配的 function 文件）")

    searched = "\n".join(f"- {s.label}：{s.path}"
                         + ("" if s.path.is_dir() else "（不存在）")
                         for s in ws.function_sources(include_missing=True))
    rows.append("\n## 搜索范围（由近到远）\n" + searched)
    if hidden_total:
        rows.append(f"\n（另有 {hidden_total} 个文件因 limit 未显示）")
    rows.append("\n注册用 `register_function('<上面那条绝对路径>')`：它会先算给你看"
                "哪些工具会生效、哪些会被更近的同名工具盖住；都不合用，才用 "
                "`write_function` 自己造。")
    return "\n".join(rows)


def _ast_signature(node: Any) -> str:
    """把一个 `def` 的形参表还原成 `f(a, b=)` 这样一行（只读 ast，不执行代码）。

    默认值只标「有/无」（`b=`），不展开表达式：`lookup` 要的是「这个工具怎么调」，
    不是把源码里的默认值原样抄一遍 —— 抄了反而会看错成「默认值就是这串字面量」。
    """
    args = getattr(node, "args", None)
    if args is None:  # pragma: no cover - 防御：非 def 节点
        return getattr(node, "name", "")
    names: List[str] = []
    posonly = list(getattr(args, "posonlyargs", []))
    pos = posonly + list(args.args)
    defaults = list(args.defaults)
    first_default = len(pos) - len(defaults)
    for i, a in enumerate(pos):
        if a.arg in ("self", "cls"):
            continue
        names.append(a.arg if i < first_default else f"{a.arg}=")
        # `/` 必须还原：它是「仅位置」的分界，漏了就让人以为能用关键字传。
        if posonly and i == len(posonly) - 1 and i < len(pos) - 1:
            names.append("/")
    if args.vararg:
        names.append("*" + args.vararg.arg)
    elif args.kwonlyargs:
        names.append("*")
    for a in args.kwonlyargs:
        names.append(f"{a.arg}=")
    if args.kwarg:
        names.append("**" + args.kwarg.arg)
    return f"{getattr(node, 'name', '?')}({', '.join(names)})"


def _audit_module(path: Path) -> Dict[str, Any]:
    """对一个 function 文件做静态体检，返回各项判据的原始结果（不执行代码）。

    解码走 `read_source`（PEP 263 编码声明），与注册器**同一套规则**：中文 Windows
    上手写文件常带 `# -*- coding: gbk -*-`，硬按 UTF-8 读会让同一个文件
    「能被 import / 能被加载」却「被判成解析失败或无公开函数」—— 两个判据各说一套，
    `lookup_functions` 就会把一个真能用的文件藏起来。读不了（编码声明与内容矛盾、
    权限、文件消失）时接连口都没有，如实记进 `read_error` 而不是拿乱码硬解析。
    """
    rep: Dict[str, Any] = {
        "syntax_error": "", "read_error": "", "module_doc": "", "functions": [],
        "private_helpers": 0, "side_effects": [], "has_main_guard": False,
        "has_entry": False, "leaked": [],
    }
    try:
        code = read_source(path)
    except (OSError, ValueError, SyntaxError) as exc:
        rep["read_error"] = f"{type(exc).__name__}: {exc}"
        return rep
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        rep["syntax_error"] = f"{exc.msg} (line {exc.lineno})"
        return rep

    doc = ast.get_docstring(tree) or ""
    rep["module_doc"] = doc.strip().splitlines()[0].strip() if doc.strip() else ""
    rep["has_main_guard"] = _has_main_guard(tree)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "__main__":
                rep["has_entry"] = True
            elif node.name.startswith("_"):
                rep["private_helpers"] += 1
            else:
                fn_doc = ast.get_docstring(node) or ""
                params = [a for a in node.args.args if a.arg not in ("self", "cls")]
                rep["functions"].append({
                    "name": node.name,
                    "signature": _ast_signature(node),
                    "doc_first": fn_doc.strip().splitlines()[0].strip() if fn_doc.strip() else "",
                    "params": len(params),
                    "annotated": all(a.annotation is not None for a in params),
                    "return_annotated": node.returns is not None,
                    "has_args": "Args:" in fn_doc,
                    "has_returns": "Returns:" in fn_doc,
                })
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for tgt in targets:
                if (isinstance(tgt, ast.Name) and isinstance(node.value, ast.Lambda)
                        and not tgt.id.startswith("_")):
                    rep["leaked"].append(tgt.id)
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            # 模块级裸调用（print / mkdir / 建连接）：reload 会 exec_module，等于每次加载都跑一遍
            rep["side_effects"].append(f"`{_call_label(node.value.func) or '?'}()` 第 {node.lineno} 行")
    return rep


def _function_issues(rep: Dict[str, Any]) -> List[str]:
    """把一份 `_audit_module()` 结果翻译成「规范化清单」的问题列表。

    这是 function 规范化的**唯一判据**：`audit_functions`（给人看的报告）与
    升级扫描（`upgrade.scan_upgrade` 判 nonconforming）都从这里取，不各写一份。
    本项目在「function 注册判据」上有过 4 份拷贝、其中 1 份与注册器不一致的教训。

    Args:
        rep: `_audit_module()` 的原始结果

    Returns:
        问题描述清单；空列表 = 这条文件合规。
    """
    issues: List[str] = []

    if rep.get("read_error"):
        issues.append(f"读不了这个文件（{rep['read_error']}）—— 编码声明与内容矛盾 / 权限 / 已删除")
    if rep["syntax_error"]:
        issues.append(f"语法错误：{rep['syntax_error']}")
    if not rep["module_doc"]:
        issues.append("缺模块 docstring 首行（一句话说明这个文件干什么）")
    if not rep["functions"]:
        issues.append("没有可注册的公开函数（公开 = 不以 `_` 开头的顶层 def）")
    for fn in rep["functions"]:
        if not fn["annotated"]:
            issues.append(f"`{fn['name']}`：有参数没写类型注解 → 工具 schema 与 CLI 都拿不到类型")
        if not fn["return_annotated"]:
            issues.append(f"`{fn['name']}`：缺返回类型注解")
        if fn["params"] and not fn["has_args"]:
            issues.append(f"`{fn['name']}`：docstring 缺 `Args:` 段 → 参数在 --help 里没有说明")
        if not fn["has_returns"]:
            issues.append(f"`{fn['name']}`：docstring 缺 `Returns:` 段 → --help 里看不到它返回什么")
        if fn["name"] in _RESERVED_TOOL_NAMES:
            issues.append(f"`{fn['name']}`：这是命令行入口的保留名，会与 --help 语义混淆，建议改名")
    if rep["side_effects"]:
        issues.append("模块级有副作用（每次加载都会执行）：" + "、".join(rep["side_effects"]))
    if rep["leaked"]:
        issues.append("这些公开名绑定的是 lambda（会注册成 `<lambda>` 工具，多个互相覆盖）："
                      + ", ".join(rep["leaked"]) + "；请改成 `def` 或加 `_` 前缀")
    if rep["functions"] and not rep["has_main_guard"]:
        issues.append('没有命令行尾钩（`if __name__ == "__main__":`）→ 只能靠 '
                      '`python -m openthree.tools --file` 调')
    if rep["has_main_guard"] and not rep["has_entry"]:
        issues.append("有 `__main__` 保护但没有 `def __main__(argv=None)` 入口函数")
    return issues


def function_conformance(path: Path) -> Dict[str, Any]:
    """体检**单个** function 文件（纯静态：不执行代码、不起子进程跑 --help）。

    升级扫描需要逐文件判「这个自造 function 还合不合当前规范」，而它不该
    import `_audit_module` 这种私有名 —— 私有名一旦被外部依赖，就再也没法自由
    重构。所以这里给一个**公开**入口，判据仍只有 `_function_issues` 一份。

    Args:
        path: function 文件路径

    Returns:
        `_audit_module()` 的全部字段，外加 `issues`（问题清单，空 = 合规）；
        文件读不了时 `issues` 里带一条说明。
    """
    target = Path(path)
    try:
        rep = _audit_module(target)
    except OSError as exc:
        return {"syntax_error": "", "module_doc": "", "functions": [],
                "private_helpers": 0, "side_effects": [], "has_main_guard": False,
                "has_entry": False, "leaked": [],
                "issues": [f"读不了这个文件：{exc}"]}
    rep = dict(rep)
    rep["issues"] = _function_issues(rep)
    return rep


def audit_functions(run_help: bool = True) -> str:
    """体检 .openthree/functions/ 下每个文件的「规范化清单」，并真跑一次 --help。

    检查的是「这个文件是不是一个合格的、人能独立运行的 function 文件」：
    模块 docstring、每个公开函数的类型注解与 `Args:`/`Returns:` 段、
    模块级有无副作用、有没有命令行尾钩、入口名是否越界（不许叫 main/cli）。
    最后一条只有它敢承诺：**在子进程里真的跑一次 `python 本文件.py --help`**，
    断言退出码为 0 且输出里列出每一个公开函数 —— 静态看不出的问题在这里会露出来。

    写新 function 之后跑它，比事后一个个试要快。已知它是「检查」不是「修复」：
    报告里的每一条都得靠 `write_function` 或 `replace_in_file` 去改。

    Args:
        run_help: 是否真的在子进程里跑一次 --help（默认 True；慢一点但更可信）
    """
    ws = KERNEL.workspace
    if ws is None:
        return "[错误] 内核未就绪"
    files = ws.list_function_files()
    if not files:
        return "[ok] .openthree/functions/ 下还没有 function 文件。"

    rows: List[str] = ["# function 规范化体检"]
    bad_files = 0
    total_issues = 0

    for path in files:
        rep = _audit_module(path)
        rel = _rel_to(ws.root, path)
        issues: List[str] = _function_issues(rep)

        help_note = ""
        if run_help and rep["has_main_guard"] and not rep["syntax_error"]:
            env = dict(os.environ)
            prev = env.get("PYTHONPATH", "")
            pkg_parent = str(Path(__file__).resolve().parents[1])
            env["PYTHONPATH"] = os.pathsep.join(
                [x for x in (str(ws.root), pkg_parent, prev) if x]
            )
            code, out, err = _run_capture(
                [sys.executable, str(path), "--help"],
                work=str(ws.root), timeout=60, env=env,
            )
            if code is None:
                issues.append(f"`--help` 进程起不来：{(out or err).strip()[:300]}")
            elif code != 0:
                issues.append(f"`--help` 退出码 {code}（应为 0）：{(err or out).strip()[:300]}")
            else:
                missing = [f["name"] for f in rep["functions"] if f["name"] not in out]
                if missing:
                    issues.append("`--help` 里没有列出这些公开函数：" + ", ".join(missing))
                else:
                    help_note = f"`--help` 正常，列出 {len(rep['functions'])} 个子命令"

        total_issues += len(issues)
        if issues:
            bad_files += 1
            rows.append(f"\n## ⚠ {rel}（{len(issues)} 项待改）")
            rows.extend(f"- {i}" for i in issues)
            if help_note:
                rows.append(f"- ✅ {help_note}")
        else:
            rows.append(f"\n## ✅ {rel}")
            rows.append(f"- {len(rep['functions'])} 个公开函数，"
                        f"{rep['private_helpers']} 个私有辅助；规范化清单 6 条全过")
            if help_note:
                rows.append(f"- ✅ {help_note}")

    head = (f"\n共 {len(files)} 个文件：{len(files) - bad_files} 个合格，"
            f"{bad_files} 个待改，{total_issues} 项问题。")
    return "\n".join(rows) + head


# ---------------------------------------------------------------- 改提示词


def read_prompts() -> str:
    """读取当前的系统提示词与用户提示词。

    开头先给出系统提示词自带的**章节索引** —— 自省时先看结构一眼看清全貌，
    要用编辑器式操作改哪一段再决定；全文紧随其后。

    Returns:
        提示词全文（含章节索引）；失败返回 `[错误] …`。
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

    Returns:
        `[ok] 系统提示词已更新 …`；失败返回 `[错误] …`。
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

    Returns:
        `[ok] 任务已更新 …`；失败返回 `[错误] …`。
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
    """读取你自己的计划（`.openthree/prompts/plan.md`）—— 长任务的待办清单。

    Returns:
        计划全文与未完成条目数；失败返回 `[错误] …`。
    """
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

    Returns:
        `[ok] 计划已更新 …`；失败返回 `[错误] …`。
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
    """列出系统提示词的历史版本——写坏了可以回退（自省的前提是可后悔）。

    Returns:
        历史版本清单；失败返回 `[错误] …`。
    """
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

    Returns:
        指定版本的全文或概览；失败返回 `[错误] …`。
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

    Returns:
        `[ok] 系统提示词已回退到 …`；失败返回 `[错误] …`。
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


# ---------------------------------------------------------------- 身份


_NAME_MAX = 24
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _clean_name(name: str) -> tuple[str, str]:
    """清洗一个名字，返回 (清洗后的名字, 错误说明)。"""
    raw = (name or "").strip()
    if not raw:
        return "", "名字不能为空"
    if _CONTROL_CHARS.search(raw):
        return "", "名字里不能有控制字符 / 换行"
    if len(raw) > _NAME_MAX:
        return "", f"名字太长（{len(raw)} 字符，上限 {_NAME_MAX}）"
    return raw, ""


def set_identity(name: str, motto: str = "") -> str:
    """给自己起名 / 改名。

    名字不是内核常量，而是你自己长出来的：写进 `.openthree/identity.json`，
    与 `limits.json` 同源（磁盘即状态、每步被重读），所以**下一步它就出现在你的
    处境里**，也出现在终端身份卡与工作报告上。每次改名都记一条演化事件，
    于是「名字的演化史」与「工具的演化史」并列。

    Args:
        name: 新名字（1~24 字符；去首尾空白，禁控制字符与换行）
        motto: 可选的自我格言（一句话，会显示在身份卡上）

    Returns:
        `[ok] …`，说明新名字与下一步何时生效；失败返回 `[错误] …`。
    """
    ws = KERNEL.workspace
    if ws is None:
        return "[错误] 内核未就绪"
    clean, err = _clean_name(name)
    if err:
        return f"[错误] {err}"
    motto_clean = (motto or "").strip()
    if _CONTROL_CHARS.search(motto_clean):
        return "[错误] 格言里不能有控制字符 / 换行"
    old = ws.read_identity()
    if old.get("name") == clean and (old.get("motto") or "") == motto_clean:
        return f"[ok] 你仍然叫 {clean}（名字没变）。"
    doc = ws.write_identity(clean, motto_clean)
    verb = "给自己起了名字" if not old.get("name") else "改名"
    summary = f"{verb}：{clean}" + (f" —— {motto_clean}" if motto_clean else "")
    if KERNEL.recorder is not None:
        KERNEL.recorder.record(
            "evolution",
            summary=summary,
            data={"action": "set_identity", "name": clean, "motto": motto_clean,
                  "previous": old.get("name"), "renames": doc.get("renames", 0)},
        )
    extra = "" if not old.get("name") else f"（原叫 {old.get('name')}）"
    return (f"[ok] {verb}：{clean}{extra}{' —— ' + motto_clean if motto_clean else ''}；"
            "下一步起它会出现在你的处境、终端身份卡与报告里。")


def _kernel_card(ws) -> list:
    """「我跑在哪套内核上」那几行 —— 内核版本、结构 schema、工作区完整性。

    这三件事都属于「我是谁」，所以不另立工具：`whoami` 一条答全。
    名字以 `_` 开头，所以它只是辅助函数，不会多出一个工具（工具清单不变重）。

    老工作区没有 `workspace.json`（`read_workspace_meta()` 返回 `{}`）时，
    如实写「无版本戳」，绝不把 0 当 schema 报出去 —— 后者会让人以为结构
    停在第 0 版，而真相是「这版内核还没管过它」。
    """
    from .workspace import WORKSPACE_SCHEMA, kernel_version

    kver = kernel_version() or "（未知）"
    meta = ws.read_workspace_meta() or {}
    if meta:
        wver = str(meta.get("kernel_version") or "（未知）")
        schema = meta.get("schema")
        lines = [f"内核：{kver}（本工作区由 {wver} 铺出；结构 schema "
                 f"{schema if schema is not None else '（未知）'}/{WORKSPACE_SCHEMA}）"]
    else:
        lines = [f"内核：{kver}（本工作区**无版本戳** —— 它早于版本戳机制；"
                 "想知道差什么，跑 `openthree -u --check`）"]

    entries = ws.required_entries()
    missing = [e.path for e in entries if not (ws.dir / e.path).exists()]
    if missing:
        head = "、".join(missing[:6]) + ("…" if len(missing) > 6 else "")
        lines.append(f"工作区：缺 {len(missing)}/{len(entries)} 项 —— {head}")
    else:
        lines.append(f"工作区：{len(entries)}/{len(entries)} 项齐备"
                     "（内容是否过期由 `openthree -u --check` 判）")
    return lines


def whoami() -> str:
    """看一眼自己是谁：内核 / schema / 名字 / 格言。

    还没有名字时如实说「还没有」，并提示用 `set_identity` 起一个。

    Returns:
        身份卡文本（内核 / schema / 名字 / 格言）；失败返回 `[错误] …`。
    """
    ws = KERNEL.workspace
    if ws is None:
        return "[错误] 内核未就绪"
    lines = _kernel_card(ws)
    identity = ws.read_identity()
    name = identity.get("name")
    if not name:
        lines.append("名字：还没有名字 —— 想清楚自己是谁时，用 "
                     "set_identity(name, motto=…) 给自己起一个。")
        return "\n".join(lines)
    lines.append(f"名字：{name}")
    if identity.get("motto"):
        lines.append(f"格言：{identity['motto']}")
    lines.append(f"起名于：{identity.get('born_at') or '（未知）'}"
                 f"；改名 {int(identity.get('renames') or 0)} 次")
    lines.append(f"身份档：{ws.identity_file}")
    return "\n".join(lines)


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

    Returns:
        `[ok] 已记录。`
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

    Returns:
        `[ok] 已记住。`；失败返回 `[错误] …`。
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

    Returns:
        记忆条目清单（无命中 / 空时给出说明）；失败返回 `[错误] …`。
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

    Returns:
        折叠结果（省下多少字符）；失败返回 `[错误] …`。
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

    Returns:
        用量读数（已用 / 预算 / 占比 / 可折范围 / 折叠次数）；失败返回 `[错误] …`。
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

    Returns:
        策略变更结果；失败返回 `[错误] …`。
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

    Returns:
        边界读数（各旋钮的当前值与来源）；失败返回 `[错误] …`。
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

    Returns:
        边界变更结果；失败返回 `[错误] …`。
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

    - 只要本场**还有轮预算**（处境里写「这一轮收尾后还有下一轮」）：本消息收尾后，
      运行期会新开一轮，并把 `summary` 当作交接说明交给你下一轮 —— 单次任务与
      自动接力都如此。这正是「长任务分轮推进」的正规出口。
    - 轮预算用尽时（处境里没有那句「还有下一轮」）：它只是把发言权交还，运行期
      如实告诉你没有下一轮。若还想继续，用 `set_limits(max_rounds=…)` 调大轮预算
      （若没被启动参数锁定）。

    Args:
        summary: 给「下一轮的你」的交接说明（已完成什么、还剩什么、下一步做什么）。

    Returns:
        本轮收尾说明（轮预算与下一轮安排）；失败返回 `[错误] …`。
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
        carried = "已把你的交接说明交给下一轮（它会出现在下一轮的开头）" if summary.strip() \
            else "（这次没写交接说明，下一轮从头接着做）"
        return (f"[ok] 已结束第 {r['index']} 轮（本场最多 {r['max']} 轮，还剩 {r['remaining']} 轮）。"
                f"运行期将开下一轮；{carried}。")
    return (f"[ok] 已结束第 {r['index']} 轮 —— 轮预算（{r['max']} 轮）已用尽，**没有下一轮**。"
            "若事情没做完，请在 finish 里写清「还剩什么」；"
            "还想再跑几轮可用 set_limits(max_rounds=…) 调大轮预算（未被锁定的话）。")


def finish(answer: str) -> str:
    """任务完成，给出最终结论并结束循环。

    Args:
        answer: 面向用户的最终答复

    Returns:
        `[ok] 已提交最终答复。`
    """
    if KERNEL.agent is not None:
        KERNEL.agent.finish(answer)
    if KERNEL.recorder is not None:
        KERNEL.recorder.record("finish", summary=answer[:200], data={"answer": answer})
    return "[ok] 已提交最终答复。"


def get_time() -> str:
    """获取当前时间。

    Returns:
        `YYYY-MM-DD HH:MM:SS` 格式的当前时间。
    """
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


# 这些名字是 function 文件的「命令行入口」保留名。定义成同名公开函数，
# 会在 CLI 里与入口抢位置，且名字本身不表达「这是个工具」，容易让人误以为是入口。
_RESERVED_TOOL_NAMES = frozenset({"main", "cli", "run_cli"})

# 「高危调用」清单：只是**提醒**（很多合法的自造工具确实要起子进程），
# 但值得让模型知道「你写的这行会动到系统」——写出来时就说清，胜过事后排查。
_DANGEROUS_CALLS = frozenset({
    "eval", "exec", "compile", "__import__",
    "os.system", "os.popen", "os.remove", "os.unlink", "os.rmdir", "os.kill",
    "os._exit", "shutil.rmtree", "subprocess.run", "subprocess.Popen",
    "subprocess.call", "subprocess.check_output", "socket.socket", "ctypes.CDLL",
})


def _has_main_guard(tree: ast.AST) -> bool:
    """源码里有没有 ``if __name__ == "__main__":`` 这个标准尾钩。

    只认最朴素的两种写法（``__name__ == "__main__"`` 与反向），
    以及 ``__name__ in ("__main__",)`` —— 判据太宽会把「随便比较一下」
    当成尾钩，反而让体检失真。
    """
    for node in getattr(tree, "body", []):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not isinstance(test, ast.Compare) or len(test.ops) != 1:
            continue
        left, op, right = test.left, test.ops[0], test.comparators[0]

        def _is_name(n: ast.AST) -> bool:
            return isinstance(n, ast.Name) and n.id == "__name__"

        if isinstance(op, ast.Eq):
            pair = (left, right)
            if any(_is_name(a) for a in pair):
                other = right if _is_name(left) else left
                if isinstance(other, ast.Constant) and other.value == "__main__":
                    return True
        elif isinstance(op, ast.In) and _is_name(left):
            if isinstance(right, (ast.Tuple, ast.List)):
                for elt in right.elts:
                    if isinstance(elt, ast.Constant) and elt.value == "__main__":
                        return True
    return False


def _static_audit(code: str, builtin_names: Optional[set] = None) -> Dict[str, Any]:
    """对一份待落盘的 function 源码做**纯静态**体检（只解析、不执行）。

    返回 dict：``syntax_error``（语法错误）/ ``functions``（会被注册的公开
    def 函数名）/ ``all_functions``（全部顶层 def）/ ``leaked``（公开名被绑成
    lambda 的，会被注册成 ``<lambda>`` 工具）/ ``reserved``（用了 CLI 保留名
    的公开函数）/ ``shadows``（覆盖了同名内置工具）/ ``dangerous``（高危调用）。

    注册器最终会 ``exec_module``、再按 ``is_registrable`` 收函数。静态审计
    只看得到语法，所以对「公开名绑定 lambda」这类**语法合法但不该发生**的写法，
    这里显式报出来 —— 它们真会被注册，工具名还会变成 ``<lambda>``。
    """
    result: Dict[str, Any] = {
        "syntax_error": "", "functions": [], "all_functions": [],
        "leaked": [], "reserved": [], "shadows": [], "dangerous": [],
        "has_main_guard": False,
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
    leaked: List[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            all_names.append(node.name)
            if not node.name.startswith("_"):
                public.append(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            for tgt in targets:
                if (isinstance(tgt, ast.Name) and isinstance(value, ast.Lambda)
                        and not tgt.id.startswith("_")):
                    leaked.append(tgt.id)
    result["functions"] = public
    result["all_functions"] = all_names
    result["leaked"] = leaked
    result["reserved"] = sorted(n for n in public if n in _RESERVED_TOOL_NAMES)
    result["has_main_guard"] = _has_main_guard(tree)
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


def _audit_notes(audit: Dict[str, Any], shadows: bool = True) -> List[str]:
    """把静态审计结果翻译成给模型看的提示行（没有问题时返回空列表）。

    Args:
        audit: `_static_audit()` 的结果
        shadows: 是否报「覆盖内置工具」。调用方若已经逐条报过「会顶掉哪个内置」
            （`register_function` 用 `_lookup_status` 报得更具体），传 False 免得重复。
    """
    notes: List[str] = []
    if audit.get("functions"):
        notes.append(f"可注册工具：{', '.join(audit['functions'])}")
    else:
        warn = _public_function_warning_from_audit(audit)
        if warn:
            notes.append("⚠ " + warn)
    if audit.get("leaked"):
        notes.append("⚠ 这些公开名绑定的是 lambda：" + ", ".join(audit["leaked"])
                     + "。注册器会把它注册成工具，但工具名会变成 `<lambda>`，"
                     "多个这样的名字还会互相覆盖。请改成 `def`，或加上 `_` 前缀。")
    if audit.get("reserved"):
        notes.append("⚠ 这些公开函数用了命令行入口的保留名：" + ", ".join(audit["reserved"])
                     + "。它们会被注册成工具，但也会与 `python 文件.py --help` 的"
                     "入口语义混淆。建议改成能表达用途的名字。")
    if audit.get("functions") and not audit.get("has_main_guard"):
        notes.append("提示：这个文件还没有命令行尾钩，人/其他脚本只能通过 "
                     "`python -m openthree.tools --file <本文件> <函数> ...` 调用它。"
                     "想让 `python 本文件.py --help` 直接可用，在文件末尾加上：\n"
                     "    def __main__(argv=None) -> int:\n"
                     "        \"\"\"命令行入口。\"\"\"\n"
                     "        from openthree.cli_tool import run_cli\n"
                     "        return run_cli(globals(), argv=argv)\n"
                     "\n"
                     "\n"
                     "    if __name__ == \"__main__\":\n"
                     "        raise SystemExit(__main__())\n"
                     "（入口名必须是 `__main__`：dunder 不会被注册成工具，"
                     "叫 main/cli 会凭空多出一个空参数工具。）")
    if shadows and audit.get("shadows"):
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
        from openthree.tool import build_tool, is_registrable
    except BaseException as e:
        rep["warnings"].append(f"无法 import openthree.tool（{type(e).__name__}: {e}）")
        build_tool = None
        is_registrable = None
    import inspect
    for nm in dir(mod):
        if nm.startswith("_"):
            continue
        obj = getattr(mod, nm)
        if isinstance(obj, type):
            rep["classes"].append(nm)
            continue
        # 与注册器共用同一判据，验收结果才对得上 reload 之后真正拿到的东西。
        if is_registrable is not None:
            if not is_registrable(mod.__name__, nm, obj):
                continue
        else:
            # 退化路径：openthree.tool 都 import 不到时，验收本身已不可信，
            # 这里只做最基本的过滤，并已在 warnings 里说明原因。
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
    "lookup_functions": lookup_functions,
    "register_function": register_function,
    "unregister_function": unregister_function,
    "list_functions": list_functions,
    "audit_functions": audit_functions,
    "read_prompts": read_prompts,
    "write_system_prompt": write_system_prompt,
    "write_user_prompt": write_user_prompt,
    "read_plan": read_plan,
    "write_plan": write_plan,
    "list_prompt_versions": list_prompt_versions,
    "read_prompt_version": read_prompt_version,
    "restore_system_prompt": restore_system_prompt,
    "set_identity": set_identity,
    "whoami": whoami,
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


def __main__(argv=None) -> int:
    """命令行入口：`python -m openthree.builtins --help` 看全部内置函数。

    这个模块在 package 内，顶层有相对导入（``from .kernel import KERNEL``），
    所以**只能用 `-m` 运行**；`python openthree/builtins.py` 会在到达本函数之前
    就因「no known parent package」失败。用户函数（``.openthree/functions/*.py``）
    才用 `python xxx.py` 直接跑。

    命名成 dunder 是刻意的：注册器与 CLI 共用同一条判据，dunder 永不注册成工具；
    这个函数的参数表里也没有 ``kernel``，所以它既不是工具、也不会污染工具集。
    """
    from .cli_tool import run_cli

    return run_cli(globals(), argv=argv)


if __name__ == "__main__":
    raise SystemExit(__main__())
