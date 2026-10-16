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


def test_config_default_max_tokens():
    """内核默认生成上限：配置缺省时 `LLMConfig` 与 `load_config` 都给这个值。"""
    assert LLMConfig().max_tokens == 32768


def test_load_config_falls_back_to_default_max_tokens(tmp_path, monkeypatch):
    """没有任何一层给出 max_tokens 时，`load_config` 回落内核默认（而非 0/None）。"""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    (tmp_path / ".openthree").mkdir()
    (tmp_path / ".openthree" / "config.yaml").write_text(
        "llm:\n  api_key: k\n  base_url: u\n", encoding="utf-8")
    cfg = load_config(tmp_path)
    assert cfg.max_tokens == LLMConfig().max_tokens


def test_config_reads_context_window(tmp_path):
    (tmp_path / ".openthree").mkdir()
    (tmp_path / ".openthree" / "config.yaml").write_text(
        "llm:\n  api_key: k\n  base_url: u\n  context_window_tokens: 128k\n",
        encoding="utf-8")
    cfg = load_config(tmp_path)
    assert cfg.context_window_tokens == 128_000


# ---------------------------------------------------------------- 重试层：可观测与分层
def test_attempts_reported_on_success_and_failure():
    """`attempts` 是「真正打到线上的次数」：成功=重试后成功，失败=用尽的次数。"""
    # 失败 2 次后成功 → attempts 应为 3
    client, state = _client(fail_times=2)
    r = _llm(client).chat([{"role": "user", "content": "hi"}])
    assert r["attempts"] == 3 and state["calls"] == 3

    # 一直失败、retries=1 → 用尽 2 次
    client2, state2 = _client(fail_times=99)
    r2 = _llm(client2, retries=1).chat([{"role": "user", "content": "hi"}])
    assert r2["attempts"] == 2 and r2["error"]

    # 不可重试 → 只 1 次
    client3, _ = _client(fail_times=99, exc_factory=lambda: ValueError("bad request"))
    r3 = _llm(client3, retries=5).chat([{"role": "user", "content": "hi"}])
    assert r3["attempts"] == 1


def test_retryable_covers_500_and_guards_against_number_substrings():
    """5xx 的 "500" 要认；但不能把 "5000 tokens" 这类数字误判成 5xx。"""
    for status in ("408", "409", "429", "500", "502", "503", "504"):
        assert _retryable(RuntimeError(f"Error code: {status} - boom")), status
    # 词边界：数字里嵌着的 500 不算
    assert not _retryable(RuntimeError("max_tokens exceeds 5000 tokens"))
    assert not _retryable(ValueError("invalid api key"))


def test_client_timeout_is_split_and_sdk_retry_disabled():
    """connect 超时必须单独短，且 SDK 传输层重试显式关闭（默认 0）。"""
    llm = LLM(LLMConfig(api_key="k", base_url="http://test/v1",
                        timeout=600.0, connect_timeout=5.0, max_retries=0))
    client = llm._ensure_client()
    assert client.max_retries == 0
    t = client.timeout
    # httpx.Timeout：connect 被单独钉在 5s，read 仍是 600s
    assert t.connect == 5.0
    assert t.read == 600.0

