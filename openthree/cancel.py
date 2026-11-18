"""取消令牌：让「调用方取消」从一句状态变成真的停手。

判据不是「状态变成 canceled」，而是：**此后不再发出新的 LLM 请求、不再发起
新的工具调用**。这条可以数 `logs/<session>.json` 里的 `llm` / `tool` 事件来验证。

Python 杀不了线程（没有 `Thread.kill`；`PyThreadState_SetAsyncExc` 只在字节码
边界生效，阻塞在 `socket.read` / `subprocess.wait` 里收不到，注入点还可能落在
`with` 块中间 —— 锁不放、文件写一半）。所以本模块只做两件事：

1. **检查点**：工作线程走到它们时自己退出（`check()`）。这一步覆盖「步首 /
   工具之间 / 轮间」。
2. **拆资源**：叫不醒一个阻塞的线程，但它等的那个东西能从外边拆掉 ——
   socket 用 `shutdown(SHUT_RDWR)`，子进程用 `kill()`（能立刻唤醒阻塞中的
   `communicate`）。这一步覆盖「正在等模型返回」与「卡在一条命令里」。

`request()` 幂等、线程安全、不可重置：一次运行一个令牌，旧 run 的取消绝不会
误伤新 run。

一条前提要写在这里，将来改动它的人必须重审本模块：**进程内任一时刻最多一个
在跑的 ReAct**（桥只有一条工作线程，控制台与频道共用 `_run_lock`）。`_RUNNING`
按 task_id 单值登记就建立在这条前提上；真正并发跑两个 ReAct 时，它要先改。
"""
from __future__ import annotations

import socket as _socket
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple


class Cancelled(Exception):
    """这一轮被调用方取消 —— 不是错误，是「奉命收工」。

    调用方要把它与「运行时异常」分开看：前者是奉命，后者是故障。所以它在
    `runtime._run_round` 与 `agent._execute` 里都必须排在 `except Exception`
    **之前** —— 顺序反了，取消就会被记成一条错误。
    """

    def __init__(self, reason: str = "") -> None:
        self.reason = str(reason or "")
        super().__init__(self.reason or "已按调用方要求停止")


class _Hook:
    """一次「取消时拆掉我在等的资源」的登记。

    `done` 让登记方能在收尾时**有界地**等一下（例如 `run_shell` 等清进程树的
    结果），而不是猜它做完了没有。
    """

    __slots__ = ("fn", "done")

    def __init__(self, fn: Callable[[], Any]) -> None:
        self.fn = fn
        self.done = threading.Event()


def _spawn(hook: _Hook) -> None:
    """在后台线程里跑一个 killer。

    **不能同步跑在 `request()` 里**：清进程树的 `taskkill` 自带 10s 上限，同步
    执行会让 `tasks/cancel` 的 HTTP 响应最多晚 10 秒 —— 而调用方要的正是「立刻」。
    """

    def _run() -> None:
        try:
            hook.fn()
        except Exception:  # noqa: BLE001 - 拆资源失败不该反过来炸掉取消流程
            pass
        finally:
            hook.done.set()

    threading.Thread(target=_run, name="cancel-killer", daemon=True).start()


class CancelToken:
    """一次运行的取消请求：幂等、线程安全、不可重置（一轮一个）。

    `task_id` 用来让频道层找到「正在跑的那一个」（见 `running_of`）：
    follow up 到达时 `task["_cancel"]` 已经换成新令牌，而卡住的可能是上一轮。
    """

    def __init__(self, *, task_id: str = "") -> None:
        self.task_id = str(task_id or "")
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._reason = ""
        self._killers: List[_Hook] = []
        self._in_flight: Tuple[str, float] = ("", 0.0)
        # 「请求过」与「真的被中止了」是两件事：取消可能落在最后一个检查点之后，
        # 那一轮会正常 finish 并交出完整答案。把它记成 canceled 会让调用方丢掉
        # 一个合法答案，所以落终态要用这个标志，而不是 `requested`。
        self._aborted = False

    # --- 读 -----------------------------------------------------------
    @property
    def requested(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def aborted(self) -> bool:
        """这一轮**真的**被中止了（`Cancelled` 冒到 `Agent.run` 之外）。

        与 `requested` 的区别是实打实的：取消落在最后一个检查点之后时，那一轮
        会正常 finish、交出完整答案 —— 此时 `requested` 为真而 `aborted` 为假，
        落终态就该是 `completed`。
        """
        return self._aborted

    def mark_aborted(self) -> None:
        """由 `Agent.run` 在 `Cancelled` 冒出时调用（只此一处，避免多处各判一遍）。"""
        self._aborted = True

    def wait(self, timeout: Optional[float] = None) -> bool:
        """等到「取消」被请求；返回 True 表示已请求。

        重试退避用它代替 `time.sleep` —— 否则一次取消要多等一个 backoff
        （最长 30s），而那张上界表上写的是「毫秒级」。
        """
        return self._event.wait(timeout)

    # --- 请求 ---------------------------------------------------------
    def request(self, reason: str = "") -> bool:
        """第一次调用返回 True（并拆掉在飞的资源），之后返回 False。"""
        with self._lock:
            if self._event.is_set():
                return False
            self._reason = str(reason or "调用方要求停止")
            # 先置位再拆资源：检查点与 wait() 立刻可见，不必等 killer 起完线程。
            self._event.set()
            killers = list(self._killers)
        abort_owned_by(self)              # 关掉本令牌名下的在飞 socket（毫秒级）
        for hook in killers:
            _spawn(hook)                  # 子进程这类可能慢的，交给后台线程
        return True

    def check(self) -> None:
        """已请求 → raise Cancelled。检查点只调这一句。"""
        if self._event.is_set():
            raise Cancelled(self._reason or "已按调用方要求停止")

    # --- 可杀资源 -----------------------------------------------------
    def attach_killer(self, fn: Callable[[], Any]) -> _Hook:
        """登记「取消时拆掉我在等的资源」。

        工具作者的契约：会长时间阻塞的工具（自开 socket、自己 `urlopen`、
        自己 `join` 子进程）应当把**可杀的那个资源**交出来。不交的工具，取消
        对它的上界就是「它自己结束」—— 那是契约，不是缺陷。
        """
        hook = _Hook(fn)
        with self._lock:
            self._killers.append(hook)
            already = self._event.is_set()
        if already:
            _spawn(hook)                  # 取消比登记更早到达：立刻拆
        return hook

    def detach_killer(self, hook: _Hook) -> None:
        with self._lock:
            try:
                self._killers.remove(hook)
            except ValueError:
                pass

    # --- 可观测 -------------------------------------------------------
    def in_flight(self, name: str = "", started: Optional[float] = None) -> None:
        """记下此刻在执行哪个工具 —— 取消时才能说清「卡在哪」。"""
        with self._lock:
            if name:
                self._in_flight = (str(name), time.time() if started is None else started)
            else:
                self._in_flight = ("", 0.0)

    def in_flight_text(self) -> str:
        """「run_shell（已 12.3s）」或空串。"""
        with self._lock:
            name, started = self._in_flight
        if not name:
            return ""
        return f"{name}（已 {max(0.0, time.time() - started):.1f}s）"


class _NullToken(CancelToken):
    """永不触发的令牌：没有取消通道时用它，行为与改造前逐字节相同。

    单次任务（`openthree "…"`）、`-i`、所有既有测试都走这一条 —— 于是
    `cancel=None` 不需要在调用处写一堆 `if`。
    """

    def request(self, reason: str = "") -> bool:  # noqa: D401 - 契约：永不起火
        return False


NULL = _NullToken()


# ---------------------------------------------------------------- 正在跑的那一个
_LOCAL = threading.local()
_RUN_LOCK = threading.Lock()
_RUNNING: Dict[str, CancelToken] = {}


def current() -> Optional[CancelToken]:
    """当前线程绑定的令牌（没有则 None）。

    **线程本地**，所以 `LLM.chat` / `post_json` / `run_shell` 的签名一个字节都
    不用改，就地取得到令牌 —— 工具 schema 因此也不会多出一个 `cancel` 参数。
    """
    return getattr(_LOCAL, "token", None)


def bind(tok: Optional[CancelToken]) -> None:
    """`Agent.run()` 起跑前绑定：写线程本地，同时登记「这个 task 正在跑的是它」。"""
    _LOCAL.token = tok
    if tok is not None and tok.task_id:
        with _RUN_LOCK:
            _RUNNING[tok.task_id] = tok


def unbind(tok: Optional[CancelToken]) -> None:
    """只解绑自己 —— 防误清别人的（同一个线程将来若复用，这条是安全网）。"""
    if getattr(_LOCAL, "token", None) is tok:
        _LOCAL.token = None
    if tok is not None and tok.task_id:
        with _RUN_LOCK:
            if _RUNNING.get(tok.task_id) is tok:
                _RUNNING.pop(tok.task_id, None)


def running_of(task_id: str) -> Optional[CancelToken]:
    """这个任务此刻**正在跑**的令牌；没在跑则 None。"""
    if not task_id:
        return None
    with _RUN_LOCK:
        return _RUNNING.get(str(task_id))


# ---------------------------------------------------------------- 在飞的 socket
_LIVE_LOCK = threading.Lock()
_LIVE: Dict[int, Tuple[Any, Optional[CancelToken]]] = {}   # id(sock) → (sock, owner)
_ABORTED: set = set()                                      # 被我们自己关掉的 socket


def watch(sock: Any, owner: Optional[CancelToken] = None) -> None:
    """登记一个在飞 socket。`owner` 省略时取当前线程绑定的令牌。

    **必须带 owner**：不带的话「取消一个排队中的任务」会把别人正在等的响应
    一起掐断（`-i` 控制台与 A2A 共用一条工作线程，但令牌可以不止一个）。

    **已请求的令牌要当场拆**：取消可能比登记更早到达（`connect()` 的那几秒里、
    或检查点与 `chat()` 之间）。只登记不检查的话，那个 socket 会一直等着，
    直到 `read()` 自己超时 —— 「取消之后不再发新请求」这条保证就破了。
    """
    if sock is None:
        return
    if owner is None:
        owner = current()
    with _LIVE_LOCK:
        _LIVE[id(sock)] = (sock, owner)
        already = owner is not None and bool(owner.requested)
        if already:
            _ABORTED.add(id(sock))
    if already:
        _shutdown(sock)


def unwatch(sock: Any) -> None:
    if sock is None:
        return
    with _LIVE_LOCK:
        _LIVE.pop(id(sock), None)
        _ABORTED.discard(id(sock))    # 不 discard 会随长跑缓慢涨


def was_aborted(sock: Any) -> bool:
    """这个 socket 是被我们（取消）关掉的，还是真的网络故障？

    区分它才能决定「抛 Cancelled」还是「抛 TransportError」—— 抛错了，一次
    奉命取消会被记成一次网络故障并重试。
    """
    if sock is None:
        return False
    with _LIVE_LOCK:
        return id(sock) in _ABORTED


def abort_owned_by(tok: CancelToken) -> int:
    """只 shutdown **这个令牌的** socket，返回掐断的个数。"""
    with _LIVE_LOCK:
        targets = [s for (s, owner) in _LIVE.values() if owner is tok]
        for sock in targets:
            _ABORTED.add(id(sock))
    for sock in targets:
        _shutdown(sock)
    return len(targets)


def _shutdown(sock: Any) -> None:
    """`shutdown` 让另一个线程里的 `read()` 立刻抛；`close()` 做不到这点
    （它要等读线程让出同一把锁）。"""
    try:
        sock.shutdown(_socket.SHUT_RDWR)
    except Exception:  # noqa: BLE001 - 未连接 / 已关闭 / 测试里的假 socket
        pass
