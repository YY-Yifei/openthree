"""进度回声：CLI 在长任务里不再沉默。

这一层是纯展示，所以测试只关心「说没说 / 说了什么 / 会不会卡线程」，
不关心 ReAct 的正确性（那是 test_agent.py 的事）。
"""
from __future__ import annotations

import io
import json
import time

from openthree.agent import Agent
from openthree.kernel import KERNEL
from openthree.llm import LLMConfig
from openthree.progress import NullProgress, Progress, _short_args
from openthree.recorder import Recorder


# ---------------------------------------------------------------- 纯展示层
def test_null_progress_is_silent():
    p = NullProgress()
    p.step(1, 10)
    p.llm_start("m")
    p.tool_done("x", 1.0)
    p.close()
    assert p.enabled is False


def test_progress_writes_to_stream():
    buf = io.StringIO()
    p = Progress(stream=buf, heartbeat=0)
    p.step(2, 200)
    p.llm_start("stub-model")
    p.llm_done(1200.0, {"prompt_tokens": 10, "completion_tokens": 3})
    p.thought("我在想事情")
    p.tool_start("read_file", {"path": "a.txt"})
    p.tool_done("read_file", 30.0, 42)
    out = buf.getvalue()
    assert "[step 2/200]" in out
    assert "stub-model" in out
    assert "in=10" in out and "out=3" in out
    assert "我在想事情" in out
    assert "read_file" in out and "42 字符" in out


def test_progress_reports_error():
    buf = io.StringIO()
    p = Progress(stream=buf, heartbeat=0)
    p.llm_done(500.0, None, "TimeoutError: read timed out")
    assert "✗" in buf.getvalue() and "Timeout" in buf.getvalue()


def test_short_args_hides_big_blobs():
    """多行 / 超长参数只报体量，不把代码糊满终端一行。"""
    line = _short_args({"name": "fib", "code": "def fib(n):\n    return n\n" * 20})
    assert "name=fib" in line
    assert "字符)" in line and "\n" not in line and "def fib" not in line


def test_heartbeat_fires_during_long_wait():
    buf = io.StringIO()
    p = Progress(stream=buf, heartbeat=0.05)
    p.llm_start("m")          # 开始一个「长等待」
    time.sleep(0.2)           # 模拟模型迟迟不返回
    p.llm_done(200.0)
    out = buf.getvalue()
    assert "已等待" in out, out
    assert "LLM 返回" in out


def test_heartbeat_stops_on_close():
    buf = io.StringIO()
    p = Progress(stream=buf, heartbeat=0.05)
    p.llm_start("m")
    p.close()
    time.sleep(0.15)
    first = buf.getvalue()
    time.sleep(0.15)
    assert buf.getvalue() == first, "close 之后心跳线程还在打点"


# ---------------------------------------------------------------- Agent 打点
class _StubLLM:
    """第一轮调工具，第二轮收尾。"""

    def __init__(self):
        self.config = LLMConfig(api_key="k", base_url="u", model="stub-model")
        self.n = 0

    def chat(self, messages, tools=None, tool_choice="auto"):
        self.n += 1
        if self.n == 1:
            msg = {"role": "assistant", "content": "先看一眼时间。",
                   "tool_calls": [{"id": "c1", "type": "function",
                                   "function": {"name": "get_time", "arguments": "{}"}}]}
        else:
            msg = {"role": "assistant", "content": "看完了，收尾。",
                   "tool_calls": [{"id": "c2", "type": "function",
                                   "function": {"name": "finish",
                                                "arguments": json.dumps({"answer": "好了"})}}]}
        return {"message": msg, "usage": {"prompt_tokens": 5, "completion_tokens": 2},
                "elapsed_ms": 7.0, "raw_request": {"model": "stub-model", "messages": []},
                "raw_response": {"choices": [{"message": msg}]}, "error": None}


def _wire(ws, registry, rec, ag):
    """把各部件挂进 KERNEL —— finish/get_time 这些内置工具依赖它。"""
    KERNEL.workspace = ws
    KERNEL.registry = registry
    KERNEL.recorder = rec
    KERNEL.agent = ag


def test_agent_emits_progress_for_steps_and_tools(ws, registry):
    buf = io.StringIO()
    rec = Recorder(ws, user_input="t", model="stub-model")
    ag = Agent(ws, registry, _StubLLM(), rec,
               progress=Progress(stream=buf, heartbeat=0))
    _wire(ws, registry, rec, ag)
    answer = ag.run()
    out = buf.getvalue()
    assert answer == "好了"
    assert "[step 1/" in out and "[step 2/" in out
    assert "get_time" in out               # 工具调用有回声
    assert "完成" in out
    assert "先看一眼时间。" in out          # 模型的思考有回声


def test_agent_without_progress_is_unchanged(ws, registry):
    """不传 progress 时必须零行为变化 —— 默认走 NullProgress。"""
    rec = Recorder(ws, user_input="t", model="stub-model")
    ag = Agent(ws, registry, _StubLLM(), rec)
    _wire(ws, registry, rec, ag)
    assert isinstance(ag.progress, NullProgress)
    assert ag.run() == "好了"
