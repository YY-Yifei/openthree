"""Agent 层：工具结果截断/落盘、上下文预算、折叠校验、finish 语义。"""
from __future__ import annotations

import json

from openthree.agent import _estimate_chars, _index_steps, _parse_args, _safe_name
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
    ok, _ = ag.compact(1, 1, "第一步")
    assert ok
    ag.compact(2, 2, "第二步")
    ag._apply_pending_compactions()
    assert len(ag.history) == 1
    assert "已折叠" in ag.history[0]["content"]


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


# ---------------------------------------------------------------- 其他
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
