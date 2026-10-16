"""进度回声：让 CLI 在长任务里一直有输出。

设计边界（很重要）：

    * 这是**纯展示层**，不含任何决策逻辑，不参与 ReAct，不影响回答质量。
    * Agent 只调用注入进来的 progress 对象，它并不认识「终端 / print」。
    * 不传 progress 时，Agent 走 NullProgress，行为完全一致。

为什么需要它：一次 LLM 调用可能几十秒到几分钟，一次 run_shell 可能几十秒，
而 openthree 在开跑前说过三句话之后就彻底沉默了 —— 用户无法区分
「模型正在思考」和「进程已经卡死」。这里补上那个回声。

    [step 3/200]
      · 思考中（deepseek-v4.1-flash）…
      · … 等待模型返回，已等待 30s        ← 心跳，长等待时周期性出现
      · LLM 返回 (12.3s · in=20653 out=412)
        说：内置工具里没有斐波那契，我造一个 function 给自己用。
      · 调用 write_function(name=fib, code=(118 字符))
      · ↳ write_function 完成 (0.0s · 输出 107 字符)
"""
from __future__ import annotations

import sys
import threading
import time
from typing import Any, Dict, Optional


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
                 enabled: bool = True) -> None:
        self.stream = stream if stream is not None else sys.stdout
        self.enabled = bool(enabled)
        self.heartbeat = max(0.0, float(heartbeat or 0))
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._phase = ""
        self._phase_start = 0.0
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
                print(text, file=self.stream, flush=True)
            except Exception:  # noqa: BLE001
                pass

    def _start_heartbeat(self, phase: str) -> None:
        self._phase = phase
        self._phase_start = time.time()
        if not self.enabled or self.heartbeat <= 0:
            return
        self._stop_heartbeat()
        self._stop = threading.Event()
        t = threading.Thread(target=self._beat, args=(self._stop,), daemon=True)
        t.start()
        self._thread = t

    def _beat(self, stop: threading.Event) -> None:
        """长等待期间周期性打点，证明「还活着，在干活」。"""
        while not stop.wait(self.heartbeat):
            waited = time.time() - self._phase_start
            self._say(f"  · … {self._phase}，已等待 {waited:.0f}s")

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

    def llm_start(self, model: str) -> None:
        self._say(f"  · 思考中（{model}）…")
        self._start_heartbeat("等待模型返回")

    def llm_done(self, elapsed_ms: float, usage: Optional[Dict[str, Any]] = None,
                 error: Optional[str] = None, attempts: int = 1) -> None:
        self._stop_heartbeat()
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

    def tool_start(self, name: str, args: Optional[Dict[str, Any]] = None) -> None:
        self._say(f"  · 调用 {name}({_short_args(args or {})})")
        self._start_heartbeat(f"{name} 执行中")

    def tool_done(self, name: str, elapsed_ms: float, output_len: int = 0,
                  error: Optional[str] = None) -> None:
        self._stop_heartbeat()
        secs = (elapsed_ms or 0) / 1000
        if error:
            self._say(f"  ✗ ↳ {name} 失败 ({secs:.1f}s)：{_one_line(error, 200)}")
        else:
            self._say(f"  · ↳ {name} 完成 ({secs:.1f}s · 输出 {output_len} 字符)")

    def note(self, text: str) -> None:
        self._say(f"  · {_one_line(text, 200)}")

    def close(self) -> None:
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
