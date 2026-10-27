"""把一个 function 文件变成命令行程序。

判据：CLI 的参数表由 ``build_tool`` 从「签名 + docstring」推导 —— 与模型看到的
工具完全同源，因此不会出现「模型 7 个参数、命令行只有 5 个」的发散。

两种用法：

    # 文件自带尾钩（推荐，每个 function 文件都应如此）
    python .openthree/functions/safe_edit.py --help
    python .openthree/functions/safe_edit.py replace_in_file --path a.py --old x --new y

    # 保底通道：不用碰任何文件，连没加尾钩的存量文件也能跑
    python -m openthree.tools read_file --path README.md --limit 20
    python -m openthree.tools --file .openthree/functions/safe_edit.py --list

退出码：0 成功 / 1 函数返回 ``[错误] …`` / 2 抛异常。
输出编码恒为 UTF-8（Windows 控制台默认 GBK，会让外部 agent 解码失败）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------- 输出编码
def force_utf8() -> None:
    """把 stdout/stderr 切到 UTF-8。

    Windows 控制台默认 GBK，管道里吐的是 GBK 字节；外部 agent 按 UTF-8 解会
    直接 UnicodeDecodeError。``PYTHONIOENCODING`` 在这里改不动 —— Python 层
    已初始化，所以必须进程内 reconfigure。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- 参数取值
def _read_text_arg(value: str) -> str:
    """字符串参数的 ``@file`` / ``-`` 约定：从文件或 stdin 读长文本。

    ``@@x`` 是转义，表示字面量 ``@x``。
    """
    if value == "-":
        return sys.stdin.read()
    if value.startswith("@@"):
        return value[1:]
    if value.startswith("@"):
        path = value[1:]
        try:
            return Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"无法读取 {path}：{exc}") from exc
    return value


# ---------------------------------------------------------------- 离线 Kernel
def resolve_project_dir(explicit: Optional[str] = None) -> Tuple[Path, bool]:
    """定位项目目录；返回 (目录, 该目录下是否已有 .openthree)。

    显式 ``--project-dir`` 优先；否则从 cwd 向上找最近的含 ``.openthree/`` 的
    目录；都没有就用 cwd。
    """
    if explicit:
        root = Path(explicit).expanduser().resolve()
        return root, (root / ".openthree").is_dir()
    cur = Path.cwd().resolve()
    for cand in (cur, *cur.parents):
        if (cand / ".openthree").is_dir():
            return cand, True
    return cur, False


def setup_offline_kernel(project_dir: Optional[str] = None) -> Any:
    """给 CLI 准备一个「离线内核」：有 workspace / registry，没有 recorder / agent。

    ``recorder=None`` 与 ``agent=None`` 是既有设计（kernel.py），所以
    ``finish`` / ``context_*`` / ``limits_*`` 在这里会如实返回「未就绪」——
    那是正确行为，不是坏了。

    只在项目目录**已有** ``.openthree/`` 时调用 ``init()``：
    否则``read_file`` 这类只读命令会在人随便一个目录里凭空建出工作区。
    """
    from .builtins import BUILTIN_FUNCTIONS
    from .kernel import KERNEL
    from .registry import Registry
    from .workspace import Workspace

    root, has_ws = resolve_project_dir(project_dir)
    ws = Workspace(root)
    if has_ws:
        ws.init()                      # 幂等：已存在的内容不覆盖

    reg = Registry(ws)
    for name, func in BUILTIN_FUNCTIONS.items():
        reg.register_builtin(name, func)

    KERNEL.workspace = ws
    KERNEL.registry = reg
    KERNEL.recorder = None
    KERNEL.agent = None
    return KERNEL


def _kernel() -> Any:
    """取当前进程的 KERNEL；没铺好就现铺一个。

    懒初始化是刻意的：``read_file`` 这类命令根本不需要内核，
    不该为它扫描工作区、注册 33 个内置工具。
    """
    from .kernel import KERNEL

    if not KERNEL.ready:
        setup_offline_kernel()
    return KERNEL


# ---------------------------------------------------------------- 由函数构造
def _build_from_globals(funcs: Dict[str, Any]) -> Dict[str, Any]:
    """用与注册器同一条判据挑出可注册函数，再 build_tool 拿 schema。"""
    from .tool import build_tool, is_registrable

    mod_name = funcs.get("__name__")
    tools: Dict[str, Any] = {}
    for name, obj in funcs.items():
        if not is_registrable(mod_name, name, obj):
            continue
        try:
            tools[name] = build_tool(obj)
        except Exception:  # noqa: BLE001  签名解析失败 → 这个函数在 CLI 里不可用
            continue
    return tools


# ---------------------------------------------------------------- argparse
def _add_globals(p: argparse.ArgumentParser) -> None:
    """三个全局开关。

    ``default=argparse.SUPPRESS`` 是必须的：否则子 parser 会把 ``--json`` 的
    默认 False 写回 namespace，覆盖掉「写在子命令之前」的那个 True。
    """
    p.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                   help='单行 JSON 输出：{"ok":..,"fn":..,"result":..}（给其他 agent 用）')
    p.add_argument("--quiet", action="store_true", default=argparse.SUPPRESS,
                   help="成功时不打印正文，只靠退出码（适合 && 串联）")
    p.add_argument("--traceback", action="store_true", default=argparse.SUPPRESS,
                   help="异常时打印完整堆栈")


def _brief(description: str) -> str:
    """取 docstring 的第一段（首行小标题之后的那个自然段）作为 --help 的简介。"""
    text = (description or "").strip()
    if not text:
        return ""
    para: List[str] = []
    seen_body = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            if para:
                break
            continue
        if re.match(r"^(Args|Arguments|参数|Returns?|返回)\s*[:：]", stripped):
            break
        para.append(stripped)
        seen_body = True
    if not seen_body:
        return ""
    return " ".join(para)


def _returns_hint(description: str) -> str:
    """从工具描述里抽出 ``Returns:`` 段 —— 它回答「这个命令返回什么」。"""
    text = description or ""
    lines = text.splitlines()
    out: List[str] = []
    inside = False
    for line in lines:
        stripped = line.strip()
        if not inside:
            if re.match(r"^(Returns?|返回)\s*[:：]\s*$", stripped):
                inside = True
            continue
        if not stripped:
            if out:
                break
            continue
        if re.match(r"^(Args|Arguments|参数|Yields?|Raises|Examples?|Notes?)\s*[:：]", stripped):
            break
        out.append(stripped)
    return " ".join(out)


def _add_one(p: argparse.ArgumentParser, name: str, sch: Dict[str, Any],
             required: bool) -> None:
    """把一个参数的 JSON Schema 片段翻成 argparse 的一个选项。"""
    flag = "--" + name.replace("_", "-")
    aliases: List[str] = [flag]
    if flag != "--" + name:
        aliases.append("--" + name)          # 同时接受 --show_hidden 这种写法

    kw: Dict[str, Any] = {}
    if sch.get("description"):
        kw["help"] = sch["description"]
    if "enum" in sch:
        kw["choices"] = sch["enum"]

    ptype = sch.get("type")
    pytype = {"integer": int, "number": float}.get(ptype, str)

    if ptype == "boolean":
        p.add_argument(*aliases, action=argparse.BooleanOptionalAction,
                       default=bool(sch.get("default", False)), **kw)
        return
    if required:
        p.add_argument(*aliases, type=pytype, required=True, **kw)
        return
    # 忠实采用签名里的默认值：Optional[X] = None 时没有 default 键，
    # argparse 自然得到 None —— 「不传」与「传 0」因此仍然可分。
    p.add_argument(*aliases, type=pytype, default=sch.get("default"), **kw)


# ---------------------------------------------------------------- 执行
def _render(result: Any) -> str:
    """把函数返回值变成要打印的文本。

    ``ImageResult`` 只在 Agent 内可注入上下文，CLI 下只能给出文字摘要。
    """
    if result is None:
        return ""
    brief = getattr(result, "brief", None)
    if brief is not None and not isinstance(result, str):
        return f"{brief}\n[图片仅在 Agent 内可见；命令行下无法展示]"
    return str(result)


def run_tools(tools: Dict[str, Any], argv: Optional[List[str]] = None,
              prog: Optional[str] = None) -> int:
    """按 tools（工具名 → Tool）跑一次命令行调用。"""
    force_utf8()
    if argv is None:
        argv = sys.argv[1:]

    if not tools:
        print("[错误] 这个文件里没有可注册的公开函数"
              "（公开函数 = 不以 `_` 开头的顶层 def）", file=sys.stderr)
        return 1

    single = len(tools) == 1
    only = next(iter(tools.values())) if single else None

    # 单函数文件：命令行本身就是「这个函数」，于是把它自己的说明当 --help 正文。
    if single and only is not None:
        desc_parts = [_brief(only.description)]
        ret = _returns_hint(only.description)
        if ret:
            desc_parts.append("返回：" + ret)
        top_desc = "\n\n".join(p for p in desc_parts if p)
    else:
        top_desc = "function CLI：每个公开函数就是一个命令。"

    top = argparse.ArgumentParser(
        prog=prog or sys.argv[0],
        description=top_desc,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="提示：--json 是给其他 agent 的稳定接口；"
               "finish / context_* / limits_* 这类需要 Agent 循环的函数在这里会返回未就绪。")
    _add_globals(top)

    if not single:
        sub = top.add_subparsers(dest="_cmd", metavar="子命令")
        for nm, t in tools.items():
            sp_desc = _brief(t.description)
            ret = _returns_hint(t.description)
            if ret:
                sp_desc = (sp_desc + "\n\n返回：" + ret).strip()
            sp = sub.add_parser(nm, help=_brief(t.description) or "",
                                description=sp_desc,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
            _add_globals(sp)
            for pn, sch in t.parameters.items():
                _add_one(sp, pn, sch, pn in t.required)
    else:
        for pn, sch in only.parameters.items():
            _add_one(top, pn, sch, pn in only.required)

    ns = top.parse_args(argv)
    opts = {k: bool(getattr(ns, k, False)) for k in ("json", "quiet", "traceback")}

    if single:
        name = next(iter(tools))
    else:
        name = getattr(ns, "_cmd", None)
        if not name:
            top.print_help()
            return 0

    tool = tools[name]
    kwargs: Dict[str, Any] = {}
    try:
        for pn, sch in tool.parameters.items():
            value = getattr(ns, pn, None)
            if value is None:
                continue
            if isinstance(value, str) and sch.get("type") == "string":
                value = _read_text_arg(value)
            kwargs[pn] = value
    except ValueError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 1

    try:
        if getattr(tool, "needs_kernel", False):
            result = tool.func(_kernel(), **kwargs)
        else:
            result = tool.func(**kwargs)
    except Exception as exc:  # noqa: BLE001
        if opts["traceback"]:
            import traceback as _tb
            _tb.print_exc()
        msg = f"[错误] {type(exc).__name__}: {exc}"
        if opts["json"]:
            print(json.dumps({"ok": False, "fn": name, "error": msg},
                             ensure_ascii=True))
        else:
            print(msg, file=sys.stderr)
        return 2

    text = _render(result)
    is_err = text.startswith("[错误]")
    if opts["json"]:
        # ensure_ascii=True 不是审美：输出退化成纯 ASCII，调用方无论按 UTF-8、
        # GBK 还是 latin-1 解码都能原样读走。
        print(json.dumps({"ok": not is_err, "fn": name,
                          "result": text or None}, ensure_ascii=True))
    elif is_err:
        print(text, file=sys.stderr)
    elif not opts["quiet"]:
        print(text if result is not None else "[ok]（无返回值）")
    return 1 if is_err else 0


def run_cli(funcs: Dict[str, Any], argv: Optional[List[str]] = None) -> int:
    """给 function 文件的尾钩用：把本模块的公开函数变成一个命令行程序。"""
    return run_tools(_build_from_globals(funcs), argv=argv)
