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
  * 进度回声：把「正在做什么」通过注入的 progress 打出去，CLI 不再长时间沉默。
    这是纯展示层，不参与决策；不注入时走 NullProgress，行为与从前一致。
  * 跨轮记忆：会话里「之前几轮」由 Runtime 显式传入（seed_turns），以
    user/assistant 消息播种进 history，于是多轮会话里模型记得住用户说过什么。
    **Agent 自己不读磁盘** —— 没有会话（如单次任务）时 seed_turns 为空，
    历史就是空的，不会莫名背上上一轮的旧对话；会话记忆只活在进程内
    （见 runtime.begin_session / workspace.append_turn）。
    system 依然每轮重组装，播种只发生在消息层，演化机制不受影响。
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .kernel import KERNEL
from .llm import CHARS_PER_TOKEN, DEFAULT_CONTEXT_WINDOW_TOKENS, LLM
from .multimodal import (DEFAULT_DETAIL, ImageResult, build_content,
                         content_text, count_images, estimate_content_chars)
from .progress import NullProgress
from .prompt import build_messages
from .recorder import Recorder
from .registry import Registry
from .workspace import Workspace

# 单个工具结果进入 history 的字符上限；超出则落盘、history 留预览。
TOOL_RESULT_LIMIT = 4000
# 上下文预算（历史累计字符数）默认由「模型上下文窗口」推导，见 resolve_context_budget。
# 预算 ≈ min(窗口 token 数, MAX_CONTEXT_BUDGET_CHARS)：窗口越大给越多，但兜个上限。
#   窗口 = 1M token（默认）→ 预算 = 1_000_000 字符（约 50 万 token 的历史）
# 这里的 1_000_000 同时也是「不显式给窗口时」的等效默认预算。
DEFAULT_CONTEXT_BUDGET_CHARS = 1_000_000
# 预算的硬上限（= 默认窗口的推导值）：窗口更大时不再线性放大，避免阈值离谱。
# 想放开就调大这里，或用 --context-budget 显式给。
MAX_CONTEXT_BUDGET_CHARS = 1_000_000
# 历史至多占用上下文窗口的比例（留一半给 system/工具 schema/模型输出）。
CONTEXT_BUDGET_RATIO = 0.5


def resolve_context_budget(context_budget_chars: Optional[int],
                           context_window_tokens: Optional[int]) -> int:
    """算出本轮的历史字符预算。

    优先用显式传入的 `context_budget_chars`（0 = 关闭自动折叠）；否则按模型
    上下文窗口推导：budget = min(窗口 token 数, MAX_CONTEXT_BUDGET_CHARS)。
    推导用到 CHARS_PER_TOKEN（1 token ≈ 2 字符）与 RATIO（历史至多占用窗口的一半），
    两者相乘、相消后「窗口 token 数」恰好等值于「字符预算」。

    Args:
        context_budget_chars: 显式预算（字符）；None 表示按窗口推导，0 表示关闭。
        context_window_tokens: 模型上下文窗口（token）；None 表示用默认窗口。
    """
    if context_budget_chars is not None:
        return int(context_budget_chars)
    window = int(context_window_tokens or DEFAULT_CONTEXT_WINDOW_TOKENS)
    derived = int(window * CHARS_PER_TOKEN * CONTEXT_BUDGET_RATIO)
    return int(min(derived, MAX_CONTEXT_BUDGET_CHARS))


class Agent:
    def __init__(self, workspace: Workspace, registry: Registry, llm: LLM,
                 recorder: Recorder, *, max_iterations: int = 200,
                 tool_result_limit: int = TOOL_RESULT_LIMIT,
                 context_budget_chars: Optional[int] = None,
                 seed_turns: Optional[Sequence[Dict[str, Any]]] = None,
                 progress: Optional[Any] = None,
                 max_truncations: int = 2) -> None:
        self.ws = workspace
        self.reg = registry
        self.llm = llm
        self.rec = recorder
        self.max_iterations = max_iterations
        self.tool_result_limit = max(0, int(tool_result_limit))
        # 本轮用户输入携带的图片（绝对路径或 http/data URL）。
        # 在第一步注入成一条独立的 user 消息（与任务文本配对），供跨轮还原。
        self.current_images: List[str] = []
        self.task_text: str = ""
        self._images_injected = False
        # 预算默认从模型上下文窗口推导（100M 窗口 → 1 亿字符）；
        # 显式传值优先，0 表示关闭自动折叠。
        self.context_budget_chars = resolve_context_budget(
            context_budget_chars, getattr(getattr(llm, "config", None),
                                          "context_window_tokens", None))
        # 跨轮记忆：本会话「之前几轮」的对话，由 Runtime 显式传入。
        # Agent 不读盘 —— 没有会话（单次任务）时这里就是空的，从零开跑。
        self.seed_turns: List[Dict[str, Any]] = [
            dict(t) for t in (seed_turns or []) if isinstance(t, dict)]
        # 进度回声：只用来「说话」，不参与任何判断
        self.progress = progress if progress is not None else NullProgress()

        self.history: List[Dict[str, Any]] = self._seed_history()
        self.answer: Optional[str] = None
        self.finished = False
        self.iteration = 0
        self.last_action = ""
        self._compact_marks: List[Tuple[int, int, str]] = []
        # 输出被 max_tokens 截断时的重发次数（截断可恢复，不该杀死整场任务）
        self.max_truncations = max(0, int(max_truncations))
        self.truncations = 0

    # ---------------------------------------------------------------- 跨轮记忆
    def _seed_history(self) -> List[Dict[str, Any]]:
        """把本会话「之前几轮」播种成本轮历史的开头。

        多轮会话的「记性」长在这里：每轮都是新 Agent，上一轮的 history 不继承；
        运行期把会话中累积的轮次（seed_turns）显式传进来，这里还原成
        user/assistant 消息，模型就看得到「用户之前说过什么」。

        关键：**Agent 自己绝不读磁盘**。所以单次任务（没有会话）时 seed_turns
        为空，历史就是空的 —— 不会莫名背上上一次对话。会话记忆只活在进程内。
        播种只走消息层，system 依然每轮重组装，演化机制不受影响。
        """
        turns = self.seed_turns
        if not turns:
            return []
        history: List[Dict[str, Any]] = []
        for turn in turns:
            user = (turn.get("user") or "").strip()
            answer = (turn.get("answer") or "").strip()
            imgs = [x for x in (turn.get("images") or []) if x]
            if user or imgs:
                history.append({"role": "user",
                                "content": build_content(user, imgs,
                                                         base_dir=self.ws.root)})
            if answer:
                history.append({"role": "assistant", "content": answer})
        if history:
            self.rec.record("system", summary=f"跨轮记忆：播种会话内 {len(turns)} 轮对话",
                            data={"action": "seed_session_turns",
                                  "turns": len(turns), "messages": len(history)})
            self.progress.note(f"跨轮记忆：播种会话内 {len(turns)} 轮对话")
        return history

    # ---------------------------------------------------------------- 主循环
    def run(self) -> str:
        while not self.finished and self.iteration < self.max_iterations:
            self.iteration += 1
            self.rec.begin_step()
            self.progress.step(self.iteration, self.max_iterations)
            self._apply_pending_compactions()
            self._auto_compact()
            self._inject_images()

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
            self.progress.llm_start(self.llm.config.model)
            result = self.llm.chat(messages, tools=self.reg.schemas() or None)
            msg = result.get("message") or {}
            self.progress.llm_done(result.get("elapsed_ms", 0.0), result.get("usage"),
                                   result.get("error"))
            self.rec.record_llm(result.get("raw_request", {}), result.get("raw_response", {}),
                                result.get("elapsed_ms", 0.0), result.get("usage"),
                                result.get("error"))

            if result.get("error"):
                # 被 max_tokens 截断：这是**可恢复**的 —— tool_calls 极可能是半截
                # 非法 JSON，但模型的思路还在。温和地让它重发本条消息（而非杀死
                # 整场任务，那会白烧掉前面所有步的成本）。
                if _is_truncation(result):
                    self.truncations += 1
                    self.rec.record("error", summary="模型输出被截断，请重发",
                                    data={"error": result["error"],
                                          "attempt": self.truncations,
                                          "max": self.max_truncations})
                    if self.truncations <= self.max_truncations:
                        self.last_action = (
                            f"你上一条回复被 max_tokens={self.llm.config.max_tokens} 截断了"
                            f"（finish_reason=length），tool_calls 可能是半截非法 JSON。"
                            f"请**重发同样的一条消息**，但更精简：只调用最必要的一个工具，"
                            f"或把长内容拆成几步；不要重复已完成的步骤。")
                        self.progress.note(f"输出被截断，已要求重发（{self.truncations}/{self.max_truncations}）")
                        continue
                    self.answer = (f"[未能完成] 模型连续 {self.truncations} 次因 max_tokens 截断输出，"
                                   f"请调大 max_tokens（config.yaml 的 llm.max_tokens）后重试。")
                    break
                self.rec.record("error", summary="LLM 调用失败", data={"error": result["error"]})
                self.answer = f"[未能完成] 模型调用失败：{result['error']}"
                break

            content = content_text(msg.get("content")).strip()
            tool_calls = msg.get("tool_calls") or []

            # 记下模型说的话（reasoning 也是工作记录的一部分）
            if content:
                self.progress.thought(content)
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
        pending_images: List[Dict[str, Any]] = []
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
                self.progress.note(f"跳过 {name}（已 finish）")
                summaries.append(f"{name} → （跳过：已 finish）")
                continue

            args = _parse_args(raw_args)
            started = time.time()
            self.progress.tool_start(name, args)
            output, error = self._execute(name, args)
            elapsed = (time.time() - started) * 1000

            # 工具返回带图结果：tool 消息只能承载文本，图片另补一条 user 消息。
            # 先归一化成 (文本, 图片清单)，后续一切按文本处理。
            # 先登记，等本批全部 tool 消息补齐后再统一追加（保持 assistant → tool* 的合法序列）。
            call_images: List[str] = []
            if isinstance(output, ImageResult):
                call_images = list(output.images)
                if call_images:
                    pending_images.append({"text": output.brief(), "images": call_images,
                                           "detail": output.detail})
                output = output.brief()
            output = output if isinstance(output, str) else str(output or "")

            self.progress.tool_done(name, elapsed, len(output), error)

            self.rec.record(
                "tool",
                summary=f"{name}({_short_args(args)})",
                data={"name": name, "arguments": args, "output": _clip(output),
                      "error": error, "call_id": call_id, "images": call_images},
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
        for item in pending_images:
            self._append_image_message(item["text"], item["images"], item["detail"])

    def _append_image_message(self, text: str, images: List[str],
                              detail: str = DEFAULT_DETAIL) -> None:
        """把一批图片作为一条 user 消息追加进 history —— 这是模型「看见」的唯一通道。"""
        if not images:
            return
        content = build_content(
            (text or "").strip() or "以下是图片（工具刚生成的）：", images,
            detail=detail or DEFAULT_DETAIL, base_dir=self.ws.root)
        self.history.append({"role": "user", "content": content})
        n = count_images(content)
        self.rec.record("note", summary=f"注入 {n} 张图片供模型查看",
                        data={"action": "inject_images", "count": n,
                              "sources": images, "detail": detail or DEFAULT_DETAIL})
        self.progress.note(f"注入 {n} 张图片供模型查看")

    def _inject_images(self) -> None:
        """把本轮的「任务文本 + 图片」作为一条 user 消息注入 history（只注入一次）。

        刻意**不复用**跨轮播种的旧 user 消息：那些是历史轮次，本轮任务
        与它们不是同一条。注入放在 history 末尾 —— 它才是「当前这一问」。
        """
        if self._images_injected or not self.current_images:
            return
        text = (self.task_text or "").strip() or "（图片输入）"
        self.history.append({
            "role": "user",
            "content": build_content(text, self.current_images, base_dir=self.ws.root),
        })
        self._images_injected = True
        n = len(self.current_images)
        self.rec.record("note", summary=f"本轮输入含 {n} 张图片",
                        data={"action": "seed_images", "count": n,
                              "sources": self.current_images})
        self.progress.note(f"本轮输入含 {n} 张图片")

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
            # 参数不匹配是最常见的模型失误：**一步**把正确签名回给模型，
            # 别让它再多调一次 list_functions 才知道自己错在哪。
            return "", (f"参数不匹配：{exc}\n正确签名：{tool.signature_hint()}\n"
                        f"（可用参数：{', '.join(tool.parameters) or '无'}；"
                        f"必填：{', '.join(tool.required) or '无'}）")
        except Exception as exc:  # noqa: BLE001
            import traceback
            return "", f"执行异常 {type(exc).__name__}: {exc}\n{traceback.format_exc()[-800:]}"
        if out is None:
            return "[ok]（无返回值）", None
        if isinstance(out, ImageResult):
            return out, None      # 保住类型：上层要拆出图片另发 user 消息
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
        self.progress.note(f"上下文超预算（约 {base} 字符），自动折叠 step {lo}-{hi}")
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


def _is_truncation(result: Dict[str, Any]) -> bool:
    """判断这次 LLM 结果是不是「被 max_tokens 截断」——这是可恢复的错误。

    优先用 llm 层给的 error_kind（明确信号）；退回看 finish_reason / 错误文本，
    好让 agent 在旧版 LLM 返回上也能识别。
    """
    if result.get("error_kind") == "truncated":
        return True
    if result.get("finish_reason") == "length":
        return True
    err = str(result.get("error") or "").lower()
    return "finish_reason=length" in err or "被截断" in err


def _estimate_chars(history: List[Dict[str, Any]]) -> int:
    """粗略估算一条 messages 的字符体量（1 token ≈ 2 字符就够了）。

    多模态 content（list）由 multimodal.estimate_content_chars 折算：
    图片按固定成本计，**不**按 base64 实际长度 —— 否则一张图就撑爆预算。
    """
    total = 0
    for m in history:
        total += estimate_content_chars(m.get("content"))
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
