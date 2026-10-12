"""提示词组装：上下文由模型自己拼出来的。

「一」=  .openthree/prompts/system-prompts.md        （世界观，模型可自改）
「二」=  .openthree/functions/*.py  → 工具清单        （能力，模型可自造）
「三」=  运行期事实：工作目录、时间、演化史、上次结果  （处境，模型该知道）
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

# 你的工具（二）

{digest}

# 你上次的行动

{last_action}

# 演化史（你为自己做了哪些改造）

{evolution}

---

# 当前任务（三）

{user_prompt}
"""


def build_runtime_block(ws: Workspace, reg: Registry, *, model: str, iteration: int,
                        max_iterations: int, last_action: str = "") -> str:
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
        digest=reg.digest(),
        last_action=last_action or "（这是第一步）",
        evolution=evolution,
        user_prompt=ws.read_user_prompt().strip() or "（空）",
    )


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
                   last_action: str = "") -> List[Dict[str, Any]]:
    """组装完整 messages。system = 一 + 运行期三；history 是模型自己走过的路。"""
    system_prompt = ws.read_system_prompt().strip()
    runtime = build_runtime_block(ws, reg, model=model, iteration=iteration,
                                  max_iterations=max_iterations, last_action=last_action)
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": f"{system_prompt}\n\n{runtime}"}
    ]
    messages.extend(history)
    return messages
