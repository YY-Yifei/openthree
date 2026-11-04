"""LLM 层：传输（一次 POST）+ 指数退避重试 + finish_reason 处理。

被守护的不变量（动这里要守住）：

1. 重试**只在** `LLM.chat()` 这一层发生：`retries=N` ⇒ 最多 N+1 次真实请求。
   下面没有第二个会偷偷重试的传输层，所以次数可预期；
2. 可重试性**由状态码裁决**：408 / 409 / 429 / 5xx 重试，其它 4xx 立刻返回
   —— 重试一个鉴权失败只是把同一个错误再发一遍、再等一遍。状态码为 0
   （根本没走到 HTTP：DNS / 连接 / 超时）一律可重试；
3. connect 与 read 的超时**必须分开**：连不上主机要在几秒内失败，而「模型想得久」
   仍能等满 `timeout`。混成一个的代价是每次重试干等十分钟；
4. `finish_reason=length` 要如实上报为截断，且报的是**本次调用真正生效**的
   max_tokens，而不是 config 里的那个；
5. 整层只用标准库：没有 openai / httpx 也要能跑（没有编译器的环境装不了
   pydantic-core，那正是我们不引 SDK 的原因）。
"""
from __future__ import annotations

import json
import pathlib
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import openthree.llm as llm_mod
from openthree.llm import (LLM, LLMConfig, TransportError, _backoff, _retryable,
                           load_config, parse_token_count, post_chat)


# ---------------------------------------------------------------- 测试替身
def _resp(finish_reason: str = "stop", content: str = "ok") -> dict:
    """一份最小可用的 chat/completion 响应（transport 直接返回 dict）。"""
    return {
        "choices": [{"message": {"role": "assistant", "content": content},
                     "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }


def _transport(monkeypatch, fail_times=0, exc_factory=None, resp=None) -> dict:
    """假 transport：记下每次要发出去的 payload，并按需先失败 `fail_times` 次。"""
    state = {"calls": 0, "payloads": []}

    def fake_post_chat(base_url, api_key, payload, **kwargs):
        state["calls"] += 1
        state["payloads"].append(payload)
        if state["calls"] <= fail_times:
            raise (exc_factory or (lambda: TransportError("read timed out")))()
        return resp if resp is not None else _resp()

    monkeypatch.setattr(llm_mod, "post_chat", fake_post_chat)
    return state


def _llm(retries: int = 3) -> LLM:
    return LLM(LLMConfig(api_key="k", base_url="http://test/v1", retries=retries))


# ---------------------------------------------------------------- 重试
def test_retry_succeeds_after_transient_failure(monkeypatch):
    state = _transport(monkeypatch, fail_times=2)
    r = _llm().chat([{"role": "user", "content": "hi"}])
    assert r["error"] is None
    assert r["message"]["content"] == "ok"
    assert state["calls"] == 3


def test_retry_bounded_by_config(monkeypatch):
    state = _transport(monkeypatch, fail_times=99)
    t0 = time.time()
    r = _llm(retries=1).chat([{"role": "user", "content": "hi"}])
    assert r["error"] and "read timed out" in r["error"]
    assert state["calls"] == 2
    assert time.time() - t0 < 10  # 退避不能失控


def test_non_retryable_fails_fast(monkeypatch):
    state = _transport(monkeypatch, fail_times=99,
                       exc_factory=lambda: ValueError("bad request"))
    r = _llm(retries=5).chat([{"role": "user", "content": "hi"}])
    assert r["error"] and "ValueError" in r["error"]
    assert state["calls"] == 1  # 参数类错误不重试


def test_retry_is_decided_by_status_code(monkeypatch):
    """429 值得重试；401 不值得 —— 鉴权失败重试多少次都还是失败。"""
    state = _transport(monkeypatch, fail_times=2,
                       exc_factory=lambda: TransportError("HTTP 429: slow down", status=429))
    r = _llm(retries=3).chat([{"role": "user", "content": "hi"}])
    assert r["error"] is None and r["attempts"] == 3 and state["calls"] == 3

    state2 = _transport(monkeypatch, fail_times=99,
                        exc_factory=lambda: TransportError("HTTP 401: bad key", status=401))
    r2 = _llm(retries=5).chat([{"role": "user", "content": "hi"}])
    assert r2["attempts"] == 1 and "401" in r2["error"]
    assert state2["calls"] == 1


def test_status_code_beats_text_in_error_body(monkeypatch):
    """正文里写着 timeout 的 400 仍然是 400：状态码优先，不能被文本带偏。"""
    state = _transport(
        monkeypatch, fail_times=99,
        exc_factory=lambda: TransportError("HTTP 400: upstream timeout", status=400))
    r = _llm(retries=5).chat([{"role": "user", "content": "hi"}])
    assert r["attempts"] == 1 and state["calls"] == 1


def test_retryable_classifier():
    assert _retryable(TimeoutError("x"))
    assert _retryable(RuntimeError("503 Service Unavailable"))
    assert _retryable(RuntimeError("connection reset"))
    assert not _retryable(ValueError("invalid api key"))
    # 带状态码时只看状态码：文本不再参与裁决
    assert _retryable(TransportError("x", status=0))       # 没走到 HTTP
    assert _retryable(TransportError("x", status=429))
    assert not _retryable(TransportError("x", status=400))
    assert not _retryable(TransportError("HTTP 400: timeout", status=400))


def test_retryable_covers_500_and_guards_against_number_substrings():
    """5xx 的 "500" 要认；但不能把 "5000 tokens" 这类数字误判成 5xx。"""
    for status in ("408", "409", "429", "500", "502", "503", "504"):
        assert _retryable(RuntimeError(f"Error code: {status} - boom")), status
    # 词边界：数字里嵌着的 500 不算
    assert not _retryable(RuntimeError("max_tokens exceeds 5000 tokens"))
    assert not _retryable(ValueError("invalid api key"))


def test_backoff_is_capped_and_jittered():
    assert 1.0 <= _backoff(0) <= 1.5
    assert 2.0 <= _backoff(1) <= 2.5
    assert _backoff(99) <= 30.5


# ---------------------------------------------------------------- 传输层
def test_post_chat_splits_connect_and_read_timeout(monkeypatch):
    """connect 受 connect_timeout 管，连上之后 read 换成 timeout —— 两者不能混成一个。

    混成一个的后果：连不上主机也要干等满 read 超时（默认 600s），一次重试十分钟。
    """
    seen = {}

    class FakeSock:
        def settimeout(self, value):
            seen["read_timeout"] = value

    class FakeConn:
        def __init__(self, host, port, timeout=None, **kwargs):
            seen["connect_timeout"] = timeout
            self.sock = None

        def connect(self):
            self.sock = FakeSock()

        def request(self, method, path, body=None, headers=None):
            seen["request"] = (method, path)

        def getresponse(self):
            class _R:
                status = 200

                def getheader(self, name):
                    return None

                def read(self):
                    return b'{"ok": true}'

            return _R()

        def close(self):
            seen["closed"] = True

    monkeypatch.setattr(llm_mod.http.client, "HTTPConnection", FakeConn)
    out = post_chat("http://example.test/v1", "sk-k", {"model": "m"},
                    timeout=600.0, connect_timeout=5.0)

    assert out == {"ok": True}
    assert seen["connect_timeout"] == 5.0     # 建连：短超时
    assert seen["read_timeout"] == 600.0      # 连上之后：等模型的那个超时
    assert seen["request"] == ("POST", "/v1/chat/completions")
    assert seen["closed"] is True


def test_post_chat_read_timeout_fires_without_waiting_for_connect_timeout():
    """服务端接受连接却永不回话：只该由 read 超时打破，且远早于 connect_timeout。"""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)                     # 只 listen 不 accept：连接会成功，但永远没有响应
    port = srv.getsockname()[1]
    t0 = time.time()
    try:
        with pytest.raises(TransportError) as ei:
            post_chat(f"http://127.0.0.1:{port}/v1", "k", {"model": "m"},
                      timeout=1.0, connect_timeout=30.0)
    finally:
        srv.close()
    assert ei.value.status == 0       # 没走到 HTTP
    assert time.time() - t0 < 5.0     # 被 read 的 1s 打破，而不是干等 30s


def _serve(handler_cls) -> HTTPServer:
    srv = HTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_post_chat_end_to_end_against_real_server():
    """真发一次 HTTP：路径拼接、Bearer 头、JSON 往返都得对（不 mock 传输层）。"""
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("content-length") or 0)
            seen["body"] = json.loads(self.rfile.read(n) or b"{}")
            seen["auth"] = self.headers.get("authorization")
            seen["path"] = self.path
            raw = json.dumps(_resp(finish_reason="tool_calls")).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    srv = _serve(Handler)
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}/v1"
        out = post_chat(base, "sk-secret", {"model": "m", "messages": []})
    finally:
        srv.shutdown()
        srv.server_close()

    assert out["choices"][0]["finish_reason"] == "tool_calls"
    assert seen["path"] == "/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-secret"
    assert seen["body"]["model"] == "m"


def test_post_chat_error_body_reaches_the_caller():
    """4xx / 5xx 的说明几乎总在正文里，光看状态码没法排查 —— 必须把它带出来。"""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            raw = json.dumps(
                {"error": {"message": "invalid api key", "type": "auth"}}).encode()
            self.send_response(401)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    srv = _serve(Handler)
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}/v1"
        with pytest.raises(TransportError) as ei:
            post_chat(base, "k", {"model": "m"})
    finally:
        srv.shutdown()
        srv.server_close()

    assert ei.value.status == 401
    assert "invalid api key" in str(ei.value)


# ---------------------------------------------------------------- 代理
def test_proxy_parsed_from_environment(monkeypatch):
    """HTTPS_PROXY 的主机 / 端口 / 认证要能解析出来 —— 换掉 SDK 不该丢掉代理能力。"""
    monkeypatch.setenv("HTTPS_PROXY", "http://user:pw@127.0.0.1:8080")
    monkeypatch.setenv("NO_PROXY", "")
    assert llm_mod._proxy_for("https", "api.example.com") == (
        "127.0.0.1", 8080, "Basic dXNlcjpwdw==")


def test_proxy_honours_no_proxy(monkeypatch):
    """命中 `no_proxy` 就直连：内网端点不该被推进代理。"""
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8080")
    monkeypatch.setenv("NO_PROXY", "api.example.com")
    assert llm_mod._proxy_for("https", "api.example.com") is None


def test_no_proxy_configured_returns_none(monkeypatch):
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(llm_mod.urllib.request, "getproxies", lambda: {})
    assert llm_mod._proxy_for("https", "api.example.com") is None


def test_https_target_tunnels_through_proxy(monkeypatch):
    """有代理时：连接建在代理上，CONNECT 隧道指向真端点，认证头带上。"""
    seen = {}

    class FakeSock:
        def settimeout(self, value):
            seen["read_timeout"] = value

    class FakeConn:
        def __init__(self, host, port, timeout=None, **kwargs):
            seen["conn"] = (host, port)

        def set_tunnel(self, host, port, headers=None):
            seen["tunnel"] = (host, port, headers)

        def connect(self):
            self.sock = FakeSock()

        def request(self, method, path, body=None, headers=None):
            seen["request"] = (method, path)

        def getresponse(self):
            class _R:
                status = 200

                def getheader(self, name):
                    return None

                def read(self):
                    return b"{}"

            return _R()

        def close(self):
            pass

    monkeypatch.setattr(llm_mod, "_proxy_for",
                        lambda scheme, host: ("127.0.0.1", 8080, "Basic Zm9v"))
    monkeypatch.setattr(llm_mod.http.client, "HTTPSConnection", FakeConn)
    post_chat("https://api.example.com/v1", "sk-k", {"model": "m"})

    assert seen["conn"] == ("127.0.0.1", 8080)                 # 连的是代理
    assert seen["tunnel"][:2] == ("api.example.com", 443)      # 隧道指向真端点
    assert seen["tunnel"][2]["Proxy-Authorization"] == "Basic Zm9v"
    assert seen["read_timeout"] == 600.0                       # 有代理也保持超时分离


# ---------------------------------------------------------------- 生成参数按调用覆盖
def test_finish_reason_length_is_reported(monkeypatch):
    _transport(monkeypatch, resp=_resp(finish_reason="length", content="半截"))
    r = _llm().chat([{"role": "user", "content": "hi"}])
    assert r["error"] and "length" in r["error"]
    assert r["finish_reason"] == "length"


def test_chat_per_call_max_tokens_and_temperature(monkeypatch):
    """按次覆盖不动 config —— Agent 靠它传「模型自管的运行边界」，避免跨轮污染。"""
    state = _transport(monkeypatch)
    llm = _llm()
    llm.config.max_tokens, llm.config.temperature = 8192, 0.3
    llm.chat([{"role": "user", "content": "hi"}],
             max_tokens=1234, temperature=1.5)
    p = state["payloads"][0]
    assert p["max_tokens"] == 1234 and p["temperature"] == 1.5
    # 配置本身没被改写
    assert llm.config.max_tokens == 8192 and llm.config.temperature == 0.3


def test_chat_falls_back_to_config_when_not_overridden(monkeypatch):
    state = _transport(monkeypatch)
    llm = _llm()
    llm.chat([{"role": "user", "content": "hi"}])
    p = state["payloads"][0]
    assert p["max_tokens"] == llm.config.max_tokens
    assert p["temperature"] == llm.config.temperature


def test_truncation_error_reports_effective_max_tokens(monkeypatch):
    """截断报错要说实话：用本次调用真正生效的边界，而非 config。"""
    _transport(monkeypatch, resp=_resp(finish_reason="length"))
    r = _llm().chat([{"role": "user", "content": "hi"}], max_tokens=777)
    assert r["error"] and "777" in r["error"]


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
def test_attempts_reported_on_success_and_failure(monkeypatch):
    """`attempts` 是「真正打到线上的次数」：成功=重试后成功，失败=用尽的次数。"""
    # 失败 2 次后成功 → attempts 应为 3
    state = _transport(monkeypatch, fail_times=2)
    r = _llm().chat([{"role": "user", "content": "hi"}])
    assert r["attempts"] == 3 and state["calls"] == 3

    # 一直失败、retries=1 → 用尽 2 次
    state2 = _transport(monkeypatch, fail_times=99)
    r2 = _llm(retries=1).chat([{"role": "user", "content": "hi"}])
    assert r2["attempts"] == 2 and r2["error"]

    # 不可重试 → 只 1 次
    state3 = _transport(monkeypatch, fail_times=99,
                        exc_factory=lambda: ValueError("bad request"))
    r3 = _llm(retries=5).chat([{"role": "user", "content": "hi"}])
    assert r3["attempts"] == 1 and state3["calls"] == 1


# ---------------------------------------------------------------- 依赖面
def test_llm_layer_imports_no_third_party_http_sdk():
    """LLM 层只用标准库：装不上 openai / pydantic 的环境（无编译器）也要能跑。"""
    src = pathlib.Path(llm_mod.__file__).read_text(encoding="utf-8")
    for forbidden in ("import openai", "import httpx", "import pydantic"):
        assert forbidden not in src, f"LLM 层不该依赖 {forbidden}"


def test_main_dependencies_stay_pure_python():
    """主依赖里不能出现 openai（它拖 pydantic-core / Rust），否则 iOS 之类装不上。"""
    root = pathlib.Path(__file__).resolve().parents[1]
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    deps = text.split("dependencies = [", 1)[1].split("]", 1)[0]
    assert "openai" not in deps
