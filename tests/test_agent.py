"""Agent 层：工具结果截断/落盘、上下文预算、折叠校验、finish 语义。"""
from __future__ import annotations

import json

from openthree.agent import (_estimate_chars, _index_steps, _parse_args, _safe_name,
                             resolve_context_budget)
from openthree.workspace import Workspace

from conftest import step


# ---------------------------------------------------------------- 工具结果截断
def test_spill_truncates_and_persists(make_agent, ws: Workspace):
    ag = make_agent(tool_result_limit=200)
    out = ag._spill("call_9", "run_shell", "A" * 5000)
    assert len(out) < 1000
    assert "5000" in out and "tool-output" in out
    assert (ws.logs_dir / "tool-output" / "call_9.txt").read_text(encoding="utf-8") == "A" * 5000


def test_spill_keeps_short_output(make_agent):
    ag = make_agent(tool_result_limit=200)
    assert ag._spill("c", "x", "短") == "短"


def test_spill_can_be_disabled(make_agent):
    ag = make_agent(tool_result_limit=0)
    assert ag._spill("c", "x", "B" * 5000) == "B" * 5000


def test_safe_name_strips_path_chars():
    assert "/" not in _safe_name("a/b\\c:d")
    assert _safe_name("") == "call"


# ---------------------------------------------------------------- 上下文预算
def test_auto_compact_shrinks_history(make_agent):
    ag = make_agent(context_budget_chars=900)
    for i in range(12):
        step(ag, i, tool_chars=300)
    before = _estimate_chars(ag.history)
    ag._auto_compact()
    after = _estimate_chars(ag.history)
    assert after < before
    assert any("自动折叠" in e.summary for e in ag.rec.events)


def test_auto_compact_noop_within_budget(make_agent):
    ag = make_agent(context_budget_chars=10_000_000)
    for i in range(3):
        step(ag, i)
    n = len(ag.history)
    ag._auto_compact()
    assert len(ag.history) == n


def test_auto_compact_disabled(make_agent):
    ag = make_agent(context_budget_chars=0)
    for i in range(6):
        step(ag, i, tool_chars=5000)
    n = len(ag.history)
    ag._auto_compact()
    assert len(ag.history) == n


# ---------------------------------------------------------------- 预算默认值
def test_default_budget_follows_context_window():
    """默认预算按上下文窗口推导：1M 窗口 → 100 万字符。"""
    assert resolve_context_budget(None, 1_000_000) == 1_000_000
    assert resolve_context_budget(None, 128_000) == 128_000
    # 超长窗口被硬上限兜住
    assert resolve_context_budget(None, 10_000_000_000) == 1_000_000


def test_default_budget_is_1m_without_window():
    assert resolve_context_budget(None, None) == 1_000_000


def test_explicit_budget_wins_and_zero_disables():
    assert resolve_context_budget(1234, 100_000_000) == 1234
    assert resolve_context_budget(0, 100_000_000) == 0


def test_agent_default_budget_derives_from_llm_config(make_agent):
    """Agent 缺省 context_budget_chars 时，从 llm.config.context_window_tokens 推导。"""
    ag = make_agent()
    ag.llm.config.context_window_tokens = 128_000
    # 重新构造以读取新的窗口值
    from openthree.agent import Agent
    ag2 = Agent(ag.ws, ag.reg, ag.llm, ag.rec)
    assert ag2.context_budget_chars == 128_000


# ---------------------------------------------------------------- 折叠校验
def test_compact_rejects_out_of_range(make_agent):
    ag = make_agent()
    step(ag, 1)
    step(ag, 2)
    ok, msg = ag.compact(9, 12, "乱折")
    assert not ok and "1-2" in msg


def test_compact_rejects_no_steps(make_agent):
    ag = make_agent()
    ok, msg = ag.compact(1, 2, "空历史")
    assert not ok


def test_compact_rejects_swapped_bounds(make_agent):
    ag = make_agent()
    step(ag, 1)
    ok, _ = ag.compact(2, 1, "反了")
    assert not ok


def test_compact_accepts_and_folds(make_agent):
    ag = make_agent()
    step(ag, 1)
    step(ag, 2)
    # 折 step 1（带摘要）→ 立即生效：摘要节点 + 未折的 step 2
    ok, _ = ag.compact(1, 1, "第一步")
    assert ok
    assert len(ag.history) == 3
    assert "已折叠 step 1-1" in ag.history[0]["content"]
    assert ag.history[1]["content"] == "think 2"


def test_compact_rejects_last_step(make_agent):
    """最后一步（你正在走的）不许折，否则会孤儿化紧随其后的 tool 消息。"""
    ag = make_agent()
    for i in range(3):
        step(ag, i)
    ok, msg = ag.compact(3, 3, "最后一步")
    assert not ok and "最后一步" in msg


def test_compact_empty_summary_drops(make_agent):
    """空摘要 = 直接丢弃：不留任何占位节点，省得最彻底。"""
    ag = make_agent()
    step(ag, 1)
    step(ag, 2)
    ok, msg = ag.compact(1, 1, "")
    assert ok and "丢弃" in msg
    assert len(ag.history) == 2          # 只剩 step 2 的 assistant + tool，没有摘要节点
    assert not any("已折叠" in str(m.get("content")) for m in ag.history)


# ---------------------------------------------------------------- finish 语义
def test_finish_skips_remaining_tool_calls(make_agent, tmp_path):
    ag = make_agent()
    sentinel = tmp_path / "SENTINEL.txt"
    ag.finish("done")
    ag._run_tool_calls([
        {"id": "c1", "function": {"name": "write_file",
                                  "arguments": json.dumps({"path": str(sentinel),
                                                           "content": "x"})}},
    ])
    assert not sentinel.exists()
    assert ag.history and "跳过" in ag.history[0]["content"]


def test_tool_messages_balance_tool_calls(make_agent):
    ag = make_agent()
    ag._run_tool_calls([
        {"id": "c1", "function": {"name": "read_file",
                                  "arguments": json.dumps({"path": "nope.txt"})}},
    ])
    assert len(ag.history) == 1
    assert ag.history[0]["role"] == "tool" and ag.history[0]["tool_call_id"] == "c1"


# ---------------------------------------------------------------- 截断可恢复
def _stub_llm(results):
    """返回一个按顺序吐出预设结果的假 LLM（记录收到的 messages）。"""
    calls = {"n": 0, "messages": []}

    class Stub:
        class config:
            model = "stub"
            context_window_tokens = 1_000_000
            max_tokens = 8192

        def chat(self, messages, tools=None, tool_choice="auto"):
            calls["messages"].append(messages)
            r = results[min(calls["n"], len(results) - 1)]
            calls["n"] += 1
            return r

    return Stub(), calls


def test_truncation_is_retried_not_fatal(make_agent):
    """finish_reason=length 可恢复：应要求模型重发，而不是整场任务失败。"""
    from openthree.agent import _is_truncation
    trunc = {"message": {"role": "assistant", "content": "半截"},
             "error": "截断", "error_kind": "truncated", "finish_reason": "length"}
    ok = {"message": {"role": "assistant", "content": "好的",
                      "tool_calls": [{"id": "c1", "type": "function",
                                      "function": {"name": "finish",
                                                   "arguments": '{"answer": "done"}'}}]},
          "error": None, "finish_reason": "stop"}
    assert _is_truncation(trunc) and not _is_truncation(ok)

    ag = make_agent()
    stub, calls = _stub_llm([trunc, ok])
    ag.llm = stub
    ag.max_truncations = 2
    answer = ag.run()
    assert ag.finished and answer == "done"
    assert ag.truncations == 1
    assert calls["n"] == 2                       # 截断后重发了一次
    # 重发时的 system 里带上了「被截断、请重发」的提示
    sys_text = calls["messages"][1][0]["content"]
    assert "截断" in sys_text
    # 被截断的那条回复不该污染 history
    assert all(m.get("content") != "半截" for m in ag.history)


def test_truncation_gives_up_after_limit(make_agent):
    trunc = {"message": {"role": "assistant", "content": "半截"},
             "error": "截断", "error_kind": "truncated", "finish_reason": "length"}
    ag = make_agent()
    stub, calls = _stub_llm([trunc])
    ag.llm = stub
    ag.max_truncations = 1
    answer = ag.run()
    assert "max_tokens" in answer
    assert calls["n"] == 2                        # 1 次原发 + 1 次重发
def test_parse_args_handles_junk():
    assert _parse_args({"a": 1}) == {"a": 1}
    assert _parse_args("") == {}
    assert _parse_args('{"a": 1}') == {"a": 1}
    assert "value" in _parse_args("{not json")


def test_index_steps_counts_tool_batches():
    hist = []
    for i in range(3):
        hist.append({"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}"}]})
        hist.append({"role": "tool", "tool_call_id": f"c{i}", "name": "x", "content": "y"})
    idx = _index_steps(hist)
    assert sorted(idx) == [1, 2, 3]
    assert idx[1] == (0, 1)
    assert idx[3] == (4, 5)
