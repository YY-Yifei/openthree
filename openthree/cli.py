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

    openthree channel add             引导式配置频道（微信 / 钉钉），写入 .openthree/channels.yaml
    openthree -d                      后台常驻：**不读控制台**，只等频道消息

频道（`.openthree/channels.yaml`）在**启动时自动加载**：`-i` 下控制台与频道并存
（频道消息进来就像另一个用户在打字），`-d` 下只等频道、完全不碰 stdin。
一份配置可以放多个频道，它们各自一条线程、互不拖累；内核一行都不用改 ——
频道消息走的就是 `-i` 里那一句 `Runtime.run()`。

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
from .workspace import DIRNAME, canon_path

# `--help` 的尾注（argparse `epilog`）。它答「**怎么自省**」，不答「此刻是什么」——
# 后者属于运行期状态，写进来 `--help` 就不再可 diff、不能进文档。
# 三条命令各管一件事：我是谁、差什么、版本几。
_EPILOG = """\
自省（都不调模型）
  openthree --inspect       我是谁：名字 / 工作区 / 提示词 / 计划 / 工具数 / 上下文策略
                            （会顺手把缺失的工作区铺好，与 --init 等价）
  openthree -u --check      我差什么：相对**本机已装内核**的漂移清单
                            （缺 / 旧 / 不合规；只读、不改文件；有漂移退出码 1，适合 CI）
  openthree -V              内核版本；智能体自己用 whoami 看它的版本与 schema

语义边界：-u 只让**这个工作区**追平本机内核，
让 openthree 本身体变新是 `pip install -U openthree` —— 那是两件事。
"""


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
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("task", nargs="*",
                   help="任务描述；留空则**进入交互模式**（-i 是默认模式）。"
                        "要让「留空」走单次语义（沿用磁盘上的当前任务跑一次），"
                        "用 --no-interactive；stdin 不是终端时自动走这条路")
    p.add_argument("-C", "--dir", default=".", help="项目目录（.openthree 会建在这里），默认当前目录")
    # 单横线长选项：argparse 允许显式注册（`-reasoning` 已有先例），而 `-profile`
    # 正是最容易脱口而出的写法。
    p.add_argument("-profile", "--profile", action="append", default=None,
                   metavar="DIR", dest="profile",
                   help="额外的 function 来源：一个**含 .openthree/ 的目录**（与 -C 同构），"
                        "只取其中的 .openthree/functions/*.py。可重复给，也可逗号分隔"
                        "（路径本身含逗号时只能重复给 —— 没有转义写法）。"
                        "顺序就是优先级：先写的更近；本项目（d=0）永远压过它们。"
                        "除 functions 之外一概不读（memory / config / prompts 都不共享）")
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
    # 三种写法都收：`--reasoning-effort`（标准）、`--reasoning`（短别名）、
    # `-reasoning`（单横线）。最后那个不是笔误 —— argparse 允许显式注册单横线
    # 长选项，而 `-reasoning low` 正是最容易脱口而出的写法，顺手认了比让人
    # 对着 "unrecognized arguments" 猜要好。
    p.add_argument("--reasoning-effort", "--reasoning", "-reasoning", default=None,
                   metavar="EFFORT", dest="reasoning_effort",
                   help="推理强度（显式给出即锁定）：minimal / low / medium / high；"
                        "不传则取 config.yaml 的 llm.reasoning_effort，再退回「不传这个字段」。"
                        "取值不设白名单 —— 各家 provider 档位不同，写错由 provider 报错")
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
    p.add_argument("-d", "--daemon", action="store_true",
                   help="后台常驻：**不再等控制台输入**，只等频道（微信 / 钉钉）消息，"
                        "收到就当作一次用户输入跑 ReAct、把答案回到原会话。"
                        "频道来自 .openthree/channels.yaml（用 `openthree channel add` 生成）。"
                        "与任务参数互斥；Ctrl-C 退出")
    p.add_argument("--no-channels", action="store_true",
                   help="不自动加载频道（交互模式默认会加载 .openthree/channels.yaml 里启用的频道）")
    p.add_argument("--only", default=None, metavar="NAME[,NAME]",
                   help="只启动指定名字的频道（配合 -d / -i；默认全部启用项）")
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
    raw = list(sys.argv[1:] if argv is None else argv)
    # `openthree channel …` 是子命令：它有自己的参数（--type / --set / --online …），
    # 主解析器不认识它们，所以在主解析器之前就截住，原样转交。
    if raw and raw[0] in ("channel", "channels"):
        from .channels.cli import main as channel_main

        return channel_main(raw[1:])
    args = build_parser().parse_args(argv)
    project_dir = Path(args.dir).resolve()

    # --- -profile 的校验（也在起 Runtime 之前，理由同上：构造 Runtime 会写盘） ---
    # 路径打错时若等到 Runtime 之后才发现，用户拿到的是一个凭空建出来的新工作区。
    raw_profiles = _collect_profiles(args)
    if raw_profiles and (args.upgrade or args.check):
        print("[错误] -u/--upgrade 与 --check 不接受 -profile —— 它们扫的是"
              "「本项目对本机内核」的漂移，profile 不是本项目的一部分。",
              file=sys.stderr)
        return 2
    profiles, profile_errors, profile_notes = _resolve_profiles(raw_profiles, project_dir)
    if profile_errors:
        print("[错误] -profile 有问题：", file=sys.stderr)
        for e in profile_errors:
            print(f"       {e}", file=sys.stderr)
        print("       profile 是一个**含 .openthree/ 的项目目录**（与 -C 同构）；"
              "先用 `openthree -C <该目录> --init` 把它的工作区铺出来。",
              file=sys.stderr)
        return 2
    for n in profile_notes:
        print(f"[提示] {n}", file=sys.stderr)

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
        reasoning_effort=args.reasoning_effort,
        heartbeat=args.heartbeat,
        turns=args.turns,
        max_rounds=args.max_rounds,
        name=args.name,
        profiles=profiles,
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

    # --- 后台常驻：只等频道输入 ---
    # 它和「跑一个任务」是两种东西：任务有终点，频道没有。所以先拦住任何任务参数，
    # 否则用户会以为 `-d "顺便干点什么"` 两件事都会发生。
    if args.daemon:
        if " ".join(args.task).strip() or _collect_images(args):
            print("[错误] -d 不接受任务 / 图片参数 —— 它只等频道消息。"
                  "要跑任务就去掉 -d。", file=sys.stderr)
            return 2
        if args.upgrade:
            print("[错误] -d 与 -u 互斥。", file=sys.stderr)
            return 2
        # -d 的全部意义就是「只跑频道」；同时说 --no-channels 是自相矛盾，
        # 而默默忽略其中一个会让用户以为另一边生效了。
        if args.no_channels:
            print("[错误] -d 与 --no-channels 互斥 —— -d 就是「只跑频道」。",
                  file=sys.stderr)
            return 2
        return _daemon(rt, args)

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
                            upgrade=True, dry_run=args.dry_run,
                            only=args.only, no_channels=True, quiet=args.quiet)

    # 不带参数 = 进交互模式（-i 是默认模式）。前提是有人在键盘边：stdin 不是终端
    # （管道 / CI / 脚本）时 REPL 会立刻 EOF、什么也不做就退出，所以那种情况走单次。
    # 显式 -i 不受此限；--no-interactive 强制走单次。
    if (args.interactive
            or (not args.no_interactive
                and not task and not images
                and args.auto <= 0 and args.resume <= 0
                and _stdin_is_tty())):
        return _interactive(rt, task, args.append, images, auto=args.auto,
                            resume=args.resume, only=args.only,
                            no_channels=args.no_channels, quiet=args.quiet)

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


def _collect_profiles(args) -> list[str]:
    """从 `-profile` 收集目录字符串（重复给与逗号分隔等价，按出现顺序拼接）。"""
    out: list[str] = []
    for chunk in (getattr(args, "profile", None) or []):
        for x in str(chunk or "").split(","):
            x = x.strip()
            if x:
                out.append(x)
    return out


def _resolve_profiles(raw: list[str], project_dir: Optional[Path] = None,
                      ) -> tuple[list[Path], list[str], list[str]]:
    """展开 / resolve / canonical 去重 / 校验 `-profile` 的每一项。

    必须在构造 `Runtime` **之前**调用：`Runtime.__init__` 会 `ws.init()` 落盘，
    所以「路径打错了」不能等到那之后才发现 —— 否则用户拿到的是一个凭空建出来
    的新工作区，而他以为只是在挂一个共享库。

    「挂了但等于没挂」不算错误（退出码仍是 0），但也绝不静默：`Workspace.
    function_sources` 会在加载层按 canon_path 去重、按目录是否存在过滤，那里
    只是**正确地少加载**了，不知道该向谁报告 —— 提示只能在这里给。

    Args:
        raw: `-profile` 收集来的原始字符串清单（未去重）
        project_dir: `-C` 解析后的项目目录；给了才能认出「挂到项目自己」

    Returns:
        `(profiles, errors, notes)`；errors 非空时调用方打印并返回 exit 2，
        notes 是给人看的提示（逐条打印，不拦）。
    """
    profiles: list[Path] = []
    errors: list[str] = []
    notes: list[str] = []
    seen: set[str] = set()
    own = canon_path(project_dir) if project_dir is not None else None
    for item in raw:
        try:
            p = Path(item).expanduser().resolve()
        except (OSError, RuntimeError) as exc:   # pragma: no cover - 路径异常
            errors.append(f"{item}：路径无法解析（{exc}）")
            continue
        if not (p / DIRNAME).is_dir():
            why = "" if p.is_dir() else "（这个目录本身不存在）"
            errors.append(f"{p}：不是一个工作区{why} —— 缺 {p / DIRNAME}")
            continue
        key = canon_path(p)
        if own is not None and key == own:
            notes.append(f"{p}：与 -C 的项目目录是同一个，已忽略 —— "
                         f"本项目本来就是最内层（d=0），不需要挂自己。")
            continue
        if key in seen:      # 同一目录被点名两次：按先写的（更近）算
            notes.append(f"{p}：重复点名，按先写的（更近）算。")
            continue
        seen.add(key)
        if not (p / DIRNAME / "functions").is_dir():
            notes.append(f"{p}：这一档没有 {DIRNAME}/functions 目录，暂时不贡献任何工具"
                         f"（仍算已挂载，可以往里写 function）。")
        profiles.append(p)
    return profiles, errors, notes


def _interactive(rt: Runtime, first_task: str, append: bool,
                 first_images: Optional[list] = None, auto: int = 0,
                 resume: int = 0, upgrade: bool = False,
                 dry_run: bool = False, only: Optional[str] = None,
                 no_channels: bool = False, quiet: bool = False) -> int:
    _force_utf8()
    _print_banner(rt)
    # 交互模式 = 一个进程内会话：逐轮累积、跨轮可见，退出即忘。
    loaded = rt.begin_session(resume=resume)

    # 频道在交互模式下**自动加载**：控制台与频道共用这一个会话、这一把锁 ——
    # 于是频道消息进来就像另一个人在你旁边敲了一行字，回答会回到它来的那个会话。
    bridge = channels_mgr = None
    if not no_channels:
        from .channels import AgentBridge, load_manager

        bridge = AgentBridge(rt, echo=not quiet)
        try:
            channels_mgr = load_manager(rt.ws, bridge, only=_split_channel_names(only))
        except Exception as exc:  # noqa: BLE001 - 频道配坏了不该挡住控制台
            print(f"[频道] 配置读取失败（忽略）：{exc}")
        if channels_mgr is not None and channels_mgr.statuses:
            channels_mgr.start()
            print(f"[频道] 已启动 {len(channels_mgr.statuses)} 个："
                  f"{', '.join(channels_mgr.active)}（消息进来就像一行用户输入）")
        elif channels_mgr is not None and channels_mgr.skipped:
            print("[频道] 未启动：\n" + "\n".join(f"  - {s}" for s in channels_mgr.skipped))

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
                # 与控制台的频道工作线程共用一把锁：同一时刻只跑一条 ReAct 循环。
                if auto > 0:
                    doc = (bridge.under_lock(lambda: rt.run_auto(
                        user_arg, rounds=auto, append=append, images=images))
                        if bridge is not None else
                        rt.run_auto(user_arg, rounds=auto, append=append, images=images))
                    answers = doc.get("auto_answers") or []
                else:
                    doc = (bridge.under_lock(lambda: rt.run(
                        user_arg, append=append, images=images, session=True))
                        if bridge is not None else
                        rt.run(user_arg, append=append, images=images, session=True))
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
        # 频道同理：先把线程停掉、把桥收工，再收会话，免得收尾期间还有消息进来。
        if channels_mgr is not None:
            channels_mgr.stop()
        if bridge is not None:
            bridge.stop()
        rt.end_session()


def _split_channel_names(raw) -> Optional[list[str]]:
    """`--only wx,ding` → `["wx", "ding"]`；空 → None（= 全部启用项）。"""
    if not raw:
        return None
    names = [x.strip() for x in str(raw).replace("，", ",").split(",") if x.strip()]
    return names or None


def _daemon(rt: Runtime, args) -> int:
    """后台常驻：启动频道，只等它们把消息送进来。

    它不读 stdin —— 这正是 `-d` 与 `-i` 的分水岭：`-i` 等人敲字，`-d` 等人发消息。
    两者用的是同一个 `Runtime.run()`：所以「频道消息」与「用户输入」在智能体看来
    是同一件事，内核不需要知道 `-d` 存在。
    """
    from .channels import AgentBridge, load_manager

    bridge = AgentBridge(rt, echo=not args.quiet)
    try:
        mgr = load_manager(rt.ws, bridge, only=_split_channel_names(args.only))
    except Exception as exc:  # noqa: BLE001 - 配置坏了要给出可操作的提示
        print(f"[错误] 读取频道配置失败：{exc}", file=sys.stderr)
        return 2

    if not mgr.statuses:
        print("[错误] 没有可启动的频道：", file=sys.stderr)
        print(mgr.skip_summary(), file=sys.stderr)
        print("\n先配一个：openthree channel add\n"
              "看现有配置：openthree channel list", file=sys.stderr)
        return 2

    _print_banner(rt)
    rt.begin_session()
    try:
        mgr.start()
        print(f"[频道] 已启动 {len(mgr.statuses)} 个：{', '.join(mgr.active)}")
        print("[频道] 后台运行中 —— 不再读控制台输入，Ctrl-C 退出。\n", flush=True)
        error = mgr.wait()
        if error:
            print(f"[频道] 有频道异常退出：{error}", file=sys.stderr)
    except KeyboardInterrupt:
        print("\n[频道] 收到中断，正在退出…")
    finally:
        mgr.stop()
        rt.end_session()
    return 0


def _split_image_arg(raw: str) -> list:
    out = []
    for x in str(raw).replace("，", ",").split(","):
        x = x.strip()
        if x:
            out.append(x)
    return out


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
