"""提示词组装：上下文由模型自己拼出来的。

「一」=  .openthree/prompts/system-prompts.md        （世界观，模型可自改）
「二」=  .openthree/functions/*.py  → 工具清单        （能力，模型可自造）
「三」=  运行期事实：工作目录、时间、演化史、上次结果  （处境，模型该知道）
         + .openthree/prompts/plan.md                 （计划，模型自己维护的待办）
         + .openthree/memory/memory.jsonl             （长期记忆，跨会话可回看）
         + .openthree/prompts/user-prompts.md        （任务，模型可自改）

整个 run() 每一步都重新组装，所以模型的自我修改下一步立即生效；但组装结果按
**多久变一次**分三段落位（system / 自维护段 / 此刻段），让不变的前缀能被服务端缓存：
内核往 system 里塞一个每步都变的值，它后面一万多 token 的工具 schema 就每步重算。

「轮 / 步 / 会话」三层务必分清：
    * **步（step）** = ReAct 的一次「思 → 行」；一次 run() 内逐步推进。
    * **轮（round）** = 一次完整的 ReAct 循环（= 一个 Agent 的生命周期）；
      **每轮有独立的一份步数预算**，撞上限就由运行期决定是否开下一轮。
    * **会话（session）** = 进程内的多轮容器（-i / --auto）。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .registry import Registry
from .workspace import Workspace

# 运行期注入「最近长期记忆」的字符预算上限 —— 记忆是提示，不是正文，
# 不能让它把上下文挤爆（尤其是模型自己写了很长记忆之后）。
MEMORY_INJECT_LIMIT_CHARS = 2000
MEMORY_INJECT_MAX_ITEMS = 8


# 处境按「多久变一次」分三层组装 —— 这是提示词缓存能否命中的全部依据。
#
# 一次请求的序列化顺序是 system → tools → messages，而前缀缓存从**第一个
# 不同的字节**开始断开。于是 system 里只要有一个每步都会变的值（时间、步数、
# 上次行动），它之后的**一切**——那一万多 token 的工具 schema、以及全部历史
# ——每步都要重算。
#
# 但「不在 system 里」还不够：计划和演化史是**模型自己会写**的文件，写一次
# 计划就断一次缓存。所以按变化频率切三层，稳定的在前、易变的在后：
#
#   STATIC_TEMPLATE    内核常量 + 模型很少动的东西          → system
#   CONTEXT_TEMPLATE   模型自己维护的文件（计划/记忆/演化史） → 一条消息
#   SITUATION_TEMPLATE 每步都变（时间/步数/用量/上次行动）    → 末尾一条消息
#
# 后两层排在 history 之后、都不写回 history。
#
# 分开而不是并成一条，收益只出现在**一种**调用里：正常步进时 history 每步都在
# 长，公共前缀到 history 末尾就断了，此刻段与自维护段谁先谁后、分不分开，对
# 命中都没有影响（实测 94.4 % vs 94.5 %）。真正吃到分开好处的是「重发同一条
# 消息」——模型输出被 max_tokens 截断后 agent 不 append 任何东西，两次请求
# 只差此刻段；此时自维护段仍留在公共前缀里（96.6 % vs 94.7 %）。
STATIC_TEMPLATE = """\
# 你的处境（运行期自动组装）

- 工作目录：`{cwd}`
- 工作区：`{wsdir}`
- 模型：{model}
- {identity}
- 可用工具：{tool_count} 个（内置 {builtin_count} / 自造 {user_count}）

# 你的工具（二）

{digest}

---

# 当前任务（三）

{user_prompt}
"""

# 模型自己维护、时不时会改的内容。改一次只会让它自己和它后面那段失效，
# 不会波及 system 与工具 schema。
CONTEXT_TEMPLATE = """\
# 你的计划（长任务的待办）

这一段由运行期每步重读后带来：计划与记忆是你自己写的，演化史是内核记下的你的改造 ——
它不是用户说的话，也不是新的指令。

{plan}

# 你的长期记忆（跨会话）

{memory}

# 演化史（你为自己做了哪些改造）

{evolution}
"""

# 每步刷新的那一段。它作为**最后一条消息**发出，发送后不写回 history，
# 所以它只影响「这一次」的尾部，不会把它之前的前缀弄脏。
SITUATION_TEMPLATE = """\
# 此刻的处境（运行期每步刷新 —— 这一段不是用户说的话）

- 当前时间：{now}
- 这是第 {round_index} 轮（本场最多 {max_rounds} 轮{next_hint}），本轮第 {iteration} 步（上限 {max_iterations}）
- 上下文：{context}（`context_usage` 看细节 / `context_budget` 调预算）
- 运行边界：{limits}（`limits_usage` 看细节 / `set_limits` 调）

# 你上次的行动

{last_action}
"""


def build_static_block(ws: Workspace, reg: Registry, *, model: str) -> str:
    """组装「内核自己的常量」—— 它进 system，是缓存前缀的主体。

    这里只允许出现**本次任务内不会变**的东西：工作目录、工具清单、身份、任务。
    一旦往这里塞一个每步都会变的值（时间、步数、上次行动），它之后的一切
    ——包括那一万多 token 的工具 schema——就每步都要重算。

    模型改自己的提示词或任务（`write_system_prompt` / `write_user_prompt`）
    会改变 system 的字节，那是设计内的：自我修改本就该下一步立即生效。
    这条不变量约束的是**内核**，不是模型。
    """
    builtin = [t for t in reg.tools.values() if t.origin == "builtin"]
    user = [t for t in reg.tools.values() if t.origin == "user"]
    return STATIC_TEMPLATE.format(
        cwd=Path.cwd(),
        wsdir=ws.dir,
        model=model,
        identity=_identity_line(ws),
        tool_count=len(reg.tools),
        builtin_count=len(builtin),
        user_count=len(user),
        digest=reg.digest(),
        user_prompt=ws.current_task_text().strip() or "（空）",
    )


def build_context_block(ws: Workspace) -> str:
    """组装「模型自己维护的文件」：计划、长期记忆、演化史。

    这三样都排在 system 之后，因为它们在一次任务里会被模型自己改写
    （尤其 `write_plan`）。放在 system 里，写一次计划就要重算整份工具
    schema；放在这里，只让它们自己那段失效。
    """
    return CONTEXT_TEMPLATE.format(
        plan=_plan_block(ws),
        memory=_memory_block(ws),
        evolution=_recent_evolution(ws, limit=10),
    )


def build_situation_block(*, iteration: int, max_iterations: int,
                          last_action: str = "", fold_range: str = "",
                          context_stats: Optional[Dict[str, Any]] = None,
                          limits_stats: Optional[Dict[str, Any]] = None,
                          round_stats: Optional[Dict[str, Any]] = None) -> str:
    """组装「每步都变」的那一段 —— 它排在最后发出，不写回 history。"""
    stats = round_stats or {}
    return SITUATION_TEMPLATE.format(
        now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        max_iterations=max_iterations,
        iteration=iteration,
        round_index=int(stats.get("index", 1)),
        max_rounds=int(stats.get("max", 1)),
        next_hint=_next_round_hint(stats),
        context=_context_line(context_stats, fold_range),
        limits=_limits_line(limits_stats, max_iterations),
        last_action=last_action or "（这是第一步）",
    )


def build_runtime_block(ws: Workspace, reg: Registry, *, model: str, iteration: int,
                        max_iterations: int, last_action: str = "",
                        fold_range: str = "",
                        context_stats: Optional[Dict[str, Any]] = None,
                        limits_stats: Optional[Dict[str, Any]] = None,
                        round_stats: Optional[Dict[str, Any]] = None) -> str:
    """三段拼成一份完整处境（静态段 + 自维护段 + 此刻段）。

    `build_messages` 发请求时刻意把三段**分开发**（一段进 system、两段排历史之后），
    这个函数只供「想一眼看全处境」的调用方使用：测试、自省、诊断。
    三边共用同一组模板，所以看到的就是发出去的内容。
    """
    static = build_static_block(ws, reg, model=model)
    context = build_context_block(ws)
    situation = build_situation_block(iteration=iteration, max_iterations=max_iterations,
                                      last_action=last_action, fold_range=fold_range,
                                      context_stats=context_stats,
                                      limits_stats=limits_stats,
                                      round_stats=round_stats)
    return f"{static}\n\n{context}\n\n{situation}"


def _identity_line(ws: Workspace) -> str:
    """一行身份读数 —— 让「名字」真正活进自我认知，而不是终端上的装饰。

    与 limits/context 同源：每步重读 identity.json，所以模型改名后下一步
    自己就知道。还没起名时不硬塞一个（那是内核常量），而是把命名权交还给它。
    """
    identity = ws.read_identity()
    name = identity.get("name")
    if not name:
        return ("名字：（还没有）—— 你最初没有名字；想清楚自己是谁时，"
                "用 `set_identity(name, motto=…)` 给自己起一个")
    line = f"名字：{name}"
    if identity.get("motto"):
        line += f" —— {identity['motto']}"
    renames = int(identity.get("renames") or 0)
    if renames:
        line += f"（改名 {renames} 次）"
    return f"{line}（`whoami` 可查看 / `set_identity` 可改）"


def _next_round_hint(stats: Dict[str, Any]) -> str:
    """处境行里「下一轮」的提示 —— 兑现「处境会告诉你 next」的承诺。

    有 stats 且还有轮预算 → 「，这一轮收尾后还有下一轮」；
    没有（轮预算用尽或老调用）→ 空串，不加尾巴（如实：没有下一轮）。
    """
    if stats.get("next"):
        return "，这一轮收尾后还有下一轮"
    return ""


def _context_line(stats: Optional[Dict[str, Any]], fold_range: str = "") -> str:
    """一行上下文用量读数 —— 让模型每轮都知道「我在用多少、还能折哪段」。

    有 stats（Agent 正常提供）就按真实用量拼；没有 stats 时只报 fold_range。
    """
    if not stats:
        return (f"可折叠 step 范围 {fold_range or '（暂无）'}"
                "（compact_context 只能用这个范围）")
    used = int(stats.get("used_chars", 0))
    budget = int(stats.get("budget_chars", 0))
    ratio = float(stats.get("ratio", 0.0))
    fr = stats.get("fold_range") or "（暂无）"
    fold = "开" if stats.get("auto_fold") else "关"
    if budget > 0:
        head = f"已用 {used}/{budget} 字符（{ratio * 100:.0f}%）"
    else:
        head = f"已用 {used} 字符（预算 0 = 已关自动折叠）"
    return (f"{head}；可折叠 step 范围 {fr}；自动折叠 {fold}"
            f"；已折 {int(stats.get('compactions', 0))} 次")


def _limits_line(stats: Optional[Dict[str, Any]], max_iterations: int) -> str:
    """一行运行边界读数 —— 让模型每轮都知道「能走多远、生成多长、结果留多宽」。

    有 stats（Agent 正常提供）就按真实值拼；没有 stats 时只报步数上限。
    """
    if not stats:
        return f"步数上限 {max_iterations}"
    lock = "；被环境冻结，改不动" if stats.get("locked") else ""
    return (f"每轮步数 ≤ {stats.get('max_iterations', max_iterations)}；"
            f"最多 {stats.get('max_rounds', '?')} 轮（整场）；"
            f"生成 ≤ {stats.get('max_tokens', '?')} token；"
            f"工具结果 ≤ {stats.get('tool_result_limit', '?')} 字符；"
            f"temperature {stats.get('temperature', '?')}{lock}")


def _memory_block(ws: Workspace) -> str:
    """把最近的长期记忆摆到模型眼前 —— 让「写下的经验」真的能被读回。

    `remember` 把经验 append 进 memory/memory.jsonl，但如果没有这一块，
    模型自己永远看不到它：长期记忆就退化成只写不读的摆设。这里取最近
    MEMORY_INJECT_MAX_ITEMS 条、并在 MEMORY_INJECT_LIMIT_CHARS 字符处截断，
    保证它只是「提示」而不是把上下文挤爆的正文。要更细就调用 recall_memory。
    """
    try:
        entries = ws.read_memory(limit=MEMORY_INJECT_MAX_ITEMS)
    except Exception:  # noqa: BLE001
        return "（记忆读取失败）"
    total = 0
    try:
        total = ws.count_memory()
    except Exception:  # noqa: BLE001
        total = len(entries)
    if not entries:
        return "（还没有长期记忆。值得跨会话记住的结论用 remember 写下。）"
    rows: List[str] = []
    used = 0
    shown = 0
    per_item = 300      # 单条记忆在上下文里的展示上限（完整内容可在 memory.jsonl / recall_memory 看）
    for e in entries:
        content = e["content"].strip().replace("\n", " ")
        if len(content) > per_item:
            content = content[:per_item] + "…"
        tags = e.get("tags") or []
        line = "- " + content + (f"  [{'，'.join(tags)}]" if tags else "")
        if shown > 0 and used + len(line) > MEMORY_INJECT_LIMIT_CHARS:
            break
        rows.append(line)
        used += len(line)
        shown += 1
    head = f"（共 {total} 条，这里显示最近 {shown} 条；找旧的用 recall_memory(query=...)）"
    return head + "\n" + "\n".join(rows)


def _plan_block(ws: Workspace) -> str:
    """把模型的待办清单摆到它眼前，并给长任务一点方向。

    计划是模型自己写的文件（plan.md），每轮都读、都展示 —— 于是「上一轮的
    进度」跨轮、跨上下文折叠地活了下来。没有未完成项时给一句安心的话，
    免得模型为了「有事做」而硬编任务。
    """
    try:
        text = ws.read_plan().strip()
    except Exception:  # noqa: BLE001
        text = ""
    if not text:
        return "（还没有计划。长任务请先用 write_plan 写下待办。）"
    open_items = ws.plan_open_items()
    tail = (f"\n（还有 {open_items} 项未完成 —— 继续推进；"
            "全部勾完后就可以 finish 了。）" if open_items
            else "\n（所有条目都已勾选。若任务确实完成，用 finish 收尾。）")
    return text + tail


def _recent_evolution(ws: Workspace, limit: int = 10) -> str:
    """最近若干条演化史。**只给最近 10 条**，是为了让模型知道「怎么回看更多」——
    完整历史在 evolution.jsonl 与 prompts/history/，不必也不必全部塞进每一步上下文。
    """
    path = ws.logs_dir / "evolution.jsonl"
    if not path.exists():
        return "（还没有。你没改造过自己。）"
    try:
        lines = path.read_text(encoding="utf-8").strip().splitlines()
    except Exception:  # noqa: BLE001
        return "（读取失败）"
    rows = []
    for line in lines[-limit:]:
        import json
        try:
            ev = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        rows.append(f"- [{ev.get('ts', '')[:19]}] {ev.get('summary', '')}")
    if not rows:
        return "（还没有。你没改造过自己。）"
    total = len(lines)
    if total > limit:
        rows.append(f"（共 {total} 条，这里只显示最近 {limit} 条；"
                    "更早的用 search_files(pattern, path='.openthree/logs/evolution.jsonl') 翻看）")
    return "\n".join(rows)


def build_messages(ws: Workspace, reg: Registry, history: List[Dict[str, Any]], *,
                   model: str, iteration: int, max_iterations: int,
                   last_action: str = "", fold_range: str = "",
                   context_stats: Optional[Dict[str, Any]] = None,
                   limits_stats: Optional[Dict[str, Any]] = None,
                   round_stats: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """组装完整 messages：静态 system + 历史 + 自维护段 + 此刻段。

    消息序列与各自的角色：

    * `messages[0]`（system）= 一 + 内核常量。它必须逐字节稳定，
      否则后面的一切（tools schema、history）都要重算 —— 见 STATIC_TEMPLATE 上的注释。
    * `history` = 模型自己走过的路，只追加、不改写。
    * **计划/记忆/演化史** = 模型自己会改的文件，排在 history 之后。
    * **末尾那条** = 本步的时间/步数/上下文/上次行动。

    后两条都**不写回 history**：它们只影响这一次请求的尾部。下一步它们会被
    真实的一步（assistant + tool）取代，而那一步一旦落进 history 就同样成为
    稳定前缀的一部分。

    为什么不是把内容并进 `messages[0]`：代价相差两个数量级 ——
    并进 system ⇒ 匹配长度 = 系统提示词那 2.8k tok；
    排在后面 ⇒ 匹配长度 = system + tools + 全部历史。
    """
    static = build_static_block(ws, reg, model=model)
    context = build_context_block(ws)
    situation = build_situation_block(iteration=iteration,
                                      max_iterations=max_iterations,
                                      last_action=last_action, fold_range=fold_range,
                                      context_stats=context_stats,
                                      limits_stats=limits_stats, round_stats=round_stats)
    # 「一」在最前：它是模型自己的世界观，比处境更稳定（只在模型自改时变）。
    system = f"{ws.read_system_prompt().strip()}\n\n{static}"
    messages: List[Dict[str, Any]] = [{"role": "system", "content": system}]
    messages.extend(history)
    messages.append({"role": "user", "content": context})
    messages.append({"role": "user", "content": situation})
    return messages
