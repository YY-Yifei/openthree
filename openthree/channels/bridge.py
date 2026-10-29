"""桥：频道消息 → 智能体 → 回复。

这一层是「频道」与「内核」之间唯一的一道缝，判据很简单：
**它做的事与在 `-i` 里敲一行字完全一样** —— 调 `Runtime.run(text, session=True)`，
把 `doc["answer"]` 拿回来。所以内核（agent / llm / runtime）一行都不用改。

两件事必须在这一层做对：

1. **串行**：`Runtime` 不是线程安全的（会话、工具、工作区都在一个进程里），
   而多个频道 + 控制台会同时想说话。所以一条消息进来先排队，由一个工作线程
   一次一条地跑 —— 「不同渠道的用户就像在同一个终端里轮流打字」。
2. **来源可见**：喂给智能体的文本要带上「谁、从哪个渠道说的」，否则它分不清是
   张三在微信里问还是群里在问。回复仍然只把答案送回去，不带这层包装。
"""
from __future__ import annotations

import queue
import threading
from typing import Any, Callable, Dict, Optional

from .base import InboundMessage

# 一条消息在队列里可以等多久（秒）；超时说明智能体被别的活占住了
DEFAULT_QUEUE_TIMEOUT = 3600.0

# 收工时还排在队列里的消息，会被回这样一句 —— 不是静默丢弃：
# 发消息的人该知道「要重发」，而不是盯着对话框等一个永远不来的回答。
SHUTDOWN_NOTICE = "[openthree] 正在退出，这条消息没有处理，请稍后重发。"


def compose_input(msg: InboundMessage) -> str:
    """把频道消息拼成喂给智能体的一段文本（来源标注 + 原文）。"""
    text = str(msg.text or "").strip()
    return f"来自{msg.label()}的消息，请直接回复：\n\n{text}"


class AgentBridge:
    """把频道消息喂进 `Runtime`，把答案送回去。

    Args:
        rt: 一个 `Runtime` 实例（与 `-i` 用的是同一个）。
        on_answer: 每条回答的回调（默认 `channel.send_text`）—— 由 manager 注入。
        echo: 是否把每条进来的消息打到终端（跟着控制台一起看，排查最省事）。
        announce: 一段提示（被注入到每条用户文本前）—— 留空则不注入。
    """

    def __init__(self, rt: Any, *, echo: bool = True,
                 announce: str = "", queue_timeout: float = DEFAULT_QUEUE_TIMEOUT) -> None:
        self.rt = rt
        self.echo = echo
        self.announce = announce
        self.queue_timeout = float(queue_timeout)
        self._queue: "queue.Queue[Optional[tuple[InboundMessage, Callable[..., None]]]]" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._run_lock = threading.Lock()
        self._closed = threading.Event()
        self._pending = 0
        self._pending_lock = threading.Lock()

    # --- 生命周期 -----------------------------------------------------
    def start(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._closed.clear()
        self._worker = threading.Thread(target=self._loop, name="channel-bridge", daemon=True)
        self._worker.start()

    def stop(self, *, timeout: float = 5.0) -> bool:
        """收工：让工作线程处理完**手头这条**然后退出。

        还排在队列里的一条都不跑 —— 但也不静默消失：逐条回一句 `SHUTDOWN_NOTICE`，
        让发消息的人知道要重发。

        返回「工作线程是否真的停住了」。False 表示手头那条还在跑（长任务），
        调用方不该在这时候收会话 —— 那是两个线程同时动同一份会话。
        """
        self._closed.set()
        self._drop_pending(SHUTDOWN_NOTICE)
        self._queue.put(None)
        worker, self._worker = self._worker, None
        if worker is None:
            return True
        worker.join(timeout=timeout)
        return not worker.is_alive()

    @property
    def busy(self) -> bool:
        return self._pending > 0

    # --- 入口 ---------------------------------------------------------
    def submit(self, msg: InboundMessage, reply: Callable[[InboundMessage, str], None]) -> None:
        """把一条频道消息排进队列（由 Channel.emit 调到，必须立刻返回）。"""
        if self._closed.is_set():
            self._reply_quietly(msg, reply, SHUTDOWN_NOTICE)
            return
        with self._pending_lock:
            self._pending += 1
        self._queue.put((msg, reply))

    def ask(self, text: str, *, source: str = "控制台") -> str:
        """同步跑一条（控制台输入走这条）：立刻执行、返回答案。"""
        with self._run_lock:
            doc = self.rt.run(text, session=True)
        return str(doc.get("answer") or "")

    def under_lock(self, fn: Callable[[], Any]) -> Any:
        """在「同一把锁」下跑一段调用（控制台要保留 --auto / 图片等语义时用它）。

        频道的工作线程与控制台共用这把锁：两边都是「一个用户的一句话」，
        不该同时跑两条 ReAct 循环。
        """
        with self._run_lock:
            return fn()

    # --- 工作线程 -----------------------------------------------------
    def _loop(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:            # stop() 的哨兵：收工
                    # 收工竞态里挤进来的最后几条：同样回一句，别让它们烂在队里
                    self._drop_pending(SHUTDOWN_NOTICE)
                    return
                self._handle(*item)
            finally:
                self._queue.task_done()

    def _handle(self, msg: InboundMessage,
                reply: Callable[[InboundMessage, str], None]) -> None:
        """跑一条消息并把答案送回去；任何异常都只影响这一条。"""
        try:
            if self.echo:
                print(f"\n[频道] ◀ {msg.label()}：{msg.text[:200]}", flush=True)
            answer = self.ask(compose_input(msg))
            if self.echo:
                print(f"[频道] ▶ {msg.label()}：{answer[:200]}", flush=True)
            try:
                reply(msg, answer)
            except Exception as exc:  # noqa: BLE001 - 发送失败不该杀死工作线程
                print(f"[频道] 回复发送失败（{msg.label()}）：{exc}", flush=True)
        except Exception as exc:  # noqa: BLE001 - 智能体出错也只影响这一条
            self._reply_quietly(msg, reply, f"[openthree] 这条消息没跑通：{exc}")
        finally:
            with self._pending_lock:
                self._pending = max(0, self._pending - 1)

    def _drop_pending(self, notice: str) -> int:
        """把队列里还排着的消息逐条回一句 `notice`，返回丢掉的条数。"""
        dropped = 0
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                if item is None:
                    continue
                msg, reply = item
                dropped += 1
                self._reply_quietly(msg, reply, notice)
            finally:
                with self._pending_lock:
                    self._pending = max(0, self._pending - 1)
                self._queue.task_done()
        return dropped

    @staticmethod
    def _reply_quietly(msg: InboundMessage,
                       reply: Callable[[InboundMessage, str], None], text: str) -> None:
        """回一句就走 —— 发不出去也不能挡住收工。"""
        try:
            reply(msg, text)
        except Exception:  # noqa: BLE001
            pass

    # --- 观测 ---------------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        return {"queued": self._queue.qsize(), "running": self._pending > 0}
