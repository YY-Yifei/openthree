"""Agent：「三」—— ReAct 循环。

    观（observe）→ 思（LLM）→ 行（tool）→ 观（结果）→ … → finish

极简的坚持：
  * 没有任何硬编码的智能。循环里没有 if「用户想干什么」的分支。
  * 每一轮都重新组装 system（prompt.build_messages），
    所以模型改提示词 / 造工具 / 压缩上下文，下一轮立刻生效 —— 这就是演化。
  * 一切所见所行都进 Recorder。

工程加固（不改变设计）：
  * 工具结果进 history 前统一截断，超长输出落盘，history 只留预览 + 路径，
    避免同一份大输出被每轮重发（见 REVIEW.md P0-3.2）。
  * 上下文预算：超过阈值时自动把最老的若干 step 折叠成摘要，
    不再指望模型自觉调用 compact_context（见 REVIEW.md P0-3.1）。
  * finish 提交后，同批剩余 tool_calls 一律跳过（见 REVIEW.md P2-3.11）。
  * compact_context 的区间先在现有 history 上校验，如实返回成功/失败（P1-3.8）。
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from .kernel import KERNEL
from .llm import LLM
from .prompt import build_messages
from .recorder import Recorder
from .registry import Registry
from .workspace import Workspace

# 单个工具结果进入 history 的字符上限；超出则落盘、history 留预览。
TOOL_RESULT_LIMIT = 4000
# 对话历史（估算字符数）超过该值就自动折叠最老的 step；<=0 关闭。
DEFAULT_CONTEXT_BUDGET_CHARS = 120_000


class Agent:
    def __init__(self, workspace: Workspace, registry: Registry, llm: LLM,
                 recorder: Recorder, *, max_iterations: int = 200,
                 tool_result_limit: int = TOOL_RESULT_LIMIT,
                 context_budget_chars: int = DEFAULT_CONTEXT_BUDGET_CHARS) -> None:
        self.ws = workspace
        self.reg = registry
        self.llm = llm
        self.rec = recorder
        self.max_iterations = max_iterations
        self.tool_result_limit = max(0, int(tool_result_limit))
        self.context_budget_chars = int(context_budget_chars)

        self.history: List[Dict[str, Any]] = []
        self.answer: Optional[str] = None
        self.finished = False
        self.iteration = 0
        self.last_action = ""
        self._compact_marks: List[Tuple[int, int, str]] = []

    # ---------------------------------------------------------------- 主循环
    def run(self) -> str:
        while not self.finished and self.iteration < self.max_iterations:
            self.iteration += 1
            self.rec.begin_step()
            self._apply_pending_compactions()
            self._auto_compact()

            steps = _index_steps(self.history)
            fold_range = f"{min(steps)}-{max(steps)}" if steps else ""
            messages = build_messages(
                self.ws, self.reg, self.history,
                model=self.llm.config.model,
                iteration=self.iteration,
                max_iterations=self.max_iterations,
                last_action=self.last_action,
                fold_range=fold_range,
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

            # finish 已提交：同批剩余调用不再执行，但仍补齐 tool 消息保持序列合法
            if self.finished:
                self.history.append({
                    "role": "tool", "tool_call_id": call_id, "name": name,
                    "content": "[跳过：finish 已提交，未执行]",
                })
                self.rec.record("note", summary=f"跳过 {name}（已 finish）",
                                data={"name": name, "call_id": call_id})
                summaries.append(f"{name} → （跳过：已 finish）")
                continue

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

            body = output if output else (error or "[无输出]")
            self.history.append({
                "role": "tool",
                "tool_call_id": call_id,
                "name": name,
                "content": self._spill(call_id, name, body),
            })

        self.last_action = "\n".join(summaries)

    def _spill(self, call_id: str, name: str, text: str) -> str:
        """工具结果进入 history 前统一截断；超长部分落盘，history 只留预览 + 路径。"""
        text = text if isinstance(text, str) else str(text)
        limit = self.tool_result_limit
        if limit <= 0 or len(text) <= limit:
            return text
        out_dir = self.ws.logs_dir / "tool-output"
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            fp = out_dir / f"{_safe_name(call_id)}.txt"
            fp.write_text(text, encoding="utf-8")
            note = f"\n... [已截断，完整输出共 {len(text)} 字符，见 {fp}，可用 read_file 读取]"
        except Exception:  # noqa: BLE001
            note = f"\n... [已截断，共 {len(text)} 字符]"
        return text[:limit] + note

    def _execute(self, name: str, args: Dict[str, Any]) -> Tuple[str, Optional[str]]:
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
    def compact(self, start: int, end: int, summary: str) -> Tuple[bool, str]:
        """登记一次折叠意图，并先在现有 history 上校验区间是否真的存在。

        返回 (是否登记成功, 面向模型的说明)。
        """
        try:
            start, end = int(start), int(end)
        except (TypeError, ValueError):
            return False, f"[错误] start/end 必须是整数，收到 {start!r}/{end!r}"
        if start > end:
            return False, f"[错误] start({start}) 大于 end({end})"
        steps = _index_steps(self.history)
        if not steps:
            return False, "[错误] 当前还没有可折叠的 step（至少要走完一个工具调用回合）"
        lo, hi = min(steps), max(steps)
        if not any(start <= s <= end for s in steps):
            return False, (f"[错误] step 区间 {start}-{end} 与现有 step {lo}-{hi} 不相交，"
                           f"未折叠。请用当前可折叠范围 {lo}-{hi}。")
        self._compact_marks.append((start, end, summary))
        return True, f"[ok] 已登记折叠 step {start}-{end}（当前范围 {lo}-{hi}），下一轮生效。"

    def _apply_pending_compactions(self) -> None:
        """把 compact_context 的意图落到 history 上（按 step 与 message 对齐）。

        一次性处理本批全部意图，避免逐条折叠后 step 重新编号导致区间错位。
        """
        if not self._compact_marks:
            return
        marks = sorted(self._compact_marks)
        self._compact_marks = []

        steps = _index_steps(self.history)
        if not steps:
            return
        chosen = sorted({s for s in steps
                         for (st, en, _) in marks if st <= s <= en})
        if not chosen:
            return
        lo, hi = chosen[0], chosen[-1]
        lo_idx = min(steps[s][0] for s in chosen)
        hi_idx = max(steps[s][1] for s in chosen)

        summary = "; ".join(f"step {st}-{en}: {sm}" for st, en, sm in marks)
        head = self.history[:lo_idx]
        tail = self.history[hi_idx + 1:]
        node = {
            "role": "assistant",
            "content": f"[已折叠 step {lo}-{hi}] {summary}",
        }
        self.history = head + [node] + tail
        self.rec.record("evolution", summary=f"已折叠 step {lo}-{hi} 为摘要",
                        data={"action": "compact_applied", "start": lo, "end": hi,
                              "summary": summary})

    def _auto_compact(self) -> None:
        """上下文超预算时自动折叠最老的 step（不依赖模型自觉）。"""
        if self.context_budget_chars <= 0:
            return
        if _estimate_chars(self.history) <= self.context_budget_chars:
            return
        steps = _index_steps(self.history)
        keys = sorted(steps)
        keep = max(2, len(keys) // 4)          # 至少保留最近 2 步
        candidates = keys[:-keep] if len(keys) > keep else []
        if not candidates:
            return

        def size(msgs: List[Dict[str, Any]]) -> int:
            return _estimate_chars(msgs)

        base = size(self.history)
        lo = candidates[0]
        hi = lo
        for s in candidates:
            hi = s
            lo_idx = steps[lo][0]
            hi_idx = steps[s][1]
            folded = self.history[:lo_idx] + self.history[hi_idx + 1:]
            if size(folded) <= self.context_budget_chars:
                break
        self.rec.record("note", summary=f"上下文超预算（约 {base} 字符），自动折叠 step {lo}-{hi}",
                        data={"action": "auto_compact", "start": lo, "end": hi,
                              "budget": self.context_budget_chars, "before_chars": base})
        self.compact(lo, hi, "（自动折叠：早期步骤，完整过程见 logs/ 与 steps/）")
        self._apply_pending_compactions()

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


def _safe_name(name: str) -> str:
    return re.sub(r"[^0-9A-Za-z_.-]+", "_", name or "call")[:120] or "call"


def _estimate_chars(history: List[Dict[str, Any]]) -> int:
    """粗略估算一条 messages 的字符体量（1 token ≈ 2 字符就够了）。"""
    total = 0
    for m in history:
        c = m.get("content")
        if isinstance(c, str):
            total += len(c)
            total += 20
        elif c:
            total += len(str(c))
        for tc in (m.get("tool_calls") or []):
            try:
                total += len(json.dumps(tc, ensure_ascii=False))
            except Exception:  # noqa: BLE001
                total += 50
    return total


def _index_steps(history: List[Dict[str, Any]]) -> Dict[int, Tuple[int, int]]:
    """history 中每个 assistant-with-tool_calls 算一个 step，覆盖到其 tool 结果末尾。"""
    steps: Dict[int, Tuple[int, int]] = {}
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
