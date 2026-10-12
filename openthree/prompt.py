"""提示词组装：上下文由模型自己拼出来的。

「一」=  .openthree/prompts/system-prompts.md        （世界观，模型可自改）
「二」=  .openthree/functions/*.py  → 工具清单        （能力，模型可自造）
「三」=  运行期事实：工作目录、时间、演化史、上次结果  （处境，模型该知道）
         + .openthree/prompts/plan.md                 （计划，模型自己维护的待办）
         + .openthree/prompts/user-prompts.md        （任务，模型可自改）

整个 run() 每一轮都重新组装，所以模型的自我修改下一轮立即生效。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .registry import Registry
from .workspace import Workspace

RUNTIME_TEMPLATE = """\
# 你的处境（运行期自动组装）

- 工作目录：`{cwd}`
- 工作区：`{wsdir}`
- 当前时间：{now}
- 模型：{model}
- 可用工具：{tool_count} 个（内置 {builtin_count} / 自造 {user_count}）
- 本轮已是第 {iteration} 步（上限 {max_iterations}）
- 当前可折叠的 step 范围：{fold_range}（compact_context 只能用这个范围）

# 你的工具（二）

{digest}

# 你的计划（长任务的待办）

{plan}

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
                        fold_range: str = "") -> str:
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
        fold_range=fold_range or "（暂无）",
        digest=reg.digest(),
        plan=_plan_block(ws),
        last_action=last_action or "（这是第一步）",
        evolution=evolution,
        user_prompt=ws.read_user_prompt().strip() or "（空）",
    )


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
    return "\n".join(rows) if rows else "（还没有。你没改造过自己。）"


def build_messages(ws: Workspace, reg: Registry, history: List[Dict[str, Any]], *,
                   model: str, iteration: int, max_iterations: int,
                   last_action: str = "", fold_range: str = "") -> List[Dict[str, Any]]:
    """组装完整 messages。system = 一 + 运行期三；history 是模型自己走过的路。"""
    system_prompt = ws.read_system_prompt().strip()
    runtime = build_runtime_block(ws, reg, model=model, iteration=iteration,
                                  max_iterations=max_iterations, last_action=last_action,
                                  fold_range=fold_range)
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": f"{system_prompt}\n\n{runtime}"}
    ]
    messages.extend(history)
    return messages
