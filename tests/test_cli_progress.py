"""CLI 级别的证据：跑长任务时终端不会一直不动。

一个「慢模型」在思考时，终端上必须能看到 step / 思考中 / 心跳，而不是一片死寂。
"""
from __future__ import annotations

import json
import time

from openthree.runtime import Runtime


class _SlowStubLLM:
    """思考很慢（默认 0.3s），用来逼出心跳。一次 finish 收尾。"""

    def __init__(self, cfg, delay: float = 0.3, answer: str = "搞定了"):
        self.config = cfg
        self.delay = delay
        self.answer = answer

    def chat(self, messages, tools=None, tool_choice="auto", **kwargs):
        time.sleep(self.delay)
        msg = {"role": "assistant", "content": "",
               "tool_calls": [{"id": "c1", "type": "function",
                               "function": {"name": "finish",
                                            "arguments": json.dumps({"answer": self.answer})}}]}
        return {"message": msg, "usage": {"prompt_tokens": 9, "completion_tokens": 4},
                "elapsed_ms": self.delay * 1000,
                "raw_request": {"model": "stub", "messages": []},
                "raw_response": {"choices": [{"message": msg}]}, "error": None}


def _make(tmp_path, delay: float = 0.3, **kw) -> Runtime:
    rt = Runtime(tmp_path, **kw)
    rt.config.api_key = rt.config.base_url = "stub"
    rt.config.model = "stub-model"
    rt.llm = _SlowStubLLM(rt.config, delay=delay)
    return rt


def test_runtime_keeps_talking_during_slow_model(tmp_path, capsys):
    rt = _make(tmp_path, heartbeat=0.05)
    doc = rt.run("干点活")
    out = capsys.readouterr().out

    assert doc["answer"] == "搞定了"
    assert "[step 1/200]" in out            # 每一步有回声
    assert "思考中" in out                   # 调模型前有回声
    assert "已等待" in out                   # 长等待期间有心跳 —— 关键
    assert "LLM 返回" in out                 # 回来了也告诉你
    assert "第 1 轮完成" in out               # 收尾也交代是第几轮


def test_quiet_runtime_is_silent(tmp_path, capsys):
    rt = _make(tmp_path, delay=0.05, heartbeat=0.05, quiet=True)
    rt.run("干点活")
    assert capsys.readouterr().out.strip() == ""


def test_heartbeat_zero_disables_heartbeat(tmp_path, capsys):
    rt = _make(tmp_path, heartbeat=0)
    rt.run("干点活")
    out = capsys.readouterr().out
    assert "[step 1/200]" in out            # 进度照旧
    assert "已等待" not in out               # 但心跳被关掉了
