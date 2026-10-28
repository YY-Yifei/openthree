"""推理强度（reasoning_effort）与 reasoning 回传。

被守护的不变量（动这里要守住）：

1. `--reasoning-effort` 给出的值一路传到**真正发出的请求体**里；
2. 优先级是「显式 > config 字段 > config.extra」—— 人类在命令行给的值
   不能被配置文件里顺手写的透传值悄悄吃掉；
3. 三项都没有时，请求体里**没有** `reasoning_effort` 这个键（不发 null，
   严格校验的 provider 会因此 400）；
4. provider 用哪个字段名吐 reasoning（`reasoning_content` / `reasoning` /
   `reasoning_text`），就用哪个名字原样回放进历史 —— 改名等于对方认不出来，
   整段 reasoning 白存；
5. 端点要求「每条 assistant 消息都带 `reasoning_content`」时，没有 reasoning
   的那一轮要补**空串**占位：校验的是键存在，不是内容非空。占位受
   `llm.reasoning_content_replay`（auto / always / never）门控 —— 给不要求它的
   端点发多余键，同样是 400；
6. 回放进历史的 reasoning 必须**真的出现在下一次请求的 messages 里** ——
   要防的故障是「第二步 400」，所以断言落在请求体上，而不是落在 history 上；
7. 内核**自己造的** assistant 消息（折叠摘要节点、`-i` 播种的上一轮回答）同样要
   补占位 —— 「第二步 400」不只在模型吐的那条消息上：长任务一旦折叠、或多轮
   会话一开，缺占位就是同一个故障换条路径发生；
8. 察觉「推理开着」不能只看显式档位：`config.extra.reasoning_effort` 是
   `chat()` 会真的发出去的键，漏掉它同样会走到「判没开 → 不补 → 400」。
"""
from __future__ import annotations

from openthree.agent import _estimate_chars
from openthree.llm import (REASONING_FIELDS, REASONING_PLACEHOLDER_FIELD, LLM,
                           LLMConfig, _clean_effort, _redact, extract_reasoning,
                           is_always_replay, load_config,
                           resolve_reasoning_placeholder)


class _Resp:
    def __init__(self, message=None, finish_reason="stop"):
        self._message = message or {"role": "assistant", "content": "ok"}
        self._fr = finish_reason

    def model_dump(self):
        return {
            "choices": [{"message": self._message, "finish_reason": self._fr}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }


def _llm_with(client, **cfg) -> LLM:
    llm = LLM(LLMConfig(api_key="k", base_url="u", retries=0, **cfg))
    llm._client = client
    return llm


def _capturing_client(resp=None):
    """假 client：把每次 create 收到的 payload 记下来，供断言『真的发出去了什么』。"""
    state = {"payloads": []}

    class Completions:
        def create(self, **payload):
            state["payloads"].append(payload)
            return resp or _Resp()

    class Client:
        chat = type("C", (), {"completions": Completions()})()

    return Client(), state


# ---------------------------------------------------------------- 归一化
def test_clean_effort_normalizes_and_treats_blank_as_unset():
    assert _clean_effort("low") == "low"
    # 大小写归一：OpenAI 一侧的枚举全是小写，`HIGH` 原样发出去就是一句 400
    assert _clean_effort("  Medium ") == "medium"
    assert _clean_effort("HIGH") == "high"
    assert _clean_effort(None) is None
    assert _clean_effort("") is None
    assert _clean_effort("   ") is None
    assert _clean_effort(True) is None      # bool 不是档位名


# ---------------------------------------------------------------- 发出去的请求体
def test_chat_sends_per_call_reasoning_effort():
    client, state = _capturing_client()
    _llm_with(client).chat([{"role": "user", "content": "hi"}], reasoning_effort="low")
    assert state["payloads"][0]["reasoning_effort"] == "low"


def test_chat_omits_key_when_nothing_set():
    """没设置就不发这个键 —— 发 null 会被严格校验的 provider 400。"""
    client, state = _capturing_client()
    _llm_with(client).chat([{"role": "user", "content": "hi"}])
    assert "reasoning_effort" not in state["payloads"][0]


def test_config_field_used_when_call_does_not_override():
    client, state = _capturing_client()
    _llm_with(client, reasoning_effort="medium").chat([{"role": "user", "content": "hi"}])
    assert state["payloads"][0]["reasoning_effort"] == "medium"


def test_explicit_call_wins_over_config_field():
    client, state = _capturing_client()
    _llm_with(client, reasoning_effort="medium").chat(
        [{"role": "user", "content": "hi"}], reasoning_effort="high")
    assert state["payloads"][0]["reasoning_effort"] == "high"


def test_explicit_call_wins_over_config_extra():
    """extra 是透传口，显式参数必须压过它 —— 否则命令行给的值被配置文件吃掉。"""
    client, state = _capturing_client()
    _llm_with(client, extra={"reasoning_effort": "minimal"}).chat(
        [{"role": "user", "content": "hi"}], reasoning_effort="low")
    assert state["payloads"][0]["reasoning_effort"] == "low"


def test_extra_still_works_when_no_field_set():
    client, state = _capturing_client()
    _llm_with(client, extra={"reasoning_effort": "minimal"}).chat(
        [{"role": "user", "content": "hi"}])
    assert state["payloads"][0]["reasoning_effort"] == "minimal"


# ---------------------------------------------------------------- config 读取
def test_load_config_reads_reasoning_effort(tmp_path):
    ws = tmp_path / ".openthree"
    ws.mkdir()
    (ws / "config.yaml").write_text(
        "llm:\n  api_key: k\n  base_url: u\n  reasoning_effort: high\n",
        encoding="utf-8")
    cfg = load_config(tmp_path)
    assert cfg.reasoning_effort == "high"


def test_load_config_defaults_to_unset(tmp_path):
    ws = tmp_path / ".openthree"
    ws.mkdir()
    (ws / "config.yaml").write_text("llm:\n  api_key: k\n  base_url: u\n",
                                    encoding="utf-8")
    assert load_config(tmp_path).reasoning_effort is None


def test_load_config_reads_reasoning_content_replay(tmp_path):
    ws = tmp_path / ".openthree"
    ws.mkdir()
    (ws / "config.yaml").write_text(
        "llm:\n  api_key: k\n  base_url: u\n  reasoning_content_replay: always\n",
        encoding="utf-8")
    assert load_config(tmp_path).reasoning_content_replay == "always"


def test_load_config_defaults_replay_to_auto(tmp_path):
    """默认 auto：多数端点白拿一个多余键就 400，宁可少补也不要乱补。"""
    ws = tmp_path / ".openthree"
    ws.mkdir()
    (ws / "config.yaml").write_text("llm:\n  api_key: k\n  base_url: u\n",
                                    encoding="utf-8")
    assert load_config(tmp_path).reasoning_content_replay == "auto"


def test_load_config_keeps_false_as_never(tmp_path):
    """YAML 里的 `false` 是「我明确要关掉」，不能被当成假值悄悄回落 auto。"""
    ws = tmp_path / ".openthree"
    ws.mkdir()
    (ws / "config.yaml").write_text(
        "llm:\n  api_key: k\n  base_url: u\n  reasoning_content_replay: false\n",
        encoding="utf-8")
    assert resolve_reasoning_placeholder(
        load_config(tmp_path).reasoning_content_replay,
        base_url="https://api.deepseek.com", model="m", reasoning_active=True) is None


# ---------------------------------------------------------------- 三个字段名
def test_extract_reasoning_finds_each_known_field_name():
    """三个名字都是 OpenAI 兼容生态里真实存在的写法，缺一个就丢一整段 reasoning。"""
    for field in REASONING_FIELDS:
        assert extract_reasoning({field: "想一想"}) == (field, "想一想")


def test_extract_reasoning_prefers_the_first_non_empty_field():
    msg = {"reasoning_content": "", "reasoning": "b", "reasoning_text": "c"}
    assert extract_reasoning(msg) == ("reasoning", "b")


def test_extract_reasoning_ignores_missing_and_non_string():
    assert extract_reasoning({}) is None
    assert extract_reasoning(None) is None
    assert extract_reasoning({"reasoning_content": None}) is None
    assert extract_reasoning({"reasoning_content": ""}) is None


# ---------------------------------------------------------------- 占位策略
def test_auto_detects_deepseek_by_base_url():
    assert resolve_reasoning_placeholder(
        "auto", base_url="https://api.deepseek.com/v1", model="m",
        reasoning_active=True) == "reasoning_content"


def test_auto_detects_deepseek_by_model_prefix():
    """base_url 常是自建代理（域名里没有 deepseek.com），model 名是第二个信号。"""
    assert resolve_reasoning_placeholder(
        "auto", base_url="https://gw.internal/v1", model="deepseek-v4.1-flash",
        reasoning_active=True) == "reasoning_content"


def test_auto_stays_silent_for_unrelated_endpoints():
    assert resolve_reasoning_placeholder(
        "auto", base_url="https://api.openai.com/v1", model="gpt-4o",
        reasoning_active=True) is None


def test_auto_requires_reasoning_to_be_active():
    """推理没开着就不补：给不认识的端点塞多余键同样会被 400。"""
    assert resolve_reasoning_placeholder(
        "auto", base_url="https://api.deepseek.com/v1", model="m",
        reasoning_active=False) is None


def test_always_and_never_override_the_detection():
    assert resolve_reasoning_placeholder(
        "always", base_url="https://gw.internal/v1", model="m") == "reasoning_content"
    assert resolve_reasoning_placeholder(
        "never", base_url="https://api.deepseek.com/v1", model="m",
        reasoning_active=True) is None


def test_unknown_mode_falls_back_to_auto():
    """配置里写错值不该把功能关掉，也不该变成无条件补 —— 按 auto 走。"""
    assert resolve_reasoning_placeholder(
        "sometimes", base_url="https://gw.internal/v1", model="m",
        reasoning_active=True) is None
    assert resolve_reasoning_placeholder(
        "sometimes", base_url="https://api.deepseek.com", model="m",
        reasoning_active=True) == "reasoning_content"


# ---------------------------------------------------------------- Agent 一侧
class _StubLLM:
    """记下 Agent 传了什么、按脚本回复；不联网。"""

    def __init__(self, replies, **cfg):
        # 默认 base_url / model 都刻意「不像 DeepSeek」：测试要显式选择端点，
        # 而不是靠默认值蒙对。
        cfg.setdefault("base_url", "https://gw.internal/v1")
        cfg.setdefault("model", "m")
        self.config = LLMConfig(api_key="k", **cfg)
        self._replies = list(replies)
        self.calls = []

    def chat(self, messages, tools=None, tool_choice="auto", max_tokens=None,
             temperature=None, reasoning_effort=None):
        self.calls.append({"reasoning_effort": reasoning_effort,
                           "messages": messages})
        return self._replies.pop(0)


def _reply(message, error=None):
    return {"message": message, "usage": {}, "elapsed_ms": 1.0,
            "raw_request": {}, "raw_response": {}, "error": error, "attempts": 1}


def _run(make_agent, stub, **agent_kwargs):
    ag = make_agent(**agent_kwargs)
    ag.llm = stub
    ag.task_text = "测试"
    ag.run()
    return ag


def _assistants(history):
    return [m for m in history if m.get("role") == "assistant"]


def test_agent_passes_explicit_effort_and_returns_reasoning(make_agent):
    stub = _StubLLM([_reply({"role": "assistant", "content": "做完",
                             "reasoning_content": "先想想…"})])
    ag = _run(make_agent, stub, reasoning_effort="low")

    assert stub.calls[0]["reasoning_effort"] == "low"
    assistant = _assistants(ag.history)
    assert assistant and assistant[-1]["reasoning_content"] == "先想想…"


def test_agent_replays_under_the_field_name_the_provider_used(make_agent):
    """端点用 `reasoning` 吐就用 `reasoning` 回放 —— 改名等于它认不出来。"""
    stub = _StubLLM([_reply({"role": "assistant", "content": "做完",
                             "reasoning": "网关用的名字"})],
                    base_url="https://api.deepseek.com/v1")
    ag = _run(make_agent, stub)

    assistant = _assistants(ag.history)[-1]
    assert assistant["reasoning"] == "网关用的名字"


def test_agent_adds_both_keys_when_endpoint_uses_another_field(make_agent):
    """用 `reasoning` 吐、却要求 `reasoning_content` 的端点：两个键都得有。"""
    stub = _StubLLM([_reply({"role": "assistant", "content": "做完",
                             "reasoning_text": "另一种名字"})],
                    base_url="https://api.deepseek.com/v1")
    ag = _run(make_agent, stub)

    assistant = _assistants(ag.history)[-1]
    assert assistant["reasoning_text"] == "另一种名字"
    assert assistant["reasoning_content"] == ""


def test_agent_pads_empty_placeholder_when_the_turn_has_no_reasoning(make_agent):
    """模型某轮不吐 reasoning，端点仍要求字段存在 —— 补空串，否则下一请求 400。"""
    stub = _StubLLM([_reply({"role": "assistant", "content": "做完"})],
                    base_url="https://api.deepseek.com/v1", reasoning_effort="high")
    ag = _run(make_agent, stub)

    assert _assistants(ag.history)[-1]["reasoning_content"] == ""


def test_agent_omits_reasoning_key_for_endpoints_that_do_not_want_it(make_agent):
    """auto 判不出 DeepSeek 家族时一个键都不加 —— 多余键同样会被 400。"""
    stub = _StubLLM([_reply({"role": "assistant", "content": "做完"})],
                    reasoning_effort="high")
    ag = _run(make_agent, stub, reasoning_effort="high")

    assert stub.calls[0]["reasoning_effort"] == "high"
    assistant = _assistants(ag.history)[-1]
    assert assistant.get("reasoning_content") is None


def test_agent_injects_placeholder_for_always_mode(make_agent):
    """always 是逃生口：端点判不出来（或默认就在想）时由人显式打开。"""
    stub = _StubLLM([_reply({"role": "assistant", "content": "做完"})],
                    reasoning_content_replay="always")
    ag = _run(make_agent, stub)

    assert _assistants(ag.history)[-1]["reasoning_content"] == ""


def test_never_mode_adds_no_placeholder_but_still_replays(make_agent):
    """never 只管占位，不管回放 —— 真收到过 reasoning 必须原样带上。"""
    stub = _StubLLM([_reply({"role": "assistant", "content": "做完",
                             "reasoning_content": "真的想过"})],
                    base_url="https://api.deepseek.com/v1",
                    reasoning_content_replay="never")
    ag = _run(make_agent, stub, reasoning_effort="high")

    assert _assistants(ag.history)[-1]["reasoning_content"] == "真的想过"


def test_reasoning_is_actually_sent_on_the_next_request(make_agent):
    """真正要守的不变量：第二步不再 400。断言落在**请求体**上，不是 history 上。"""
    tool_call = {"id": "c1", "type": "function",
                 "function": {"name": "no_such_tool", "arguments": "{}"}}
    stub = _StubLLM([
        _reply({"role": "assistant", "content": "", "tool_calls": [tool_call],
                "reasoning_content": "第一步想了很久"}),
        _reply({"role": "assistant", "content": "做完"}),
    ], base_url="https://api.deepseek.com/v1", reasoning_effort="high")
    _run(make_agent, stub)

    assert len(stub.calls) == 2
    sent = [m for m in stub.calls[1]["messages"] if m.get("role") == "assistant"]
    assert sent and sent[0]["reasoning_content"] == "第一步想了很久"


def test_agent_treats_extra_effort_as_reasoning_active(make_agent):
    """老配置用 `extra` 透传 effort：chat() 会真的把它发出去 → 端点进入推理模式。

    此时若判「推理没开」就不补占位，下一次请求照样 400。
    """
    stub = _StubLLM([_reply({"role": "assistant", "content": "做完"})],
                    base_url="https://api.deepseek.com/v1",
                    extra={"reasoning_effort": "high"})
    ag = _run(make_agent, stub)

    assert _assistants(ag.history)[-1]["reasoning_content"] == ""


def test_seeded_assistant_turns_get_the_placeholder(make_agent):
    """`-i` 第 2 轮播种进来的 assistant 消息也要补占位 —— 否则第一条就 400。"""
    stub = _StubLLM([_reply({"role": "assistant", "content": "这一轮做完"})],
                    base_url="https://api.deepseek.com/v1", reasoning_effort="high")
    ag = make_agent(seed_turns=[{"user": "上一轮问题", "answer": "上一轮答案"}])
    ag.llm = stub
    # 播种发生在 __init__，那时挂的还是内核的默认 LLM；换成桩之后重跑同一条路径。
    ag.history = ag._seed_history()
    ag.task_text = "这一轮"
    ag.run()

    seeded = _assistants(ag.history)[0]
    assert seeded["content"] == "上一轮答案"
    assert seeded["reasoning_content"] == ""
    # 要守的是「真的发出去了」：播种消息排在请求体最前
    sent = [m for m in stub.calls[0]["messages"] if m.get("role") == "assistant"]
    assert sent[0]["reasoning_content"] == ""


def test_compaction_node_gets_the_placeholder(make_agent):
    """折叠摘要节点是内核自己造的 assistant 消息 —— 它缺占位，折叠后下一次请求就 400。"""
    stub = _StubLLM([_reply({"role": "assistant", "content": "做完"})],
                    base_url="https://api.deepseek.com/v1", reasoning_effort="high")
    ag = make_agent()
    ag.llm = stub
    for i in (1, 2):
        ag.history.append({
            "role": "assistant", "content": f"think {i}",
            "tool_calls": [{"id": f"c{i}", "type": "function",
                            "function": {"name": "x", "arguments": "{}"}}],
        })
        ag.history.append({"role": "tool", "tool_call_id": f"c{i}",
                           "name": "x", "content": "r"})

    ok, _ = ag.compact(1, 1, "摘要")
    assert ok

    node = ag.history[0]
    assert node["role"] == "assistant" and "已折叠" in node["content"]
    assert node["reasoning_content"] == ""


# ---------------------------------------------------------------- 迟到识别
def test_placeholder_backfills_history_when_reasoning_shows_up_late(make_agent):
    """前几轮不吐 reasoning、某轮才吐：早先的 assistant 消息要回头补齐。

    判据里含「本场见过 reasoning」，它翻真之前追加的消息一律拿不到占位；
    端点要的是**每条** assistant 都有这个键 —— 缺口留在历史里就是 400。
    """
    tool_call = {"id": "c1", "type": "function",
                 "function": {"name": "no_such_tool", "arguments": "{}"}}
    stub = _StubLLM([
        _reply({"role": "assistant", "content": "", "tool_calls": [tool_call]}),
        _reply({"role": "assistant", "content": "", "tool_calls": [tool_call],
                "reasoning_content": "这回想了"}),
        _reply({"role": "assistant", "content": "做完"}),
    ], base_url="https://api.deepseek.com/v1")
    ag = _run(make_agent, stub)

    # 翻真之后的请求：历史里每一条 assistant 都必须带占位
    for call in stub.calls[2:]:
        sent = [m for m in call["messages"] if m.get("role") == "assistant"]
        assert sent and all("reasoning_content" in m for m in sent)
    assert _assistants(ag.history)[0]["reasoning_content"] == ""


def test_seeded_turns_get_backfilled_when_reasoning_arrives(make_agent):
    """`-i` 播种的 assistant 消息在播种那一刻判不出「推理开着」，翻真后要补上。"""
    stub = _StubLLM([_reply({"role": "assistant", "content": "做完",
                             "reasoning_content": "想了"})],
                    base_url="https://api.deepseek.com/v1")
    ag = _run(make_agent, stub,
              seed_turns=[{"user": "上一轮问题", "answer": "上一轮回答"}])

    seeded = _assistants(ag.history)[0]
    assert seeded["content"] == "上一轮回答"
    assert seeded["reasoning_content"] == ""


# ---------------------------------------------------------------- 配置与日志
def test_legacy_requires_reasoning_content_applies_when_new_key_is_empty(tmp_path):
    """新键留空（没写 / 写 null / 写空串）时旧键仍然生效 —— 升级配置不该丢掉它。"""
    ws = tmp_path / ".openthree"
    ws.mkdir()
    (ws / "config.yaml").write_text(
        "llm:\n  api_key: k\n  base_url: u\n"
        "  reasoning_content_replay:\n  requires_reasoning_content: true\n",
        encoding="utf-8")
    cfg = load_config(tmp_path)

    assert is_always_replay(cfg.reasoning_content_replay)
    assert resolve_reasoning_placeholder(
        cfg.reasoning_content_replay, base_url="https://gw.internal/v1",
        model="m") == REASONING_PLACEHOLDER_FIELD


def test_redact_truncates_logged_reasoning_without_touching_the_request():
    """回放让每个请求重带历史 reasoning；日志全量落盘 = 体积随步数近似平方增长。"""
    original = "想" * 500
    payload = {"api_key": "secret", "messages": [
        {"role": "assistant", "content": "x", "reasoning_content": original}]}
    out = _redact(payload)

    logged = out["messages"][0]["reasoning_content"]
    assert "日志省略" in logged and len(logged) < len(original)
    assert payload["messages"][0]["reasoning_content"] == original   # 真请求没被改
    assert "api_key" not in out


def test_replay_label_tells_the_truth_about_never_and_unknown_values():
    """两个容易骗人的地方：`never` 不等于「完全不回传」；写错的取值要如实说出。"""
    from openthree.runtime import Runtime

    class _Stub:      # reasoning_replay_label 只读 self.config
        pass

    stub = _Stub()
    stub.config = LLMConfig(api_key="k", base_url="https://gw.internal/v1",
                            reasoning_content_replay="never")
    assert "never" in Runtime.reasoning_replay_label(stub)

    stub.config = LLMConfig(api_key="k", base_url="https://gw.internal/v1",
                            reasoning_content_replay="sometimes")
    label = Runtime.reasoning_replay_label(stub)
    assert "sometimes" in label and "auto" in label


# ---------------------------------------------------------------- 上下文计量
def test_estimate_chars_counts_reasoning():
    """reasoning 会被每次请求重发，漏计它 = 系统性低估用量（折叠阈值偏高）。"""
    history = [{"role": "assistant", "content": "短",
                "reasoning_content": "想" * 100}]
    assert _estimate_chars(history) >= 100
