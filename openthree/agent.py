"""Agent：「三」—— ReAct 循环。

    观（observe）→ 思（LLM）→ 行（tool）→ 观（结果）→ … → finish

极简的坚持：
  * 没有任何硬编码的智能。循环里没有 if「用户想干什么」的分支。
  * 每一轮都重新组装 system（prompt.build_messages），
    所以模型改提示词 / 造工具 / 压缩上下文，下一轮立刻生效 —— 这就是演化。
  * 一切所见所行都进 Recorder。
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from .kernel import KERNEL
from .llm import LLM
from .prompt import build_messages
from .recorder import Recorder
from .registry import Registry
from .workspace import Workspace


class Agent:
    def __init__(self, workspace: Workspace, registry: Registry, llm: LLM,
                 recorder: Recorder, *, max_iterations: int = 40) -> None:
        self.ws = workspace
        self.reg = registry
        self.llm = llm
        self.rec = recorder
        self.max_iterations = max_iterations

        self.history: List[Dict[str, Any]] = []
        self.answer: Optional[str] = None
        self.finished = False
        self.iteration = 0
        self.last_action = ""
        self._compact_marks: set = set()

    # ---------------------------------------------------------------- 主循环
    def run(self) -> str:
        while not self.finished and self.iteration < self.max_iterations:
            self.iteration += 1
            self.rec.begin_step()
            self._apply_pending_compactions()

            messages = build_messages(
                self.ws, self.reg, self.history,
                model=self.llm.config.model,
                iteration=self.iteration,
                max_iterations=self.max_iterations,
                last_action=self.last_action,
            )
            result = self.llm.chat(messages, tools=self.reg.schemas() or None)
            msg = result.get("message") or {}
            self.rec.record_llm(result.get("raw_request", {}), result.get("raw_response", {}),
                                result.get("elapsed_ms", 0.0), result.get("usage"),
                                result.get("error"))

            if result.get("error"):
                self.rec.record("error", summary="LLM 调用失败", data={"error": result["error"]})
                self.answer = f"[未能完成] 模型调用失败：{result['error']}"
                break

            content = (msg.get("content") or "").strip()
            tool_calls = msg.get("tool_calls") or []

            # 记下模型说的话（reasoning 也是工作记录的一部分）
            if content:
                self.rec.record("note", summary=content[:200],
                                data={"thought": content, "phase": "assistant"})

            assistant_msg: Dict[str, Any] = {"role": "assistant", "content": msg.get("content") or ""}
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            self.history.append(assistant_msg)

            if not tool_calls:
                # 没有工具调用 = 模型选择直接说话，视为阶段结论
                self.last_action = f"（无工具调用，直接回复）{content[:300]}"
                self.answer = content or self.answer
                self.rec.record("note", summary="模型未调用工具，视为收尾",
                                data={"answer": content})
                break

            self._run_tool_calls(tool_calls)

        if not self.finished and self.answer is None:
            self.answer = (f"[达到迭代上限 {self.max_iterations} 步，未显式 finish] "
                           f"以下是最后一条思考：\n{self.last_action}")
            self.rec.record("error", summary="达到迭代上限", data={"max": self.max_iterations})

        return self.answer or ""

    # ---------------------------------------------------------------- 执行工具
    def _run_tool_calls(self, tool_calls: List[Dict[str, Any]]) -> None:
        summaries = []
        for call in tool_calls:
            fn = call.get("function") or {}
            name = fn.get("name") or call.get("name") or ""
            raw_args = fn.get("arguments") or call.get("arguments") or "{}"
            call_id = call.get("id") or f"call_{self.iteration}_{name}"

            args = _parse_args(raw_args)
            started = time.time()
            output, error = self._execute(name, args)
            elapsed = (time.time() - started) * 1000

            self.rec.record(
                "tool",
                summary=f"{name}({_short_args(args)})",
                data={"name": name, "arguments": args, "output": _clip(output),
                      "error": error, "call_id": call_id},
                elapsed_ms=elapsed,
            )
            summaries.append(f"{name} → {_clip(output, 160)}")

            self.history.append({
                "role": "tool",
                "tool_call_id": call_id,
                "name": name,
                "content": output if output else (error or "[无输出]"),
            })

        self.last_action = "\n".join(summaries)

    def _execute(self, name: str, args: Dict[str, Any]) -> tuple[str, Optional[str]]:
        tool = self.reg.get(name)
        if tool is None:
            return "", f"未知工具 {name}。当前可用：" + ", ".join(self.reg.names())
        call_args = dict(args)
        if tool.needs_kernel:
            call_args["kernel"] = KERNEL
        try:
            out = tool.func(**call_args)
        except TypeError as exc:
            return "", f"参数不匹配（{exc}）。函数签名：{tool.signature_hint()}"
        except Exception as exc:  # noqa: BLE001
            import traceback
            return "", f"执行异常 {type(exc).__name__}: {exc}\n{traceback.format_exc()[-800:]}"
        if out is None:
            return "[ok]（无返回值）", None
        return str(out), None

    # ---------------------------------------------------------------- 上下文压缩
    def compact(self, start: int, end: int, summary: str) -> None:
        self._compact_marks.add((start, end, summary))

    def _apply_pending_compactions(self) -> None:
        """把 compact_context 的意图落到 history 上（按 step 与 message 对齐）。"""
        while self._compact_marks:
            start, end, summary = self._compact_marks.pop()
            # history 中每条 assistant(tool_calls) + 后续 tool 结果 = 一个 step
            steps = _index_steps(self.history)
            idxs = [i for s, (a, b) in steps.items() if start <= s <= end]
            if not idxs:
                continue
            lo, hi = min(idxs), max(idxs)
            head = self.history[:lo]
            tail = self.history[hi + 1:]
            node = {
                "role": "assistant",
                "content": f"[已折叠 step {start}-{end}] {summary}",
            }
            self.history = head + [node] + tail
            self.rec.record("evolution", summary=f"已折叠 step {start}-{end} 为摘要",
                            data={"action": "compact_applied", "start": start, "end": end,
                                  "summary": summary})

    # ---------------------------------------------------------------- 收尾
    def finish(self, answer: str) -> None:
        self.answer = answer
        self.finished = True

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Agent iter={self.iteration} tools={len(self.reg)} events={len(self.rec.events)}>"


# ---------------------------------------------------------------- 工具函数


def _parse_args(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {"value": data}
    except Exception:  # noqa: BLE001
        try:
            from json_repair import repair_json  # type: ignore
            data = json.loads(repair_json(raw))
            return data if isinstance(data, dict) else {"value": data}
        except Exception:  # noqa: BLE001
            return {"value": raw}


def _short_args(args: Dict[str, Any]) -> str:
    parts = []
    for k, v in args.items():
        s = str(v).replace("\n", "⏎")
        parts.append(f"{k}={s[:60]}")
    return ", ".join(parts)


def _clip(text: str, limit: int = 4000) -> str:
    text = text if isinstance(text, str) else str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [截断，共 {len(text)} 字符]"


def _index_steps(history: List[Dict[str, Any]]) -> Dict[int, tuple]:
    """history 中每个 assistant-with-tool_calls 算一个 step，覆盖到其 tool 结果末尾。"""
    steps: Dict[int, tuple] = {}
    n = 1
    i = 0
    while i < len(history):
        m = history[i]
        if m.get("role") == "assistant" and m.get("tool_calls"):
            j = i + 1
            while j < len(history) and history[j].get("role") == "tool":
                j += 1
            steps[n] = (i, j - 1)
            n += 1
            i = j
        else:
            i += 1
    return steps
