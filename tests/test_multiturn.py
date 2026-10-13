"""多轮记忆：记忆只活在**进程内会话**里，不再每次从磁盘读回。

用户诉求的两个面：
  * 用户的完整原始输入必须跨轮保留（档案层，append-only）；
  * 但**单次任务绝不自动读盘** —— 只有 `-i` / `--auto` 的会话才跨轮可见，
    想续读旧档案得用 `--resume` 显式要求。
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
def _agent(ws, registry, seed=None) -> Agent:
    rec = Recorder(ws, user_input="t", model="stub")
    return Agent(ws, registry, LLM(LLMConfig(api_key="k", base_url="u")), rec,
                 seed_turns=seed)


def _turns(*pairs):
    return [{"user": u, "answer": a} for u, a in pairs]


def test_agent_seeds_explicit_turns(ws, registry):
    ag = _agent(ws, registry,
                seed=_turns(("我叫小明，请记住", "好的"), ("我叫什么？", "你叫小明")))
    assert ag.history[0] == {"role": "user", "content": "我叫小明，请记住"}
    assert ag.history[1] == {"role": "assistant", "content": "好的"}
    assert ag.history[2] == {"role": "user", "content": "我叫什么？"}
    assert ag.history[3] == {"role": "assistant", "content": "你叫小明"}


def test_agent_without_seed_starts_empty(ws, registry):
    """回归：Agent 不再自动读盘 —— 没显式给轮次，历史就是空的。"""
    ws.append_turn("上一轮的旧对话", "旧回答")
    ag = _agent(ws, registry)                  # 不给 seed_turns
    assert ag.history == []                     # 磁盘有档案，但不读它


def test_agent_seed_takes_only_what_it_is_given(ws, registry):
    """播种什么就播什么：裁剪轮数是调用方（Runtime）的职责。"""
    ag = _agent(ws, registry, seed=_turns(("u0", "a0"), ("u1", "a1")))
    assert len(ag.history) == 4


def test_agent_records_seed_event(ws, registry):
    ag = _agent(ws, registry, seed=_turns(("我叫小明", "好的")))
    assert any(e.kind == "system" and "播种" in e.summary for e in ag.rec.events)


def test_agent_history_still_balanced_after_seed(ws, registry):
    ag = _agent(ws, registry, seed=_turns(("问题", "答案")))
    # 播种只产生 user/assistant，不会破坏 tool_call / tool 的配对
    assert all(m["role"] in ("user", "assistant") for m in ag.history)


# ---------------------------------------------------------------- Runtime 端到端
class _RecordingLLM:
    """记下每次真正发给模型的 messages，然后 finish。"""

    def __init__(self, cfg, answer: str = "收到"):
        self.config = cfg
        self.answer = answer
        self.calls: list = []

    def chat(self, messages, tools=None, tool_choice="auto", **kwargs):
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


def test_runtime_single_run_does_not_read_transcript(tmp_path):
    """本次诉求的直接回归：单次任务绝不去读上一次的对话。"""
    rt = _runtime(tmp_path)
    rt.run("我叫小明，请记住")
    rt.run("我叫什么？")

    second = rt.llm.calls[-1]
    texts = [str(m.get("content")) for m in second]
    assert not any("我叫小明，请记住" in t for t in texts)   # 不读盘，从零开跑
    assert [m["role"] for m in second] == ["system"]         # 没有任何播种
    # 但档案照写不误（可观测 / 可续读）
    assert [t["user"] for t in rt.ws.read_turns(-1)] == ["我叫小明，请记住", "我叫什么？"]


def test_runtime_session_remembers_prior_turns(tmp_path):
    """-i 那样的会话：开了会话，第二轮才看得到第一轮。"""
    rt = _runtime(tmp_path)
    rt.begin_session()
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


def test_runtime_run_session_flag_opens_session(tmp_path):
    """不显式 begin_session，也可以逐轮 session=True 累积。"""
    rt = _runtime(tmp_path)
    rt.run("第一句", session=True)
    rt.run("第二句", session=True)
    texts = [str(m.get("content")) for m in rt.llm.calls[-1]]
    assert any("第一句" in t for t in texts)
    assert any("第二句" in t for t in texts)


def test_runtime_end_session_forgets(tmp_path):
    rt = _runtime(tmp_path)
    rt.begin_session()
    rt.run("我叫小明，请记住")
    rt.end_session()
    rt.run("我叫什么？")
    assert [m["role"] for m in rt.llm.calls[-1]] == ["system"]   # 会话已关，不播种


def test_runtime_resume_reads_transcript_explicitly(tmp_path):
    """--resume 是唯一的读盘入口：显式要求才把档案读回会话。"""
    rt = _runtime(tmp_path)
    rt.run("我叫小明，请记住")            # 单次，写档案
    loaded = rt.begin_session(resume=1)  # 显式续读
    assert loaded == 1
    rt.run("我叫什么？")
    texts = [str(m.get("content")) for m in rt.llm.calls[-1]]
    assert any("我叫小明，请记住" in t for t in texts)   # 读回来了
    assert any("收到" in t for t in texts)              # 上一轮的结论也在


def test_runtime_transcript_keeps_full_input(tmp_path):
    rt = _runtime(tmp_path)
    rt.run("我叫小明，请记住")
    rt.run("我叫什么？")
    turns = rt.ws.read_turns(-1)
    assert [t["user"] for t in turns] == ["我叫小明，请记住", "我叫什么？"]


def test_runtime_turns_zero_is_single_turn(tmp_path):
    """会话里 turns=0 = 关闭记忆（但原文照样入档）。"""
    rt = _runtime(tmp_path, turns=0)
    rt.begin_session()
    rt.run("我叫小明，请记住")
    rt.run("我叫什么？")
    assert len(rt.llm.calls[-1]) == 1                    # 只剩 system
    assert len(rt.ws.read_turns(-1)) == 2                # 但原文照样入档


def test_runtime_turns_limits_seeded_rounds(tmp_path):
    rt = _runtime(tmp_path, turns=1)
    rt.begin_session()
    rt.run("第一句")
    rt.run("第二句")
    rt.run("第三句")
    texts = [str(m.get("content")) for m in rt.llm.calls[-1]]
    assert not any("第一句" in t for t in texts)          # 只播种最近 1 轮
    assert any("第二句" in t for t in texts)


def test_runtime_effective_turns_bounds_session(tmp_path):
    rt = _runtime(tmp_path, turns=2)
    rt.begin_session()
    for i in range(5):
        rt._session_turns.append({"user": f"u{i}", "answer": f"a{i}"})
    eff = rt._effective_turns()
    assert len(eff) == 2 and eff[0]["user"] == "u3"


def test_runtime_effective_turns_empty_without_session(tmp_path):
    rt = _runtime(tmp_path)
    rt._session_turns.append({"user": "x", "answer": "y"})   # 有存货但没开会话
    assert rt._effective_turns() == []
