"""可观测层：把模型的一切工作记录下来。

设计原则：**内存里是真相，磁盘上是投影**。
记录首先在内存里长成一棵完整的事件树，每来一条事件就增量落盘，
因此进程被 kill 也丢不掉已发生的思考。

落盘分类（`.openthree/logs/`）：

    session-<id>.json     整场会话（元信息 + 全部事件 + 统计）
    steps/step-0001.json  每一步（一个 assistant turn + 其工具结果）
    events/events.jsonl   追加式事件流，一行一事件（便于 tail / 流式消费）
    llm/llm-0001.json     每次模型调用的请求与响应原文（含耗时/token）
    evolution.jsonl       自我演化史：写 function、改提示词、压缩上下文
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .workspace import Workspace


def _now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def _jsonable(obj: Any) -> Any:
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
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

    def to_dict(self) -> Dict[str, Any]:
        d = {"seq": self.seq, "ts": self.ts, "step": self.step, "kind": self.kind,
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
        self._lock = threading.Lock()

        self.ws.logs_dir.mkdir(parents=True, exist_ok=True)
        (self.ws.logs_dir / "llm").mkdir(parents=True, exist_ok=True)
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
        doc = {
            "session_id": self.session_id,
            "model": self.model,
            "status": "done" if final else "running",
            "started_at": self.started_at,
            "ended_at": getattr(self, "ended_at", None),
            "user_input": self.user_input,
            "answer": getattr(self, "_final_answer", None),
            "stats": self.stats(),
            "steps": self.steps,
            "events": [e.to_dict() for e in self.events],
        }
        try:
            self.session_file.write_text(
                json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

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
        path = self.ws.steps_dir / f"step-{self._step:04d}.json"
        path.write_text(json.dumps(step_obj, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---------- 记录 ----------
    def record(self, kind: str, summary: str = "", data: Optional[Dict[str, Any]] = None,
               elapsed_ms: Optional[float] = None) -> Event:
        with self._lock:
            self._seq += 1
            ev = Event(seq=self._seq, ts=_now(), kind=kind, summary=summary,
                       data=_jsonable(data or {}), step=self._step, elapsed_ms=elapsed_ms)
            self.events.append(ev)
            self._step_buf.append(ev)
            self._append_line(self.events_file, ev.to_dict())
            if kind == "evolution":
                self._append_line(self.evolution_file, ev.to_dict())
        # 增量投影：即使进程随后被 kill，磁盘上也有最新状态
        self._flush_session()
        return ev

    def record_llm(self, request: Dict[str, Any], response: Dict[str, Any],
                   elapsed_ms: float, usage: Optional[Dict[str, Any]] = None,
                   error: Optional[str] = None) -> Event:
        self._llm_count += 1
        idx = self._llm_count
        payload = {
            "index": idx,
            "ts": _now(),
            "elapsed_ms": round(elapsed_ms, 1),
            "model": request.get("model"),
            "request": request,
            "response": response,
            "usage": usage or {},
            "error": error,
        }
        path = self.ws.logs_dir / "llm" / f"llm-{idx:04d}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self._append_line(self.trace_file, {
            "index": idx, "ts": payload["ts"], "elapsed_ms": payload["elapsed_ms"],
            "model": payload["model"], "usage": payload["usage"], "error": error,
        })
        return self.record(
            "llm",
            summary=f"LLM #{idx} {request.get('model','')} ({elapsed_ms/1000:.1f}s)",
            data={"index": idx, "usage": usage or {}, "file": str(path.name),
                  "messages": len(request.get("messages", [])),
                  "tools": len(request.get("tools", []) or [])},
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
        self._flush_session(final=(status == "done"))
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
