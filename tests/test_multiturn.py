"""多轮对话：用户的完整原始输入必须跨轮保留，且模型跨轮记得住。

这是用户诉求（「多轮对话模式应该保留用户的完整输入」）的直接回归测试。
"""
from __future__ import annotations

import json

from openthree.agent import Agent
from openthree.llm import LLM, LLMConfig
from openthree.recorder import Recorder
from openthree.workspace import Workspace


# ---------------------------------------------------------------- 档案层
def test_append_turn_is_append_only(ws: Workspace):
    ws.append_turn("我叫小明，请记住", "好的")
    ws.append_turn("我叫什么？", "你叫小明")
    lines = ws.transcript_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2                       # 每轮一行，只增不改
    # 第一条原文一个字都不能少 —— 这正是不用 user-prompts.md 当档案的原因
    assert json.loads(lines[0])["user"] == "我叫小明，请记住"
    assert json.loads(lines[1])["user"] == "我叫什么？"


def test_transcript_is_valid_jsonl_with_escaped_newlines(ws: Workspace):
    ws.append_turn("第一行\n第二行", "答")
    lines = ws.transcript_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1                       # 换行被转义，不破坏行结构
    assert json.loads(lines[0])["user"] == "第一行\n第二行"


def test_read_turns_skips_corrupted_lines(ws: Workspace):
    ws.append_turn("好的一轮", "答")
    with ws.transcript_file.open("a", encoding="utf-8") as f:
        f.write("{半截的坏行\n")              # 进程被 kill 可能留下半行
    ws.append_turn("后面一轮", "答")
    turns = ws.read_turns(-1)
    assert [t["user"] for t in turns] == ["好的一轮", "后面一轮"]


def test_append_turn_survives_workspace_reinit(ws: Workspace):
    ws.append_turn("原文一字不动", "答")
    Workspace(ws.root).init()          # 再初始化一次工作区
    assert "原文一字不动" in ws.transcript_file.read_text(encoding="utf-8")


def test_read_turns_roundtrip(ws: Workspace):
    ws.append_turn("第一句", "第一答")
    ws.append_turn("第二句", "第二答")
    turns = ws.read_turns(4)
    assert [t["user"] for t in turns] == ["第一句", "第二句"]
    assert [t["answer"] for t in turns] == ["第一答", "第二答"]


def test_read_turns_multiline_and_special_chars(ws: Workspace):
    tricky = ("第一行\n第二行\n\n### 用户\n（这是正文，不是分隔符）\n"
              "## 2020-01-01T00:00:00\n{\"user\": 假 JSON}")
    ws.append_turn(tricky, "收到")
    assert ws.read_turns(1)[0]["user"] == tricky


def test_read_turns_respects_n(ws: Workspace):
    for i in range(6):
        ws.append_turn(f"u{i}", f"a{i}")
    assert len(ws.read_turns(2)) == 2
    assert ws.read_turns(2)[-1]["user"] == "u5"
    assert len(ws.read_turns(-1)) == 6      # -1 = 全部
    assert ws.read_turns(0) == []           # 0 = 关闭


def test_read_turns_on_empty_workspace(ws: Workspace):
    assert ws.read_turns(4) == []


def test_submit_append_drops_seed_placeholder(ws: Workspace):
    ws.submit("第一件事", append=True)
    text = ws.read_user_prompt()
    assert "等待用户输入" not in text      # 种子占位符不该被当成历史任务
    assert ws.has_task() is True


# ---------------------------------------------------------------- Agent 层
def _agent(ws, registry, prior_turns=None) -> Agent:
    rec = Recorder(ws, user_input="t", model="stub")
    return Agent(ws, registry, LLM(LLMConfig(api_key="k", base_url="u")), rec,
                 prior_turns=prior_turns)


def test_agent_seeds_prior_turns(ws, registry):
    ws.append_turn("我叫小明，请记住", "好的")
    ws.append_turn("我叫什么？", "你叫小明")
    ag = _agent(ws, registry)
    assert ag.history[0] == {"role": "user", "content": "我叫小明，请记住"}
    assert ag.history[1] == {"role": "assistant", "content": "好的"}
    assert ag.history[2] == {"role": "user", "content": "我叫什么？"}
    assert ag.history[3] == {"role": "assistant", "content": "你叫小明"}


def test_agent_seed_is_bounded_by_prior_turns(ws, registry):
    for i in range(5):
        ws.append_turn(f"u{i}", f"a{i}")
    ag = _agent(ws, registry, prior_turns=2)
    assert len(ag.history) == 4
    assert ag.history[0]["content"] == "u3"


def test_agent_prior_turns_zero_disables_memory(ws, registry):
    ws.append_turn("我叫小明", "好的")
    ag = _agent(ws, registry, prior_turns=0)
    assert ag.history == []


def test_agent_without_prior_turns_defaults_to_four(ws, registry):
    for i in range(6):
        ws.append_turn(f"u{i}", f"a{i}")
    ag = _agent(ws, registry)          # 不传 = 默认 4 轮
    assert len(ag.history) == 8
    assert ag.history[0]["content"] == "u2"


def test_agent_records_seed_event(ws, registry):
    ws.append_turn("我叫小明", "好的")
    ag = _agent(ws, registry)
    assert any(e.kind == "system" and "播种" in e.summary for e in ag.rec.events)


def test_agent_history_still_balanced_after_seed(ws, registry):
    ws.append_turn("问题", "答案")
    ag = _agent(ws, registry)
    # 播种只产生 user/assistant，不会破坏 tool_call / tool 的配对
    assert all(m["role"] in ("user", "assistant") for m in ag.history)


# ---------------------------------------------------------------- Runtime 端到端
class _RecordingLLM:
    """记下每次真正发给模型的 messages，然后 finish。"""

    def __init__(self, cfg, answer: str = "收到"):
        self.config = cfg
        self.answer = answer
        self.calls: list = []

    def chat(self, messages, tools=None, tool_choice="auto"):
        self.calls.append(messages)
        msg = {"role": "assistant", "content": "",
               "tool_calls": [{"id": "c1", "type": "function",
                               "function": {"name": "finish",
                                            "arguments": json.dumps({"answer": self.answer})}}]}
        return {"message": msg, "usage": {}, "elapsed_ms": 1.0,
                "raw_request": {"model": "stub", "messages": messages},
                "raw_response": {"choices": [{"message": msg}]}, "error": None}


def _runtime(tmp_path, **kw):
    from openthree.runtime import Runtime
    rt = Runtime(tmp_path, **kw)
    rt.config.api_key = rt.config.base_url = "stub"
    rt.config.model = "stub-model"
    rt.llm = _RecordingLLM(rt.config)
    return rt


def test_runtime_second_turn_remembers_first(tmp_path):
    """用户的原话：第二轮必须看得到第一轮的完整输入。"""
    rt = _runtime(tmp_path)
    rt.run("我叫小明，请记住")
    rt.run("我叫什么？")

    second = rt.llm.calls[-1]
    texts = [str(m.get("content")) for m in second]
    assert any("我叫小明，请记住" in t for t in texts)   # 完整原文，不是摘要
    assert any("我叫什么？" in t for t in texts)
    # 当前任务照旧在 system 里（三）；上一轮以 user/assistant 播种在消息层。
    assert second[0]["role"] == "system" and "我叫什么？" in second[0]["content"]
    assert [m["role"] for m in second] == ["system", "user", "assistant"]
    assert second[1]["content"] == "我叫小明，请记住"


def test_runtime_transcript_keeps_full_input(tmp_path):
    rt = _runtime(tmp_path)
    rt.run("我叫小明，请记住")
    rt.run("我叫什么？")
    turns = rt.ws.read_turns(-1)
    assert [t["user"] for t in turns] == ["我叫小明，请记住", "我叫什么？"]


def test_runtime_turns_zero_is_single_turn(tmp_path):
    rt = _runtime(tmp_path, prior_turns=0)
    rt.run("我叫小明，请记住")
    rt.run("我叫什么？")
    assert len(rt.llm.calls[-1]) == 1                    # 只剩 system
    assert len(rt.ws.read_turns(-1)) == 2                # 但原文照样入档


def test_runtime_turns_limits_seeded_rounds(tmp_path):
    rt = _runtime(tmp_path, prior_turns=1)
    rt.run("第一句")
    rt.run("第二句")
    rt.run("第三句")
    texts = [str(m.get("content")) for m in rt.llm.calls[-1]]
    assert not any("第一句" in t for t in texts)          # 只播种最近 1 轮
    assert any("第二句" in t for t in texts)
