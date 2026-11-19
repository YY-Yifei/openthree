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

3. **状态可见**：排队、开跑、跑完、出错、被取消 —— 这些**只有这里知道**，
   所以由这里告诉频道（`Channel.notify`）。语音那头「卡了很久」而在打字的人看来
   跟死机一样，几乎总是这条缝漏了：桥是**一条**工作线程，前一条跑十分钟，
   后一条就干等十分钟，中间一个字的回声都没有。
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Any, Callable, Dict, Optional, Sequence

from ..attach import TURN_SLOT
from ..kernel import KERNEL
from .base import InboundMessage

# 一条消息在队列里可以等多久（秒）；超时说明智能体被别的活占住了
DEFAULT_QUEUE_TIMEOUT = 3600.0

# bridge 自己塞给用户的话（**不是**模型的结论）统一以这个前缀开头。
# 它是一条**文案**约定（让人一眼看出这不是答案），不是判定依据：频道层判的是
# 桥留下的带外标记 `msg.raw["notice"]`（见 `_reply_quietly`）—— 前缀能被模型
# 原样说出来，标记不能。
NOTICE_PREFIX = "[openthree] "

# 收工时还排在队列里的消息，会被回这样一句 —— 不是静默丢弃：
# 发消息的人该知道「要重发」，而不是盯着对话框等一个永远不来的回答。
SHUTDOWN_NOTICE = NOTICE_PREFIX + "正在退出，这条消息没有处理，请稍后重发。"


def _final_state(status: str, delivered: bool, spent: float) -> tuple:
    """运行期判词 + 「答复送出去了没有」→ 频道要显示的终态。

    `status` 来自 `Runtime.run()` 的会话文档：`done` / `unfinished` / `error` /
    `interrupted` / `canceled`（空 = 当作正常返回）。**不能只看「函数返回了」**：
    跑挂的那条也会带着一句话（`[运行时异常] …`）正常返回，只按返回判定就会把
    一次失败标成 ✅ —— 那是在报告里撒谎。

    `unfinished`（撞到步数上限没 finish）仍算「有答复」：答案是完整的，只是这活
    没收尾 —— 标 ⚠ 会让人以为出错了，所以在细节里写清，状态仍是完成。
    """
    used = f"用时 {spent:.0f}s"
    if status == "error":
        return "error", used
    if status in ("canceled", "interrupted"):
        return "canceled", used
    if not delivered:
        return "error", "答复没发出去"
    if status == "unfinished":
        return "done", f"{used}（未收尾）"
    return "done", used


def compose_input(msg: InboundMessage) -> str:
    """把频道消息拼成喂给智能体的一段文本（来源标注 + 原文 + 图片说明）。

    图片本身走 `Runtime.run(images=…)` 进 content；这里只补一句「附带了几张」——
    否则一条纯图消息喂过去就是一句「请直接回复：」加一片空白。
    """
    text = str(msg.text or "").strip()
    images = [i for i in (msg.images or []) if i]
    if images:
        n = len(images)
        text = f"{text}\n\n（本条消息附带 {n} 张图片）" if text else f"（附带 {n} 张图片）"
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
        # 控制台那条不记在 `_pending` 里，但它占着 `_run_lock` 时，排队的消息同样
        # 得等它。用一个显式计数而不是 `_run_lock.locked()`：那把锁也可能正被
        # **频道自己的工作线程**持有（`ask_doc` 全程持锁），那种情况下正在跑的那条
        # 已经算进 `_pending` 了，再按「锁被占」加一就会重复计数。
        self._console = 0

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
            self._notify(reply, msg, "skipped", "正在退出")
            self._reply_quietly(msg, reply, SHUTDOWN_NOTICE)
            return
        # 计数、通知、入队**在同一把锁里**做完，两个原因：
        #   * 先通知再入队 —— 入队会立刻唤醒工作线程，它可能在下一行之前就把
        #     `running` 发出去，那样「已接收 / 排队中」会晚于「处理中」到达，
        #     对方看到的表情会先 🤔 再退回 ⏳；
        #   * 三者不分开 —— 两个收消息线程并发提交时，先取号的那个可能后入队，
        #     于是一个人被告知「已接收」而它其实排在后面。锁内做完，报的队位
        #     就等于真实队位。`notify()` 的契约是**立刻返回**，所以它可以待在锁里。
        with self._pending_lock:
            self._pending += 1
            # 前面还有几条：正在跑 / 排在前面的频道消息，加上控制台占着锁时的那一条。
            ahead = self._pending - 1 + self._console
            if ahead:
                self._notify(reply, msg, "queued", f"前面还有 {ahead} 条")
            else:
                self._notify(reply, msg, "accepted")
            self._queue.put((msg, reply))

    def ask(self, text: str, *, images: Optional[Sequence[str]] = None,
            cancel: Any = None, observer: Any = None,
            on_start: Optional[Callable[[], None]] = None) -> str:
        """同步跑一条：立刻执行、返回答案。

        只要句子本身时用它；要判断「这条跑成了没有」（`done` / `unfinished` /
        `error` / `canceled`）就用 `ask_doc()` —— 那是同一件事的完整结果。
        """
        return str(self.ask_doc(text, images=images, cancel=cancel,
                                observer=observer, on_start=on_start).get("answer") or "")

    def ask_doc(self, text: str, *, images: Optional[Sequence[str]] = None,
                cancel: Any = None, observer: Any = None,
                on_start: Optional[Callable[[], None]] = None) -> Dict[str, Any]:
        """同步跑一条，返回**整份会话文档**（`answer` 与 `status` 都在里面）。

        `status` 是运行期对这一次的判词：`done`（模型收尾）/ `unfinished`（撞到步数
        上限没 finish）/ `error` / `interrupted` / `canceled`。**它不能被 `answer`
        替代** —— 跑挂的那条也会有一句话（`[运行时异常] …`），只取正文就会把一次
        失败当成一次成功。

        `images` 是本地路径或 http(s) 链接 —— 与 CLI `--image` 走同一条路：
        `Runtime.run(images=…)` → `stage_images()` → 模型的多模态 `content`。

        `cancel` 是这一条消息的取消令牌；`observer` 是这一条的进度观察者
        （频道想把自己那边显形时才给）。两个都只在非空时才往 `run()` 里传 ——
        于是既有的 rt 替身（测试里的假 Runtime）不必认识这些参数。

        `on_start` 在**真的拿到那把运行锁之后**、调 `run()` 之前触发：控制台与频道
        共用这把锁，先报到「开始处理」再干等锁，界面上就是「处理中 0s」定格几分钟。
        """
        imgs = [str(i) for i in (images or []) if i]
        extra: Dict[str, Any] = {}
        if cancel is not None:
            extra["cancel_token"] = cancel
        if observer is not None:
            extra["observer"] = observer
        with self._run_lock:
            if on_start is not None:
                try:
                    on_start()
                except Exception:  # noqa: BLE001 - 报到失败不该挡住这条消息
                    pass
            doc = self.rt.run(text, session=True, images=imgs or None, **extra)
        return doc if isinstance(doc, dict) else {"answer": str(doc or "")}

    def under_lock(self, fn: Callable[[], Any]) -> Any:
        """在「同一把锁」下跑一段调用（控制台要保留 --auto / 图片等语义时用它）。

        频道的工作线程与控制台共用这把锁：两边都是「一个用户的一句话」，
        不该同时跑两条 ReAct 循环。跑的时候记一笔 `_console`：控制台那条不在
        `_pending` 里，排队位的读数要把它算进去（见 `submit`）。
        """
        with self._run_lock:
            with self._pending_lock:
                self._console += 1
            try:
                return fn()
            finally:
                with self._pending_lock:
                    self._console -= 1

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
        # 「当前回合」发布给 `attach_file`：工具与 `Runtime.run` 跑在**同一个线程**
        # 里，所以它拿到的正是这条 msg，直接往 `msg.attachments` 上挂文件。
        # 只有**声明支持文件**的频道才发布（`reply` 是 `channel.send_text` 这个绑定
        # 方法，`__self__` 带回频道实例）：微信 / 钉钉回合里槽不存在，工具会老实回
        # 「这次答复发不出文件」，而不是假装附上了。
        # 键里带线程 id：同进程多频道共用一份 `KERNEL.state`，没有它就会串台。
        channel = getattr(reply, "__self__", None)
        slot = (TURN_SLOT, threading.get_ident())
        carrier = bool(getattr(channel, "supports_files", False))
        if carrier:
            KERNEL.state[slot] = msg
        try:
            # 还在队列里就被取消：一次 ReAct 都不跑。不让「取消」在排队场景下变成
            # 「取消了个寂寞，它还是烧了 200 步」—— 令牌在 `_rpc_send` 建好时就
            # 跟着消息进来了，这里只是看一眼。
            tok = getattr(msg, "cancel", None)
            if tok is not None and getattr(tok, "requested", False):
                self._notify(reply, msg, "canceled", "开跑前已被取消")
                self._reply_quietly(
                    msg, reply,
                    NOTICE_PREFIX + "这条消息在开跑前已被调用方取消，未执行。")
                return
            if self.echo:
                print(f"\n[频道] ◀ {msg.label()}：{msg.text[:200]}", flush=True)
            # 「开始处理」在拿到运行锁之后才报（见 `ask_doc(on_start=…)`）：
            # 控制台正跑着一条时，这条其实还在等锁 —— 先亮 🤔 再一动不动，
            # 正是这次要治的那种「看着像卡死」。
            marks = {"at": time.time()}

            def _started() -> None:
                marks["at"] = time.time()
                self._notify(reply, msg, "running")

            doc = self.ask_doc(compose_input(msg), images=msg.images, cancel=tok,
                               observer=self._observer_of(channel, msg),
                               on_start=_started)
            spent = time.time() - marks["at"]
            answer = str(doc.get("answer") or "")
            # 运行期的判词要在**送答案之前**交给频道。跑挂的那条也会带着一句话
            # （`[运行时异常] …`）正常返回，只按「答复非空」判，频道会把它记成一次
            # 成功 —— 这正是 `_final_state()` 要挡住的那种「把失败报成成功」。
            # 键名与 `notice` 分开：`notice` 的意思是「这不是结论」，`verdict` 是
            # 「这份结论的成色」。不认识它的频道行为一字不变。
            try:
                if isinstance(msg.raw, dict):
                    msg.raw["verdict"] = str(doc.get("status") or "")
            except Exception:  # noqa: BLE001 - 标记写不上不该挡住回复
                pass
            if self.echo:
                print(f"[频道] ▶ {msg.label()}：{answer[:200]}", flush=True)
            # `False` 是频道**明确**说「没送到」（webhook 过期 / 非 2xx）；
            # `None` 是「送到了，或者这个渠道没法判断」—— 既有的 send_text
            # 都返回 None，于是它们一字不改、行为照旧。
            delivered = True
            try:
                if reply(msg, answer) is False:
                    delivered = False
                    print(f"[频道] 回复没送到（{msg.label()}）：频道报了失败",
                          flush=True)
            except Exception as exc:  # noqa: BLE001 - 发送失败不该杀死工作线程
                delivered = False
                print(f"[频道] 回复发送失败（{msg.label()}）：{exc}", flush=True)
            # 先把答案送出去，再亮终态：顺序反过来的话，对方会先看到 ✅ 再等正文。
            # 终态由**运行期的判词**决定，不由「函数正常返回」决定：跑挂的那条也会
            # 有一句话（`[运行时异常] …`），只按「返回了」标 ✅ 就是在报告里撒谎。
            state, detail = _final_state(str(doc.get("status") or ""), delivered, spent)
            self._notify(reply, msg, state, detail)
        except Exception as exc:  # noqa: BLE001 - 智能体出错也只影响这一条
            self._notify(reply, msg, "error", f"{type(exc).__name__}: {exc}"[:120])
            self._reply_quietly(msg, reply, f"{NOTICE_PREFIX}这条消息没跑通：{exc}")
        finally:
            if carrier:
                KERNEL.state.pop(slot, None)
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
                # 这条已经被报成「已接收 / 排队中」，收工时得给它一个终态 ——
                # 否则它那枚 👀 / ⏳ 会永远停在那里，看起来像还在跑。
                self._notify(reply, msg, "canceled", "收工时仍在排队")
                self._reply_quietly(msg, reply, notice)
            finally:
                with self._pending_lock:
                    self._pending = max(0, self._pending - 1)
                self._queue.task_done()
        return dropped

    @staticmethod
    def _channel_of(reply: Callable[[InboundMessage, str], None]) -> Any:
        """回复回调背后那个频道实例（`reply` 一般是 `channel.send_text`）。"""
        return getattr(reply, "__self__", None)

    @classmethod
    def _notify(cls, reply: Callable[[InboundMessage, str], None],
                msg: InboundMessage, state: str, detail: str = "") -> None:
        """把状态告诉频道 —— 频道没声明支持、或者它自己炸了，都当没发生。

        与 `Progress._tell` 同一条纪律：**展示出问题绝不能影响 ReAct**。
        """
        channel = cls._channel_of(reply)
        if channel is None or not getattr(channel, "supports_status", False):
            return
        fn = getattr(channel, "notify", None)
        if fn is None:
            return
        try:
            fn(msg, state, detail)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _observer_of(channel: Any, msg: InboundMessage) -> Any:
        """频道为这条消息准备的进度观察者；不支持的频道给 None（`run()` 不收）。"""
        fn = getattr(channel, "progress_for", None) if channel is not None else None
        if fn is None:
            return None
        try:
            return fn(msg)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _reply_quietly(msg: InboundMessage,
                       reply: Callable[[InboundMessage, str], None], text: str) -> None:
        """回一句就走 —— 发不出去也不能挡住收工。

        这句话**不是模型的结论**，所以在消息上留一个带外标记（`raw["notice"]`）：
        频道层据此把它记成「没跑成」，而不是一次"成功"的空回答。光看文本前缀不够
        —— 模型完全可能把 `[openthree] ` 当作答案的开头（比如它正在解释这个前缀）。
        """
        try:
            if isinstance(msg.raw, dict):
                msg.raw["notice"] = text
        except Exception:  # noqa: BLE001 - 标记写不上也不该挡住回复
            pass
        try:
            reply(msg, text)
        except Exception:  # noqa: BLE001
            pass

    # --- 观测 ---------------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        return {"queued": self._queue.qsize(), "running": self._pending > 0}
