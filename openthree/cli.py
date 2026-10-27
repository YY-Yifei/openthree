"""命令行入口。

    openthree "你的任务"              就地初始化 .openthree/ 并开始 ReAct（单次，不读旧对话）
    openthree                         不带参数 = 进入交互模式（-i 是默认模式）
    openthree -i                      交互模式（显式写法，与不带参数等价）
    openthree --no-interactive        不带参数时走「单次」：沿用磁盘上的当前任务跑一次
    openthree -i --auto 5             长任务自动推进：计划未完成就接力，最多 5 轮
    openthree --resume 4              显式把对话档案里最近 4 轮读回本会话（默认不读）
    openthree --turns 8               会话内保留最近 8 轮（默认 4，0 关闭）
    openthree --init                  只初始化工作区
    openthree --report                只重建 three-report.html
    openthree --inspect               看一眼自己当前的样子（不调模型）
    openthree --functions             列出所有工具

交互模式是**默认模式**：不带任务参数、不带图片、不开 --auto，且 stdin 连着终端时，
`openthree` 直接进 -i，不必再记那个 -i。**stdin 不是终端时**（管道 / 重定向 / CI /
脚本，或显式 `--no-interactive`）走单次路径 —— 交互模式需要人来敲字，无人应答
的 REPL 只会立刻 EOF、什么也不做就退出。

跨轮记忆只在**会话**里存在（`-i` / `--auto`）；单次任务从零开跑，不读磁盘上
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


def _stdin_is_tty() -> bool:
    """stdin 是否连着终端 —— 「不带参数默认进交互模式」的前提。

    管道 / 重定向 / CI / pytest 下为 False：那时 REPL 会立刻 EOF、什么也不做就
    退出，所以走单次。任何异常一律当「不是终端」。
    """
    try:
        return bool(sys.stdin is not None and sys.stdin.isatty())
    except Exception:  # noqa: BLE001
        return False


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="openthree",
        description="极简 LLM Agent Harness：agent 天然自我演化（命名、长工具、改提示词）",
    )
    p.add_argument("task", nargs="*",
                   help="任务描述；留空则**进入交互模式**（-i 是默认模式）。"
                        "要让「留空」走单次语义（沿用磁盘上的当前任务跑一次），"
                        "用 --no-interactive；stdin 不是终端时自动走这条路")
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
                        "显式给出即锁定，不传则由 .openthree/limits.json / 内核默认 10 决定，"
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
                   help="交互模式：开一个进程内会话，多轮记忆（退出即忘）。"
                        "这是**默认模式** —— 不带任务参数、不带图片、不开 --auto，"
                        "且 stdin 连着终端时直接进交互模式，无需显式写 -i")
    p.add_argument("--no-interactive", action="store_true",
                   help="不带任务参数时不要进交互模式：走「单次」语义 ——"
                        "磁盘上有当前任务就沿用并跑一次，否则只打印用法。"
                        "脚本 / CI（stdin 不是终端）本来就会自动走这条路")
    p.add_argument("--auto", type=int, default=0, metavar="N",
                   help="长任务自动推进：一轮收尾后若**接力自己写过的**计划仍有未完成项"
                        "（或撞了本轮步数上限 / 模型主动 next_round），就自动接力下一轮，"
                        "最多 N 轮（0 = 关闭，默认）—— 它直接决定本场 max_rounds。常与 -i 联用。"
                        "未完成项只以**接力期间写过的计划**为准：磁盘上残留的勾选项不驱动接力。"
                        "触到轮数上限而仍有活时，进程退出码非 0（CI 可判「没做完」）。"
                        "注意这与单次任务自带的「撞步数上限自动续轮」不同：后者只在没跑完时续，"
                        "不看计划。")
    p.add_argument("--turns", type=int, default=4,
                   help="会话内保留最近多少轮（播种进上下文），0 关闭，-1 全部，默认 4")
    p.add_argument("--resume", type=int, default=0, metavar="N",
                   help="从对话档案 transcript.jsonl 续读最近 N 轮作为会话起点；"
                        "默认 0 = 不读盘，从零开始")
    p.add_argument("-u", "--upgrade", action="store_true",
                   help="升级模式：扫描当前项目的 .openthree/ 相对**本机已装内核**的漂移"
                        "（缺 / 旧 / 不合规），打印清单，然后进入交互会话 —— 由模型逐条给"
                        "升级指导，人确认/补充后才落盘。"
                        "⚠ 它不会让你的 openthree 变新（那是 pip install -U openthree），"
                        "只让**这个工作区**追平本机内核。与任务参数互斥")
    p.add_argument("--check", action="store_true",
                   help="只扫描并打印漂移清单就退出（不调模型、不改任何文件），"
                        "适合 CI。有漂移退出码 1，没有 0")
    p.add_argument("--dry-run", action="store_true",
                   help="升级模式下只让模型出提案、不落盘")
    p.add_argument("-q", "--quiet", action="store_true", help="安静模式（连进度回声也关掉）")
    p.add_argument("-V", "--version", action="version", version=f"openthree {__version__}")
    return p


def main(argv=None) -> int:
    _force_utf8()
    args = build_parser().parse_args(argv)
    project_dir = Path(args.dir).resolve()

    # --- 升级模式的参数校验（都在起 Runtime 之前，因为构造 Runtime 会写盘） ---
    task_arg = " ".join(args.task).strip()
    if args.upgrade and task_arg:
        print("[错误] -u/--upgrade 不接受任务参数 —— 它自带升级任务。"
              "要跑别的任务，去掉 -u。", file=sys.stderr)
        return 2
    if (args.check or args.dry_run) and not args.upgrade:
        print("[错误] --check / --dry-run 要与 -u 一起用。", file=sys.stderr)
        return 2
    if args.upgrade and args.init:
        print("[错误] -u 与 --init 互斥：--init 只铺工作区，-u 要读工作区。", file=sys.stderr)
        return 2

    # -u --check：只扫描、只打印、退出。**不调模型、不写任何文件** ——
    # 所以它必须走在 Runtime 构造之前：Runtime.__init__ 会 ws.init() 落盘。
    if args.upgrade and args.check:
        return _upgrade_check(project_dir)

    # -u 也必须在起 Runtime 之前拦住「没有工作区」的情形：Runtime.__init__ 会
    # ws.init() 把工作区铺出来，于是「升级一个已有工作区」会悄悄变成「新建一个
    # 全新工作区」—— 用户以为自己在升级，实际上得到的是初始化，且拿不到任何提示。
    if args.upgrade and not (project_dir / ".openthree").is_dir():
        print(f"[错误] {project_dir / '.openthree'} 不存在 —— 这里还没有工作区，"
              "没有可升级的东西。\n"
              "       新项目请直接运行 openthree（或 --init）来创建工作区。",
              file=sys.stderr)
        return 2

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

    # --- 升级模式：扫描 → 报告 → 进交互会话 ---
    # 首条任务由内核生成（报告 + 规程），不是用户输入的 —— 所以这里显式覆盖 task。
    # stdin 不是终端时不允许进升级会话：它要人逐条确认，无人应答的 REPL 毫无意义。
    if args.upgrade:
        from .upgrade import (build_upgrade_task, render_report, scan_upgrade,
                              write_playbook)

        if not _stdin_is_tty():
            print("[错误] -u 需要交互终端（要人逐条确认）。"
                  "无人值守请用 `openthree -u --check`（只扫描，不改文件）。",
                  file=sys.stderr)
            return 2
        report = scan_upgrade(rt.ws)
        print(render_report(report))
        # 规程落盘：上下文被折叠、或模型忘了规矩时，可以 read_file 重读回来。
        # 做成文件而不是内置工具，是因为工具的 schema 会出现在每一次请求里，
        # 而这条规程只在升级模式用得上。
        print(f"\n升级规程：{write_playbook(rt.ws)}")
        print()
        return _interactive(rt, build_upgrade_task(report, dry_run=args.dry_run),
                            args.append, None, auto=args.auto, resume=0,
                            upgrade=True, dry_run=args.dry_run)

    # 不带参数 = 进交互模式（-i 是默认模式）。前提是有人在键盘边：stdin 不是终端
    # （管道 / CI / 脚本）时 REPL 会立刻 EOF、什么也不做就退出，所以那种情况走单次。
    # 显式 -i 不受此限；--no-interactive 强制走单次。
    if (args.interactive
            or (not args.no_interactive
                and not task and not images
                and args.auto <= 0 and args.resume <= 0
                and _stdin_is_tty())):
        return _interactive(rt, task, args.append, images, auto=args.auto,
                            resume=args.resume)

    if not task and not rt.ws.has_task() and not images:
        _print_banner(rt)
        print("没有任务，也没进入交互模式（stdin 不是终端，或指定了 --no-interactive）。")
        print("用法：openthree \"你的任务\"；在终端里直接 openthree 则进入交互模式。")
        return 1

    _print_banner(rt)
    # --resume：显式把档案里最近 N 轮读回本会话（默认不读盘，从零开跑）。
    if args.resume > 0:
        loaded = rt.begin_session(resume=args.resume)
        print(f"[会话] 已从对话档案续读最近 {args.resume} 轮"
              f"（实际载入 {loaded} 轮）\n", flush=True)
    # 纯图片输入（只有图、没有文字任务）绝不能传 None —— None 是「沿用磁盘上的
    # 当前任务」的信号，会把图片附加到磁盘上的任务上。空串才是
    # 「本轮任务 = 看这张图」，runtime 不会去读盘。
    user_arg = task if task else ("" if images else None)
    try:
        if args.auto > 0:
            doc = rt.run_auto(user_arg, rounds=args.auto, append=args.append,
                              images=images or None)
        else:
            # --resume 时调用方自带会话（读到档案即已是会话），故 session=True 归调用方；
            # 否则不给 session → run() 自带一个短命会话，供「撞步数上限自动续轮」轮间连续。
            doc = rt.run(user_arg, append=args.append, images=images or None,
                         session=args.resume > 0)
    except RuntimeError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2

    print("\n" + "─" * 60)
    print(doc.get("answer", ""))
    print("─" * 60)
    # --auto 跑到轮数上限却仍有活（计划未完成 / 撞步数上限）时不算成功：
    # 退出码非 0，CI / 脚本才判得出「长任务其实没做完」。
    unfinished = bool(doc.get("auto_unfinished")) if args.auto > 0 else False
    return 0 if doc.get("status") == "done" and not unfinished else 1


def _upgrade_check(project_dir: Path) -> int:
    """`-u --check`：只扫描 + 打印，**不调模型、不写任何文件**。

    它刻意不经过 `Runtime`：构造 Runtime 会 `ws.init()` 落盘，而 `--check` 对 CI
    承诺「看一眼、不改动」。所以这里直接拿 `Workspace` 读盘扫描。

    Returns:
        0 = 没有漂移；1 = 有漂移；2 = 环境错误（工作区不存在等）。
    """
    from .workspace import Workspace

    ws = Workspace(project_dir)
    if not ws.dir.exists():
        print(f"[错误] {ws.dir} 不存在 —— 这里还没有工作区。"
              "先跑一次 openthree（或 --init）再来升级。", file=sys.stderr)
        return 2
    from .upgrade import render_report, scan_upgrade

    report = scan_upgrade(ws)
    print(render_report(report))
    return 1 if report.has_drift else 0


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
                 resume: int = 0, upgrade: bool = False,
                 dry_run: bool = False) -> int:
    _force_utf8()
    _print_banner(rt)
    # 交互模式 = 一个进程内会话：逐轮累积、跨轮可见，退出即忘。
    loaded = rt.begin_session(resume=resume)
    memory = ("关闭" if rt.turns == 0
              else "全部" if rt.turns < 0
              else f"最近 {rt.turns} 轮")
    print(f"交互模式。工作区 {rt.ws.dir}")
    if upgrade:
        print("升级模式：模型给指导，**改动先进待应用区**，你确认后才落盘")
        print("输入 :q 退出，:staged 看待应用改动，:apply 落盘，:undo 撤销上次升级，"
              ":r 重建报告，:i 自省")
    else:
        print(f"输入 :q 退出，:r 重建报告，:i 自省，:img 附图片；会话记忆：{memory}")
    if resume > 0:
        print(f"续读：已从对话档案载入最近 {resume} 轮（实际 {loaded} 轮）")
    if auto > 0:
        print(f"自动推进：开启（每轮跑完后若计划仍有未完成项，最多接力 {auto} 轮）")
    if upgrade and dry_run:
        print("注意：--dry-run 已开启 —— 本轮只出方案，:apply 会被拒绝")
    print(f"对话档案：{rt.ws.transcript_file}（只增不改；记忆只在本会话内）\n")
    pending = first_task
    pending_images = list(first_images or [])
    try:
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
                # 升级模式退出前提醒：待应用区还有没落盘的改动，别让人以为已经生效了。
                if upgrade:
                    from .upgrade import list_staged

                    left = list_staged(rt.ws)
                    if left:
                        print(f"注意：待应用区还有 {len(left)} 个文件没落盘"
                              "（下次 :apply 才生效）：")
                        for rel in left:
                            print(f"  - {rel}")
                return 0
            if text == ":r":
                print(f"[ok] {rt.report()}")
                continue
            if text == ":i":
                print(rt.self_inspect())
                continue
            if text == ":staged":
                from .upgrade import render_staged

                print(render_staged(rt.ws))
                continue
            if text == ":apply":
                if not upgrade:
                    print(":apply 只在升级模式（-u）下可用。")
                    continue
                from .upgrade import apply_staged

                if dry_run:
                    print("--dry-run 已开启：只出方案，不落盘。要真正升级请去掉 --dry-run。")
                    continue
                res = apply_staged(rt.ws)
                print(res.summary())
                if res.applied:
                    print("\n[提示] 改动已生效。提示词类的改动要下一轮才被读进上下文；"
                          "自造 function 若要立刻可用，用 reload_functions。")
                continue
            if text == ":undo":
                if not upgrade:
                    print(":undo 只在升级模式（-u）下可用。")
                    continue
                from .upgrade import undo_last

                res = undo_last(rt.ws)
                print(res.summary())
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
            # 纯图片输入（只附图、无文字）传 ""，绝不传 None —— None 会被 runtime
            # 当成「沿用磁盘当前任务」，把图片附到磁盘上那条任务上。
            user_arg = text if text else ""
            try:
                if auto > 0:
                    doc = rt.run_auto(user_arg, rounds=auto, append=append,
                                      images=images)
                    answers = doc.get("auto_answers") or []
                else:
                    doc = rt.run(user_arg, append=append, images=images, session=True)
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
    finally:
        # 退出（:q / EOF / Ctrl-C）也要收会话：否则同一个 Runtime 被复用（嵌入 /
        # 测试 / 同一进程再跑单次任务）时会莫名继承本会话记忆。
        rt.end_session()


def _split_image_arg(raw: str) -> list:
    out = []
    for x in str(raw).replace("，", ",").split(","):
        x = x.strip()
        if x:
            out.append(x)
    return out


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
