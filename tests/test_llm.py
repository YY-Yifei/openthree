"""LLM 层：指数退避重试 + finish_reason 处理。"""
from __future__ import annotations

import time

from openthree.llm import (LLM, LLMConfig, _backoff, _retryable, parse_token_count,
                           load_config)


class _Resp:
    def __init__(self, finish_reason="stop", content="ok"):
        self._fr = finish_reason
        self._content = content

    def model_dump(self):
        return {
            "choices": [{"message": {"role": "assistant", "content": self._content},
                         "finish_reason": self._fr}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }


def _client(fail_times=0, exc_factory=None, resp=None):
    state = {"calls": 0, "payloads": []}

    class Completions:
        def create(self, **payload):
            state["calls"] += 1
            state["payloads"].append(payload)
            if state["calls"] <= fail_times:
                raise (exc_factory or (lambda: TimeoutError("read timed out")))()
            return resp or _Resp()

    class Client:
        chat = type("C", (), {"completions": Completions()})()

    return Client(), state


def _llm(client, retries=3):
    llm = LLM(LLMConfig(api_key="k", base_url="u", retries=retries))
    llm._client = client
    return llm


def test_retry_succeeds_after_transient_failure():
    client, state = _client(fail_times=2)
    r = _llm(client).chat([{"role": "user", "content": "hi"}])
    assert r["error"] is None
    assert r["message"]["content"] == "ok"
    assert state["calls"] == 3


def test_retry_bounded_by_config():
    client, state = _client(fail_times=99)
    t0 = time.time()
    r = _llm(client, retries=1).chat([{"role": "user", "content": "hi"}])
    assert r["error"] and "Timeout" in r["error"]
    assert state["calls"] == 2
    assert time.time() - t0 < 10  # 退避不能失控


def test_non_retryable_fails_fast():
    client, state = _client(fail_times=99, exc_factory=lambda: ValueError("bad request"))
    r = _llm(client, retries=5).chat([{"role": "user", "content": "hi"}])
    assert r["error"] and "ValueError" in r["error"]
    assert state["calls"] == 1  # 参数类错误不重试


def test_finish_reason_length_is_reported():
    client, _ = _client(resp=_Resp(finish_reason="length", content="半截"))
    r = _llm(client).chat([{"role": "user", "content": "hi"}])
    assert r["error"] and "length" in r["error"]
    assert r["finish_reason"] == "length"


# ---------------------------------------------------------------- 生成参数按调用覆盖
def test_chat_per_call_max_tokens_and_temperature():
    """按次覆盖不动 config —— Agent 靠它传「模型自管的运行边界」，避免跨轮污染。"""
    client, state = _client()
    llm = _llm(client)
    llm.config.max_tokens, llm.config.temperature = 8192, 0.3
    llm.chat([{"role": "user", "content": "hi"}],
             max_tokens=1234, temperature=1.5)
    p = state["payloads"][0]
    assert p["max_tokens"] == 1234 and p["temperature"] == 1.5
    # 配置本身没被改写
    assert llm.config.max_tokens == 8192 and llm.config.temperature == 0.3


def test_chat_falls_back_to_config_when_not_overridden():
    client, state = _client()
    llm = _llm(client)
    llm.chat([{"role": "user", "content": "hi"}])
    p = state["payloads"][0]
    assert p["max_tokens"] == llm.config.max_tokens
    assert p["temperature"] == llm.config.temperature


def test_truncation_error_reports_effective_max_tokens():
    """截断报错要说实话：用本次调用真正生效的边界，而非 config。"""
    client, _ = _client(resp=_Resp(finish_reason="length"))
    r = _llm(client).chat([{"role": "user", "content": "hi"}], max_tokens=777)
    assert r["error"] and "777" in r["error"]


def test_retryable_classifier():
    assert _retryable(TimeoutError("x"))
    assert _retryable(RuntimeError("503 Service Unavailable"))
    assert _retryable(RuntimeError("connection reset"))
    assert not _retryable(ValueError("invalid api key"))


def test_backoff_is_capped_and_jittered():
    assert 1.0 <= _backoff(0) <= 1.5
    assert 2.0 <= _backoff(1) <= 2.5
    assert _backoff(99) <= 30.5


# ---------------------------------------------------------------- 上下文窗口
def test_parse_token_count_units():
    assert parse_token_count("100M") == 100_000_000
    assert parse_token_count("128k") == 128_000
    assert parse_token_count(200000) == 200000
    assert parse_token_count("1,000,000") == 1_000_000
    assert parse_token_count("2g") == 2_000_000_000
    assert parse_token_count("abc") is None
    assert parse_token_count(None) is None
    assert parse_token_count(True) is None


def test_config_default_context_window_is_1m():
    assert LLMConfig().context_window_tokens == 1_000_000


def test_config_reads_context_window(tmp_path):
    (tmp_path / ".openthree").mkdir()
    (tmp_path / ".openthree" / "config.yaml").write_text(
        "llm:\n  api_key: k\n  base_url: u\n  context_window_tokens: 128k\n",
        encoding="utf-8")
    cfg = load_config(tmp_path)
    assert cfg.context_window_tokens == 128_000
