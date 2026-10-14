"""命令行入口。

    openthree "你的任务"              就地初始化 .openthree/ 并开始 ReAct（单次，不读旧对话）
    openthree                         沿用 .openthree/prompts/user-prompts.md 继续（单次）
    openthree -i                      交互模式（进程内会话，多轮记忆）
    openthree -i --auto 5             长任务自动推进：计划未完成就接力，最多 5 轮
    openthree --resume 4              显式把对话档案里最近 4 轮读回本会话（默认不读）
    openthree --turns 8               会话内保留最近 8 轮（默认 4，0 关闭）
    openthree --init                  只初始化工作区
    openthree --report                只重建 three-report.html
    openthree --inspect               看一眼自己当前的样子（不调模型）
    openthree --functions             列出所有工具

跨轮记忆只在**会话**里存在（`-i` / `--auto`）；单次任务从零开跑，绝不读磁盘上
上一次的对话。对话原文仍 append 到 transcript.jsonl 作档案，想续读就用 --resume。

跑长任务时终端不会沉默：每一步、每次模型调用、每次工具执行都有回声，
等待超过 --heartbeat 秒还会打印心跳（默认 15s，设 0 关闭）。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Optional

from . import __version__
from .runtime import Runtime


def _force_utf8() -> None:
    """Windows 控制台默认 GBK，会让中文与符号报 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def _print_banner(rt: "Runtime") -> None:
    """身份卡：它叫什么（它是谁） + 自造工具 / 演化次数（它长成什么样了）。"""
    print("\n" + rt.identity_card() + "\n", flush=True)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="openthree",
        description="极简 LLM Agent Harness：agent 天然自我演化（命名、长工具、改提示词）",
    )
    p.add_argument("task", nargs="*", help="任务描述；留空则沿用 .openthree/prompts/user-prompts.md")
    p.add_argument("-C", "--dir", default=".", help="项目目录（.openthree 会建在这里），默认当前目录")
    p.add_argument("--name", default=None, metavar="NAME",
                   help="给这个智能体起个名字：只在它**还没有名字**时作为种子落盘"
                        "（已经有名字就不覆盖）；之后它可用 set_identity 自己改名。"
                        "不传则名字由智能体自己生长")
    p.add_argument("-m", "--model", default=None, help="覆盖模型名")
    p.add_argument("--image", action="append", default=None, metavar="PATH_OR_URL",
                   help="随任务附一张图片（多模态）：本地路径或 http(s) 链接；可重复。")
    p.add_argument("--images", default=None, metavar="A,B,C",
                   help="随任务附图片，逗号分隔（等价于多个 --image）。")
    p.add_argument("-n", "--max-iterations", type=int, default=None,
                   help="每轮 ReAct 最大步数（显式给出即锁定，模型改不动）；"
                        "不传则由 .openthree/limits.json 决定，再退回内核默认 200 —— "
                        "模型运行期可用 set_limits 改它。一轮没跑完会自动开下一轮，"
                        "整场轮数由 --max-rounds 兜住")
    p.add_argument("--max-rounds", type=int, default=None,
                   help="一次任务最多跑几轮（每轮一次完整 ReAct 循环，各自有 --max-iterations 步预算）；"
                        "显式给出即锁定，不传则由 .openthree/limits.json / 内核默认 3 决定，"
                        "模型运行期可用 set_limits(max_rounds=…) 改它。"
                        "这是「整场最多几轮」，与 --auto N 说的是同一件事（同时给就取较小者）")
    p.add_argument("--context-window", default=None,
                   help="模型上下文窗口（token）。支持 1M/128k/200000 写法；"
                        "默认取 config 里的 context_window_tokens，再退回 1M")
    p.add_argument("--context-budget", type=int, default=None,
                   help="历史累计字符数超此值自动折叠早期步骤，0 表示关闭；"
                        "默认按上下文窗口推导（1M 窗口 → 100 万字符）。"
                        "模型运行期可用 context_budget 工具在 .openthree/context.json 里改它")
    p.add_argument("--no-auto-fold", action="store_true",
                   help="关闭内核兜底自动折叠（等价于环境变量 OPENTHREE_CONTEXT_AUTOFOLD=0）；"
                        "模型仍可自己用 compact_context 折叠")
    p.add_argument("--tool-result-limit", type=int, default=None,
                   help="单个工具结果进入历史的字符上限（显式给出即锁定），超出落盘；"
                        "不传则由 .openthree/limits.json / 内核默认 4000 决定，模型可用 set_limits 改")
    p.add_argument("--max-tokens", type=int, default=None,
                   help="单次生成 token 上限（显式给出即锁定）；"
                        "不传则由 config.yaml 的 llm.max_tokens / limits.json 决定，模型可用 set_limits 改")
    p.add_argument("--temperature", type=float, default=None,
                   help="采样温度（显式给出即锁定）；不传则由 config.yaml / limits.json 决定")
    p.add_argument("--heartbeat", type=float, default=15.0,
                   help="长等待（模型/工具）期间每隔多少秒打印一次心跳，0 关闭，默认 15")
    p.add_argument("--append", action="store_true", help="把任务追加到 user-prompts.md 而不覆盖")
    p.add_argument("--init", action="store_true", help="只初始化 .openthree/ 工作区，不调模型")
    p.add_argument("--force-prompts", action="store_true", help="初始化时覆盖已有的提示词（危险）")
    p.add_argument("--report", action="store_true", help="只重新生成 three-report.html")
    p.add_argument("--inspect", action="store_true", help="看一眼自己当前的样子")
    p.add_argument("--functions", action="store_true", help="列出当前所有工具")
    p.add_argument("-i", "--interactive", action="store_true",
                   help="交互模式：开一个进程内会话，多轮记忆（退出即忘）")
    p.add_argument("--auto", type=int, default=0, metavar="N",
                   help="长任务自动推进：一轮收尾后若计划仍有未完成项（或撞了本轮步数上限），"
                        "就自动接力下一轮，最多 N 轮（0 = 关闭，默认）—— 它直接决定本场 "
                        "max_rounds。常与 -i 联用。"
                        "注意这与单次任务自带的「撞步数上限自动续轮」不同：后者只在没跑完时续，"
                        "不看计划。")
    p.add_argument("--turns", type=int, default=4,
                   help="会话内保留最近多少轮（播种进上下文），0 关闭，-1 全部，默认 4")
    p.add_argument("--resume", type=int, default=0, metavar="N",
                   help="从对话档案 transcript.jsonl 续读最近 N 轮作为会话起点；"
                        "默认 0 = 不读盘，从零开始")
    p.add_argument("-q", "--quiet", action="store_true", help="安静模式（连进度回声也关掉）")
    p.add_argument("-V", "--version", action="version", version=f"openthree {__version__}")
    return p


def main(argv=None) -> int:
    _force_utf8()
    args = build_parser().parse_args(argv)
    project_dir = Path(args.dir).resolve()

    # --no-auto-fold：把自动折叠的「硬闸」环境变量在起 Runtime 前设好，
    # 于是连 config 推导出来的默认预算也不会触发内核兜底折叠。
    if getattr(args, "no_auto_fold", False):
        os.environ["OPENTHREE_CONTEXT_AUTOFOLD"] = "0"

    rt = Runtime(
        project_dir,
        model=args.model,
        max_iterations=args.max_iterations,
        force_prompts=args.force_prompts,
        quiet=args.quiet,
        context_budget_chars=args.context_budget,
        context_window_tokens=args.context_window,
        tool_result_limit=args.tool_result_limit,
        max_tokens=args.max_tokens,
        temperature=args.temperature,
        heartbeat=args.heartbeat,
        turns=args.turns,
        max_rounds=args.max_rounds,
        name=args.name,
    )

    if args.init:
        print(f"[ok] 工作区已就绪 {rt.ws.dir}")
        print(rt.self_inspect())
        return 0

    if args.report:
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
    images = _collect_images(args)

    if args.interactive:
        return _interactive(rt, task, args.append, images, auto=args.auto,
                            resume=args.resume)

    if not task and not rt.ws.has_task() and not images:
        _print_banner(rt)
        print("没有任务。用法：openthree \"你的任务\"   或   openthree -i")
        return 1

    _print_banner(rt)
    # --resume：显式把档案里最近 N 轮读回本会话（默认不读盘，从零开跑）。
    if args.resume > 0:
        loaded = rt.begin_session(resume=args.resume)
        print(f"[会话] 已从对话档案续读最近 {args.resume} 轮"
              f"（实际载入 {loaded} 轮）\n", flush=True)
    try:
        if args.auto > 0:
            doc = rt.run_auto(task or None, rounds=args.auto, append=args.append,
                              images=images or None)
        else:
            # --resume 时调用方自带会话（读到档案即已是会话），故 session=True 归调用方；
            # 否则不给 session → run() 自带一个短命会话，供「撞步数上限自动续轮」轮间连续。
            doc = rt.run(task or None, append=args.append, images=images or None,
                         session=args.resume > 0)
    except RuntimeError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2

    print("\n" + "─" * 60)
    print(doc.get("answer", ""))
    print("─" * 60)
    return 0 if doc.get("status") == "done" else 1


def _collect_images(args) -> list[str]:
    """从 --image / --images 收集图片引用（顺序保留，去重）。"""
    out: list[str] = []
    for x in (getattr(args, "image", None) or []):
        if x:
            out.append(x)
    raw = getattr(args, "images", None)
    if raw:
        for x in str(raw).split(","):
            x = x.strip()
            if x:
                out.append(x)
    seen: set[str] = set()
    uniq: list[str] = []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq


def _interactive(rt: Runtime, first_task: str, append: bool,
                 first_images: Optional[list] = None, auto: int = 0,
                 resume: int = 0) -> int:
    _force_utf8()
    _print_banner(rt)
    # 交互模式 = 一个进程内会话：逐轮累积、跨轮可见，退出即忘。
    loaded = rt.begin_session(resume=resume)
    memory = ("关闭" if rt.turns == 0
              else "全部" if rt.turns < 0
              else f"最近 {rt.turns} 轮")
    print(f"交互模式。工作区 {rt.ws.dir}")
    print(f"输入 :q 退出，:r 重建报告，:i 自省，:img 附图片；会话记忆：{memory}")
    if resume > 0:
        print(f"续读：已从对话档案载入最近 {resume} 轮（实际 {loaded} 轮）")
    if auto > 0:
        print(f"自动推进：开启（每轮跑完后若计划仍有未完成项，最多接力 {auto} 轮）")
    print(f"对话档案：{rt.ws.transcript_file}（只增不改；记忆只在本会话内）\n")
    pending = first_task
    pending_images = list(first_images or [])
    while True:
        prompt = "你 ▸ " if not pending_images else f"你(+{len(pending_images)}图) ▸ "
        try:
            text = pending or input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        pending = ""
        if not text and not pending_images:
            continue
        if text in (":q", ":quit", ":exit"):
            return 0
        if text == ":r":
            print(f"[ok] {rt.report()}")
            continue
        if text == ":i":
            print(rt.self_inspect())
            continue
        if text == ":img" or text.startswith(":img "):
            extra = text[len(":img"):].strip()
            if not extra:
                print("用法：:img <路径或URL>[,...]")
                continue
            pending_images.extend(_split_image_arg(extra))
            print(f"[ok] 已附加 {len(pending_images)} 张图片，随下一条消息发出")
            continue
        images = pending_images or None
        pending_images = []
        try:
            if auto > 0:
                doc = rt.run_auto(text or None, rounds=auto, append=append,
                                  images=images)
                answers = doc.get("auto_answers") or []
            else:
                doc = rt.run(text or None, append=append, images=images, session=True)
                answers = doc.get("round_answers") or []
            if len(answers) > 1:
                print("\n" + "═" * 60)
                print(f"（本次任务跑了 {len(answers)} 轮 —— 一轮没做完就自动续了下一轮，"
                      f"逐轮结论如下）")
                print("═" * 60)
                for i, a in enumerate(answers, 1):
                    print(f"\n──── 第 {i} 轮 ────\n{a}")
                print("═" * 60 + "\n")
                continue
            print("\n" + "─" * 60)
            print(doc.get("answer", ""))
            print("─" * 60 + "\n")
        except RuntimeError as exc:
            print(f"[错误] {exc}", file=sys.stderr)


def _split_image_arg(raw: str) -> list:
    out = []
    for x in str(raw).replace("，", ",").split(","):
        x = x.strip()
        if x:
            out.append(x)
    return out


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
