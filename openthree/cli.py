"""命令行入口。

    openthree "你的任务"              就地初始化 .openthree/ 并开始 ReAct
    openthree                         沿用 .openthree/prompts/user-prompts.md 继续
    openthree -i                      交互模式（多轮对话）
    openthree --turns 8               多轮时保留最近 8 轮对话（默认 4，0 关闭）
    openthree --init                  只初始化工作区
    openthree --report                只重建 three-report.html
    openthree --inspect               看一眼自己当前的样子（不调模型）
    openthree --functions             列出所有工具
    openthree --serve                 起一个 HTTP 服务看报告（可选）

跑长任务时终端不会沉默：每一步、每次模型调用、每次工具执行都有回声，
等待超过 --heartbeat 秒还会打印心跳（默认 15s，设 0 关闭）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .kernel import KERNEL
from .runtime import Runtime


def _force_utf8() -> None:
    """Windows 控制台默认 GBK，会让中文与符号报 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def _print_banner() -> None:
    print("""
  openthree —— 三生万物
  道生一，一生二，二生三，三生万物
""", flush=True)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="openthree",
        description="三生万物 —— 极简 LLM Agent Harness，agent 天然自我演化",
    )
    p.add_argument("task", nargs="*", help="任务描述；留空则沿用 .openthree/prompts/user-prompts.md")
    p.add_argument("-C", "--dir", default=".", help="项目目录（.openthree 会建在这里），默认当前目录")
    p.add_argument("-m", "--model", default=None, help="覆盖模型名")
    p.add_argument("-n", "--max-iterations", type=int, default=200, help="ReAct 最大步数，默认 200")
    p.add_argument("--context-budget", type=int, default=120000,
                   help="历史累计字符数超此值自动折叠早期步骤，0 表示关闭，默认 120000")
    p.add_argument("--tool-result-limit", type=int, default=4000,
                   help="单个工具结果进入历史的字符上限，超出落盘，默认 4000")
    p.add_argument("--heartbeat", type=float, default=15.0,
                   help="长等待（模型/工具）期间每隔多少秒打印一次心跳，0 关闭，默认 15")
    p.add_argument("--append", action="store_true", help="把任务追加到 user-prompts.md 而不覆盖")
    p.add_argument("--init", action="store_true", help="只初始化 .openthree/ 工作区，不调模型")
    p.add_argument("--force-prompts", action="store_true", help="初始化时覆盖已有的提示词（危险）")
    p.add_argument("--report", action="store_true", help="只重新生成 three-report.html")
    p.add_argument("--report-only", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--inspect", action="store_true", help="看一眼自己当前的样子")
    p.add_argument("--functions", action="store_true", help="列出当前所有工具")
    p.add_argument("-i", "--interactive", action="store_true", help="交互模式，多轮对话")
    p.add_argument("--turns", type=int, default=4,
                   help="多轮对话保留最近多少轮（播种进上下文），0 关闭，-1 全部，默认 4")
    p.add_argument("-q", "--quiet", action="store_true", help="安静模式（连进度回声也关掉）")
    p.add_argument("-V", "--version", action="version", version=f"openthree {__version__}")
    return p


def main(argv=None) -> int:
    _force_utf8()
    args = build_parser().parse_args(argv)
    project_dir = Path(args.dir).resolve()

    rt = Runtime(
        project_dir,
        model=args.model,
        max_iterations=args.max_iterations,
        force_prompts=args.force_prompts,
        quiet=args.quiet,
        context_budget_chars=args.context_budget,
        tool_result_limit=args.tool_result_limit,
        heartbeat=args.heartbeat,
        prior_turns=args.turns,
    )

    if args.init:
        print(f"[ok] 工作区已就绪 {rt.ws.dir}")
        print(rt.self_inspect())
        return 0

    if args.report or args.report_only:
        path = rt.report()
        print(f"[ok] 报告已生成 {path}")
        return 0

    if args.inspect:
        print(rt.self_inspect())
        return 0

    if args.functions:
        print(rt.registry.digest())
        return 0

    task = " ".join(args.task).strip()

    if args.interactive:
        return _interactive(rt, task, args.append)

    if not task and not rt.ws.has_task():
        _print_banner()
        print("没有任务。用法：openthree \"你的任务\"   或   openthree -i")
        return 1

    _print_banner()
    try:
        doc = rt.run(task or None, append=args.append)
    except RuntimeError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2

    print("\n" + "─" * 60)
    print(doc.get("answer", ""))
    print("─" * 60)
    return 0 if doc.get("status") == "done" else 1


def _interactive(rt: Runtime, first_task: str, append: bool) -> int:
    _force_utf8()
    _print_banner()
    memory = ("关闭" if rt.prior_turns == 0
              else "全部" if (rt.prior_turns or 0) < 0
              else f"最近 {rt.prior_turns if rt.prior_turns is not None else 4} 轮")
    print(f"交互模式。工作区 {rt.ws.dir}")
    print(f"输入 :q 退出，:r 重建报告，:i 自省；跨轮记忆：{memory}")
    print(f"对话原文：{rt.ws.transcript_file}\n")
    pending = first_task
    while True:
        try:
            text = pending or input("你 ▸ ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        pending = ""
        if not text:
            continue
        if text in (":q", ":quit", ":exit"):
            return 0
        if text == ":r":
            print(f"[ok] {rt.report()}")
            continue
        if text == ":i":
            print(rt.self_inspect())
            continue
        try:
            doc = rt.run(text, append=append)
            print("\n" + "─" * 60)
            print(doc.get("answer", ""))
            print("─" * 60 + "\n")
        except RuntimeError as exc:
            print(f"[错误] {exc}", file=sys.stderr)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
