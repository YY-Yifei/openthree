"""可观测层：把模型的一切工作记录下来。

设计原则：**内存里是真相，磁盘上是投影**。
记录首先在内存里长成一棵完整的事件树，每来一条事件就增量落盘，
因此进程被 kill 也丢不掉已发生的思考。

落盘分类（`.openthree/logs/`）：

    session-<id>.json                   整场会话（元信息 + 全部事件 + 统计）
    sessions/<id>/steps/step-0001.json  每一步（一个 assistant turn + 其工具结果）
    sessions/<id>/llm/llm-0001.json     每次模型调用的请求与响应原文（含耗时/token）
    events/events.jsonl                 追加式事件流，一行一事件（便于 tail / 流式消费）
    evolution.jsonl                     自我演化史：写 function、改提示词、压缩上下文

`steps/` 与 `llm/` 的编号都是**会话内序号**，所以它们按会话分目录 —— 若放在共享
目录里，后一场会话会从 0001 重来、整体覆盖前一场（日志静默丢失）。
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .multimodal import sanitize_for_log
from .workspace import Workspace


def _now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


# session json 落盘节流（避免每条事件全量重写，O(n^2) 写放大）
FLUSH_EVERY = 5        # 满 N 条事件至少刷一次
FLUSH_INTERVAL = 2.0   # 至少间隔多少秒

# 会话的合法终态：finalize 只认这些，其余（含 "running"）一律回落成 "done"。
# 落盘的 status 必须如实：把 unfinished/interrupted/error 压成 running，
# 事后就分不清「真收尾了但状态写错」与「进程被 kill」。
_TERMINAL_STATUSES = ("done", "unfinished", "interrupted", "error")


def _jsonable(obj: Any) -> Any:
    if obj is None or isinstance(obj, (str, int, float, bool)):
        # 字符串过一道日志脱敏：图片 base64 绝不能进 logs/
        return sanitize_for_log(obj) if isinstance(obj, str) else obj
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, Path):
        return str(obj)
    return repr(obj)


@dataclass
class Event:
    seq: int
    ts: str
    kind: str                 # llm / tool / evolution / note / finish / system / error
    summary: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    step: int = 0
    elapsed_ms: Optional[float] = None
    session: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = {"seq": self.seq, "session": self.session, "ts": self.ts,
             "step": self.step, "kind": self.kind,
             "summary": self.summary, "data": self.data}
        if self.elapsed_ms is not None:
            d["elapsed_ms"] = round(self.elapsed_ms, 1)
        return d


class Recorder:
    """一场会话的记录器。"""

    def __init__(self, workspace: Workspace, session_id: Optional[str] = None,
                 user_input: str = "", model: str = "") -> None:
        self.ws = workspace
        self.session_id = session_id or workspace.new_session_id()
        self.started_at = _now()
        self.model = model
        self.user_input = user_input
        self.events: List[Event] = []
        self.steps: List[Dict[str, Any]] = []
        self._seq = 0
        self._step = 0
        self._step_buf: List[Event] = []
        self._llm_count = 0
        self._final_answer: Optional[str] = None
        self._status = "running"          # 真实终态；finalize 时落成 done/unfinished/interrupted/error
        self.ended_at: Optional[str] = None
        self._lock = threading.Lock()
        self._dirty = False
        self._flush_since = 0
        self._last_flush = time.time()

        self.ws.logs_dir.mkdir(parents=True, exist_ok=True)
        # 逐步 / 逐次 LLM 原文按会话分目录：编号是会话内序号，共享目录会跨会话覆盖。
        self.session_dir = self.ws.session_dir(self.session_id)
        self.steps_dir = self.session_dir / "steps"
        self.llm_dir = self.session_dir / "llm"
        self.steps_dir.mkdir(parents=True, exist_ok=True)
        self.llm_dir.mkdir(parents=True, exist_ok=True)
        self.session_file = self.ws.logs_dir / f"{self.session_id}.json"
        self.events_file = self.ws.events_dir / "events.jsonl"
        self.evolution_file = self.ws.logs_dir / "evolution.jsonl"
        self.trace_file = self.ws.logs_dir / "trace.jsonl"

        self._flush_session()

    # ---------- 落盘 ----------
    def _flush_session(self, final: bool = False) -> None:
        """把当前内存状态投影成一个可看的 session json。

        每次事件后都刷一遍：进程被 kill 也不丢已发生的思考。
        """
        if final:
            self._status = self._status if self._status != "running" else "done"
        doc = {
            "session_id": self.session_id,
            "model": self.model,
            "status": self._status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "user_input": self.user_input,
            "answer": self._final_answer,
            "stats": self.stats(),
            "steps": self.steps,
            "events": [e.to_dict() for e in self.events],
        }
        try:
            self.session_file.write_text(
                json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
            self._dirty = False
            self._flush_since = 0
            self._last_flush = time.time()
        except Exception:  # noqa: BLE001
            pass

    def _maybe_flush(self, force: bool = False) -> None:
        """按条数/时间节流落盘；force 时立刻写。进程被 kill 仍只丢最近几条事件。"""
        if not self._dirty:
            return
        if (force or self._flush_since >= FLUSH_EVERY
                or (time.time() - self._last_flush) >= FLUSH_INTERVAL):
            self._flush_session()

    # ---------- 步 ----------
    def begin_step(self, thought: str = "") -> int:
        self._close_step()
        self._step += 1
        self._step_buf = []
        if thought:
            self.record("note", summary=thought[:200], data={"thought": thought})
        return self._step

    def _close_step(self) -> None:
        if not self._step_buf:
            return
        step_obj = {
            "step": self._step,
            "started_ts": self._step_buf[0].ts,
            "events": [e.to_dict() for e in self._step_buf],
        }
        self.steps.append(step_obj)
        path = self.steps_dir / f"step-{self._step:04d}.json"
        path.write_text(json.dumps(step_obj, ensure_ascii=False, indent=2), encoding="utf-8")
        self._flush_session()

    # ---------- 记录 ----------
    def record(self, kind: str, summary: str = "", data: Optional[Dict[str, Any]] = None,
               elapsed_ms: Optional[float] = None) -> Event:
        with self._lock:
            self._seq += 1
            ev = Event(seq=self._seq, ts=_now(), kind=kind, summary=summary,
                       data=_jsonable(data or {}), step=self._step,
                       elapsed_ms=elapsed_ms, session=self.session_id)
            self.events.append(ev)
            self._step_buf.append(ev)
            self._append_line(self.events_file, ev.to_dict())
            if kind == "evolution":
                self._append_line(self.evolution_file, ev.to_dict())
            self._dirty = True
            self._flush_since += 1
        # 增量投影：按条数/时间节流落盘，避免每条事件全量重写
        self._maybe_flush()
        return ev

    def record_llm(self, request: Dict[str, Any], response: Dict[str, Any],
                   elapsed_ms: float, usage: Optional[Dict[str, Any]] = None,
                   error: Optional[str] = None, attempts: int = 1) -> Event:
        self._llm_count += 1
        idx = self._llm_count
        # 请求/响应原文过一道日志脱敏：图片 base64 折叠成占位符，
        # 否则一次多模态调用的日志动辄几百 KB。
        request = _jsonable(request)
        response = _jsonable(response)
        usage = _jsonable(usage or {})
        try:
            attempts = max(1, int(attempts))
        except (TypeError, ValueError):
            attempts = 1
        payload = {
            "index": idx,
            "ts": _now(),
            "elapsed_ms": round(elapsed_ms, 1),
            "model": request.get("model"),
            "request": request,
            "response": response,
            "usage": usage or {},
            "error": error,
            # 真正打到线上的次数（含重试）。>1 说明这次是重试后才拿到结果的
            # —— 事后从日志里就能看出「重试发生过」，而不只是「成功了」。
            "attempts": attempts,
        }
        path = self.llm_dir / f"llm-{idx:04d}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self._append_line(self.trace_file, {
            "index": idx, "ts": payload["ts"], "elapsed_ms": payload["elapsed_ms"],
            "model": payload["model"], "usage": payload["usage"], "error": error,
            "attempts": attempts,
        })
        retry_note = f" · {attempts} 次尝试" if attempts > 1 else ""
        return self.record(
            "llm",
            summary=f"LLM #{idx} {request.get('model','')} "
                    f"({elapsed_ms/1000:.1f}s{retry_note})",
            data={"index": idx, "usage": usage or {}, "file": str(path.name),
                  "messages": len(request.get("messages", [])),
                  "tools": len(request.get("tools", []) or []),
                  "attempts": attempts},
            elapsed_ms=elapsed_ms,
        )

    def _append_line(self, path: Path, obj: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    # ---------- 收尾 ----------
    def finalize(self, answer: str = "", status: str = "done") -> Dict[str, Any]:
        self._close_step()
        self.ended_at = _now()
        self._final_answer = answer
        self._status = status if status in _TERMINAL_STATUSES else "done"
        self._dirty = True
        self._maybe_flush(force=True)
        self._flush_session(final=True)
        return {
            "session_id": self.session_id,
            "model": self.model,
            "status": status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "user_input": self.user_input,
            "answer": answer,
            "stats": self.stats(),
            "steps": self.steps,
            "events": [e.to_dict() for e in self.events],
        }

    def stats(self) -> Dict[str, Any]:
        kinds: Dict[str, int] = {}
        for e in self.events:
            kinds[e.kind] = kinds.get(e.kind, 0) + 1
        tool_ms = sum(e.elapsed_ms or 0 for e in self.events if e.kind == "tool")
        llm_ms = sum(e.elapsed_ms or 0 for e in self.events if e.kind == "llm")
        return {
            "events": len(self.events),
            "steps": self._step,
            "by_kind": kinds,
            "tool_calls": kinds.get("tool", 0),
            "llm_calls": self._llm_count,
            "evolutions": kinds.get("evolution", 0),
            "tool_time_ms": round(tool_ms, 1),
            "llm_time_ms": round(llm_ms, 1),
        }

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Recorder {self.session_id} events={len(self.events)}>"
