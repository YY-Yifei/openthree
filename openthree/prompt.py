"""提示词组装：上下文由模型自己拼出来的。

「一」=  .openthree/prompts/system-prompts.md        （世界观，模型可自改）
「二」=  .openthree/functions/*.py  → 工具清单        （能力，模型可自造）
「三」=  运行期事实：工作目录、时间、演化史、上次结果  （处境，模型该知道）
         + .openthree/prompts/plan.md                 （计划，模型自己维护的待办）
         + .openthree/memory/memory.jsonl             （长期记忆，跨会话可回看）
         + .openthree/prompts/user-prompts.md        （任务，模型可自改）

整个 run() 每一轮都重新组装，所以模型的自我修改下一轮立即生效。
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


RUNTIME_TEMPLATE = """\
# 你的处境（运行期自动组装）

- 工作目录：`{cwd}`
- 工作区：`{wsdir}`
- 当前时间：{now}
- 模型：{model}
- 可用工具：{tool_count} 个（内置 {builtin_count} / 自造 {user_count}）
- 本轮已是第 {iteration} 步（上限 {max_iterations}）
- 上下文：{context}（`context_usage` 看细节 / `context_budget` 调预算）
- 运行边界：{limits}（`limits_usage` 看细节 / `set_limits` 调）

# 你的工具（二）

{digest}

# 你的计划（长任务的待办）

{plan}

# 你的长期记忆（跨会话）

{memory}

# 你上次的行动

{last_action}

# 演化史（你为自己做了哪些改造）

{evolution}

---

# 当前任务（三）

{user_prompt}
"""


def build_runtime_block(ws: Workspace, reg: Registry, *, model: str, iteration: int,
                        max_iterations: int, last_action: str = "",
                        fold_range: str = "",
                        context_stats: Optional[Dict[str, Any]] = None,
                        limits_stats: Optional[Dict[str, Any]] = None) -> str:
    evolution = _recent_evolution(ws, limit=10)
    builtin = [t for t in reg.tools.values() if t.origin == "builtin"]
    user = [t for t in reg.tools.values() if t.origin == "user"]
    return RUNTIME_TEMPLATE.format(
        cwd=Path.cwd(),
        wsdir=ws.dir,
        now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        model=model,
        tool_count=len(reg.tools),
        builtin_count=len(builtin),
        user_count=len(user),
        max_iterations=max_iterations,
        iteration=iteration,
        context=_context_line(context_stats, fold_range),
        limits=_limits_line(limits_stats, max_iterations),
        digest=reg.digest(),
        plan=_plan_block(ws),
        memory=_memory_block(ws),
        last_action=last_action or "（这是第一步）",
        evolution=evolution,
        user_prompt=ws.read_user_prompt().strip() or "（空）",
    )


def _context_line(stats: Optional[Dict[str, Any]], fold_range: str = "") -> str:
    """一行上下文用量读数 —— 让模型每轮都知道「我在用多少、还能折哪段」。

    有 stats（Agent 正常提供）就按真实用量拼；没有（老调用/测试）就退回到
    只用 fold_range，保持向后兼容。
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

    有 stats（Agent 正常提供）就按真实值拼；没有（老调用/测试）就退回到只报步数上限。
    """
    if not stats:
        return f"步数上限 {max_iterations}"
    lock = "；被环境冻结，改不动" if stats.get("locked") else ""
    return (f"步数 ≤ {stats.get('max_iterations', max_iterations)}；"
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
    完整历史在 evolution.jsonl 与 prompts/history/，不必也不必全部塞进每一轮上下文。
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
                   limits_stats: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """组装完整 messages。system = 一 + 运行期三；history 是模型自己走过的路。"""
    system_prompt = ws.read_system_prompt().strip()
    runtime = build_runtime_block(ws, reg, model=model, iteration=iteration,
                                  max_iterations=max_iterations, last_action=last_action,
                                  fold_range=fold_range, context_stats=context_stats,
                                  limits_stats=limits_stats)
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": f"{system_prompt}\n\n{runtime}"}
    ]
    messages.extend(history)
    return messages
