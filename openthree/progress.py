"""进度回声：让 CLI 在长任务里一直有输出。

设计边界（很重要）：

    * 这是**纯展示层**，不含任何决策逻辑，不参与 ReAct，不影响回答质量。
    * Agent 只调用注入进来的 progress 对象，它并不认识「终端 / print」。
    * 不传 progress 时，Agent 走 NullProgress，行为完全一致。
    * 每一行前面带 `[HH:MM:SS]` 墙上时间，由 `stamp()` / `stamped()` 这一处产生。

同一个回声还有一个**观察者**出口（`observer=`）：终端之外的界面（钉钉这类频道）
订阅同一批事件，把「在干什么」变成对方看得见的状态。观察者是**可选**的，方法名与
`Progress` 同名同参（`step` / `llm_start` / `llm_done` / `thought` / `tool_start` /
`tool_done` / `note` / `idle`），只实现自己关心的几个即可 —— 缺的当没有，
抛异常一律被吞掉：**展示出问题绝不能影响 ReAct**。`idle` 是长等待里的心跳
（每 `heartbeat` 秒一次，带当前阶段与已等待秒数），它是「还活着」的唯一凭据。

**观察者的方法会被并发调用**（心跳线程发 `idle`，ReAct 线程发 `step` / `tool_*`），
而且 `_stop_heartbeat()` 只等 0.3 秒 —— 一枚慢到的 `idle` 可能落在 `tool_done` 之后。
所以契约是两条：方法的实现必须**线程安全**、必须**立刻返回**（要发请求就自己排队
交给后台线程，钉钉频道就是这么做的）。做不到这两条，就会既拖慢 ReAct 又让界面
显示过期阶段。

**观察者只管「过程」，不管「终态」。** 一个 `Progress` 是**一轮**的（多轮任务里每轮
新建一个、每轮收尾都 `close()`），所以 `close` 不转给观察者 —— 否则「任务结束了」
会在多轮任务的第一轮之后就发出一次，接到它的界面会先关闭再重开。任务真正的终态
（完成 / 出错 / 取消）由桥通过 `Channel.notify()` 告诉频道，那是**一次任务**的口径。

为什么需要它：一次 LLM 调用可能几十秒到几分钟，一次 run_shell 可能几十秒，
而 openthree 在开跑前说过三句话之后就彻底沉默了 —— 用户无法区分
「模型正在思考」和「进程已经卡死」。这里补上那个回声。

时间戳解决的是另一个问题：「这一行是什么时候打的」。长任务跑几十分钟时，
墙上时间比相对秒数更有用 —— 能把终端上的一行对上日志、报告与对话档案里的时间线。
固定 10 字符宽，所以时间戳那一列天然对齐。

    [14:12:03] [step 3/200]
    [14:12:03]   · 思考中（deepseek-flash）…
    [14:12:33]   · … 等待模型返回，已等待 30s        ← 心跳，长等待时周期性出现
    [14:12:41]   · LLM 返回 (12.3s · in=20653 out=412)
    [14:12:41]     说：内置工具里没有斐波那契，我造一个 function 给自己用。
    [14:12:41]   · 调用 write_function(name=fib, code=(118 字符))
    [14:12:42]   · ↳ write_function 完成 (0.0s · 输出 107 字符)
"""
from __future__ import annotations

import sys
import threading
import time
from typing import Any, Dict, Optional

#: 时间戳格式。固定 10 字符宽（`[HH:MM:SS]`），所以终端上时间戳那一列自然对齐。
STAMP_FORMAT = "[%H:%M:%S]"


def stamp(when: Optional[float] = None) -> str:
    """一面墙上时间的短前缀。

    Args:
        when: 秒级时间戳（`time.time()` 的口径）；None = 现在。

    Returns:
        `[HH:MM:SS]`，恰好 10 个字符。
    """
    return time.strftime(STAMP_FORMAT, time.localtime(when))


def stamped(text: str, when: Optional[float] = None) -> str:
    """给一段（可能多行的）文本逐行加时间戳。

    空行原样保留 —— 它们是分隔（`step` 前那一行），盖上时间戳只会得到
    一行「只有时间的空行」。多行共用同一个时间戳：它们本来就是同一瞬间
    打出来的一整块。

    Args:
        text: 要打印的文本，可有换行。
        when: 秒级时间戳；None = 现在。

    Returns:
        非空行都以 `[HH:MM:SS] ` 开头；空行原样保留。
    """
    prefix = stamp(when)
    lines = str(text).splitlines() or [""]
    return "\n".join(f"{prefix} {ln}" if ln.strip() else ln for ln in lines)


class NullProgress:
    """默认实现：什么都不做。保证不传 progress 时零行为变化。"""

    enabled = False

    def step(self, iteration: int, max_iterations: int) -> None: ...

    def llm_start(self, model: str) -> None: ...

    def llm_done(self, elapsed_ms: float, usage: Optional[Dict[str, Any]] = None,
                 error: Optional[str] = None, attempts: int = 1) -> None: ...

    def thought(self, content: str) -> None: ...

    def tool_start(self, name: str, args: Optional[Dict[str, Any]] = None) -> None: ...

    def tool_done(self, name: str, elapsed_ms: float, output_len: int = 0,
                  error: Optional[str] = None) -> None: ...

    def note(self, text: str) -> None: ...

    def close(self) -> None: ...


class Progress:
    """往一个文本流上打进度。线程安全；长等待用后台心跳保活。"""

    def __init__(self, stream: Any = None, heartbeat: float = 15.0,
                 enabled: bool = True, observer: Any = None) -> None:
        self.stream = stream if stream is not None else sys.stdout
        self.enabled = bool(enabled)
        # 观察者：终端之外的界面（频道）订阅同一批事件。None = 没人订阅。
        self.observer = observer
        self.heartbeat = max(0.0, float(heartbeat or 0))
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._t0 = time.time()
        try:  # Windows GBK 控制台会炸中文，尽力切到 utf-8
            self.stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ 基础
    def _say(self, text: str = "") -> None:
        if not self.enabled:
            return
        with self._lock:
            try:
                print(stamped(text), file=self.stream, flush=True)
            except Exception:  # noqa: BLE001
                pass

    def _tell(self, event: str, *args: Any) -> None:
        """把一次事件转给观察者；观察者没实现这个方法、或者它自己炸了，都当没发生。

        **观察者是纯旁路**：它的任何故障都不得影响 ReAct，所以这里不做日志、不重试、
        不抛 —— 静默吞掉是这里唯一正确的处理方式。
        """
        fn = None
        try:
            fn = getattr(self.observer, event, None) if self.observer is not None else None
        except Exception:  # noqa: BLE001 - 属性本身炸了（property/__getattr__）也算没这个事件
            return
        if fn is None:
            return
        try:
            fn(*args)
        except Exception:  # noqa: BLE001
            pass

    def _start_heartbeat(self, phase: str) -> None:
        # 终端闭嘴时（quiet）观察者仍要心跳：它是频道那边唯一能证明「还活着」的东西。
        if not (self.enabled or self.observer is not None) or self.heartbeat <= 0:
            return
        self._stop_heartbeat()
        self._stop = threading.Event()
        # 阶段与起点**按值传进线程**，不存成字段：`_stop_heartbeat` 只等 0.3 秒，
        # 掉队的那一拍若去读字段，读到的会是**新阶段配新起点**（≈0s），把界面上
        # 「正在做什么」写回上一段的下一步。
        started = time.time()
        t = threading.Thread(target=self._beat, args=(self._stop, phase, started),
                             daemon=True)
        t.start()
        self._thread = t

    def _beat(self, stop: threading.Event, phase: str, started: float) -> None:
        """长等待期间周期性打点，证明「还活着，在干活」。"""
        while not stop.wait(self.heartbeat):
            waited = time.time() - started
            self._say(f"  · … {phase}，已等待 {waited:.0f}s")
            self._tell("idle", phase, waited)

    def _stop_heartbeat(self) -> None:
        self._stop.set()
        t = self._thread
        self._thread = None
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=0.3)

    # ------------------------------------------------------------ 打点
    def step(self, iteration: int, max_iterations: int) -> None:
        self._stop_heartbeat()
        self._say(f"\n[step {iteration}/{max_iterations}]")
        self._tell("step", iteration, max_iterations)

    def llm_start(self, model: str) -> None:
        self._say(f"  · 思考中（{model}）…")
        self._tell("llm_start", model)
        self._start_heartbeat("等待模型返回")

    def llm_done(self, elapsed_ms: float, usage: Optional[Dict[str, Any]] = None,
                 error: Optional[str] = None, attempts: int = 1) -> None:
        self._stop_heartbeat()
        self._tell("llm_done", elapsed_ms, usage, error, attempts)
        secs = (elapsed_ms or 0) / 1000
        # 只有真的重试过（attempts>1）才提示，免得每步都刷一条噪声
        tries = f" · 共 {attempts} 次尝试" if attempts and attempts > 1 else ""
        if error:
            self._say(f"  ✗ 模型调用失败 ({secs:.1f}s{tries})：{_one_line(error, 200)}")
            return
        u = usage or {}
        extra = ""
        if u:
            extra = f" · in={u.get('prompt_tokens', '?')} out={u.get('completion_tokens', '?')}"
        self._say(f"  · LLM 返回 ({secs:.1f}s{extra}{tries})")

    def thought(self, content: str) -> None:
        text = _one_line(content, 160)
        if text:
            self._say(f"    说：{text}")
        self._tell("thought", content)

    def tool_start(self, name: str, args: Optional[Dict[str, Any]] = None) -> None:
        self._say(f"  · 调用 {name}({_short_args(args or {})})")
        self._tell("tool_start", name, args)
        self._start_heartbeat(f"{name} 执行中")

    def tool_done(self, name: str, elapsed_ms: float, output_len: int = 0,
                  error: Optional[str] = None) -> None:
        self._stop_heartbeat()
        self._tell("tool_done", name, elapsed_ms, output_len, error)
        secs = (elapsed_ms or 0) / 1000
        if error:
            self._say(f"  ✗ ↳ {name} 失败 ({secs:.1f}s)：{_one_line(error, 200)}")
        else:
            self._say(f"  · ↳ {name} 完成 ({secs:.1f}s · 输出 {output_len} 字符)")

    def note(self, text: str) -> None:
        self._say(f"  · {_one_line(text, 200)}")
        self._tell("note", text)

    def close(self) -> None:
        # `close` **不**转给观察者：它是一轮结束（多轮任务里会出现 N 次），
        # 而观察者要的是「这一次任务结束了」，那个口径在 `Channel.notify()` 里。
        self._stop_heartbeat()


# ---------------------------------------------------------------- 辅助


def _one_line(text: Any, limit: int = 200) -> str:
    s = "" if text is None else str(text)
    s = " ".join(s.strip().split())
    return s if len(s) <= limit else s[:limit] + " …"


def _arg_brief(value: Any) -> str:
    """参数摘要：多行 / 超长内容只报体量，不把代码糊满一行。"""
    s = str(value)
    if "\n" in s or len(s) > 60:
        return f"({len(s)} 字符)"
    return s


def _short_args(args: Dict[str, Any]) -> str:
    line = ", ".join(f"{k}={_arg_brief(v)}" for k, v in args.items())
    return line if len(line) <= 120 else line[:120] + " …"
