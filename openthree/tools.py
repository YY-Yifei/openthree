"""保底通道：不用给任何文件加尾钩，也能在命令行里调用 function。

    python -m openthree.tools --list                     # 内置工具一览
    python -m openthree.tools read_file --path README.md --limit 20
    python -m openthree.tools --file .openthree/functions/safe_edit.py --list
    python -m openthree.tools --file .openthree/functions/safe_edit.py \\
        replace_in_file --path a.py --old x --new y

为什么需要它：给存量文件逐个加尾钩是有成本的，而「别的 agent 现在就要调」
不能等。这个入口对**存量和将来新造的**文件一律有效。

退出码与 ``--json`` 等全局开关与文件自带尾钩完全一致（都走 cli_tool.run_tools）。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from .cli_tool import force_utf8, run_tools, setup_offline_kernel

_USAGE = """\
openthree function 命令行（保底通道）

用法：
  python -m openthree.tools --list                     列出内置工具
  python -m openthree.tools <函数> [选项...]            调用内置工具
  python -m openthree.tools --file <路径> --list        列出该文件里的工具
  python -m openthree.tools --file <路径> <函数> [选项]  调用该文件里的工具

全局选项：
  --project-dir <目录>   指定项目目录（默认向上找最近的 .openthree/，否则 cwd）
  --json                单行 JSON 输出，给其他 agent 用
  --quiet               成功时不打印正文
  --traceback           异常时打印完整堆栈
  -h, --help            显示本帮助

示例：
  python -m openthree.tools search_files --pattern is_registrable --glob *.py
  python -m openthree.tools read_file --path README.md --json
"""


def _load_file_tools(path: Path) -> Dict[str, Any]:
    """从一个 .py 文件里取出可注册函数，并建成 Tool（与注册器同一判据）。"""
    from .tool import build_tool, is_registrable

    mod_name = f"openthree_tools_cli.{path.stem}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法为 {path} 创建模块 spec")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    # 让文件内能 import 同目录的辅助模块（与 Registry.reload 的行为一致）
    parent = str(path.parent)
    added = parent not in sys.path
    if added:
        sys.path.insert(0, parent)
    try:
        spec.loader.exec_module(module)
    finally:
        if added:
            try:
                sys.path.remove(parent)
            except ValueError:
                pass

    tools: Dict[str, Any] = {}
    for name in dir(module):
        obj = getattr(module, name)
        if not is_registrable(mod_name, name, obj):
            continue
        try:
            tools[name] = build_tool(obj, origin="user", source_file=str(path))
        except Exception:  # noqa: BLE001
            continue
    return tools


def _print_list(tools: Dict[str, Any], title: str) -> int:
    print(f"# {title}（共 {len(tools)} 个）")
    for name in sorted(tools):
        brief = (tools[name].description or "").strip().splitlines()
        print(f"  {name:24s} {brief[0] if brief else ''}")
    return 0


def _resolve_file(raw: str) -> Optional[Path]:
    path = Path(raw)
    if not path.is_absolute():
        path = (Path.cwd() / path)
    return path if path.is_file() else None


def main(argv: Optional[List[str]] = None) -> int:
    force_utf8()
    args = list(sys.argv[1:] if argv is None else argv)

    project_dir = None
    if "--project-dir" in args:
        i = args.index("--project-dir")
        if i + 1 < len(args):
            project_dir = args[i + 1]
            del args[i:i + 2]
        else:
            print("[错误] --project-dir 需要一个目录参数", file=sys.stderr)
            return 1

    target: Optional[Path] = None
    if "--file" in args:
        i = args.index("--file")
        if i + 1 >= len(args):
            print("[错误] --file 需要一个 .py 路径", file=sys.stderr)
            return 1
        raw = args[i + 1]
        target = _resolve_file(raw)
        del args[i:i + 2]
        if target is None:
            print(f"[错误] 找不到文件：{raw}", file=sys.stderr)
            return 1

    want_list = "--list" in args
    if want_list:
        args.remove("--list")

    setup_offline_kernel(project_dir)

    # 取出本次要用的工具集：文件里的，或内置的。
    if target is not None:
        try:
            pool = _load_file_tools(target)
        except Exception as exc:  # noqa: BLE001
            print(f"[错误] 加载 {target} 失败：{type(exc).__name__}: {exc}",
                  file=sys.stderr)
            return 2
        source = str(target)
    else:
        from .kernel import KERNEL
        pool = dict(KERNEL.registry.tools)
        source = "内置工具"

    if not pool:
        print(f"[错误] {source} 里没有可注册的公开函数", file=sys.stderr)
        return 1

    if want_list:
        return _print_list(pool, source)

    if not args or args[0] in ("-h", "--help"):
        print(_USAGE)
        return 0

    name, rest = args[0], args[1:]
    tool = pool.get(name)
    if tool is None:
        near = [n for n in sorted(pool) if name.lower() in n.lower()][:6]
        hint = ("\n你是不是想用：" + ", ".join(near)) if near else ""
        print(f"[错误] {source} 里没有叫 `{name}` 的函数。"
              f"用 --list 看全部。{hint}", file=sys.stderr)
        return 1

    # 只把这个函数交给 run_tools：单函数时它自动免子命令，
    # 于是 `python -m openthree.tools read_file --path x` 就是自然写法。
    return run_tools({name: tool}, argv=rest,
                     prog=f"python -m openthree.tools {name}")


if __name__ == "__main__":
    raise SystemExit(main())
