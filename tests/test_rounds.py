"""「轮」是一等概念：步/轮/会话分层、每轮独立步预算、撞上限自动续轮、next_round。

用户诉求：「step 现在有上限是否合理？是否没考虑模型改完计划/prompts 后其实已是新的一轮？
应该加上『轮』，每轮有 step 上限。」

设计：
  * 步（step）= ReAct 的一次「思 → 行」；
  * 轮（round）= 一次完整 ReAct 循环 = 一个 Agent 的生命周期，**每轮独立一份步预算**；
  * 会话（session）= 进程内的多轮容器。
回归点：单次任务撞步数上限不再「直接死」，而是在轮预算内自动续下一轮。
"""
from __future__ import annotations

import json

from openthree.agent import DEFAULT_MAX_ROUNDS
from openthree.builtins import next_round
from openthree.kernel import KERNEL
from openthree.prompt import build_runtime_block
from openthree.recorder import Recorder
from openthree.runtime import Runtime
from openthree.workspace import Workspace


class _NeverFinishLLM:
    """永远只调 get_time，从不 finish —— 必然撞步数上限。"""

    def __init__(self, cfg):
        self.config = cfg
        self.calls = 0

    def chat(self, messages, tools=None, tool_choice="auto", **kwargs):
        self.calls += 1
        msg = {"role": "assistant", "content": "继续",
               "tool_calls": [{"id": f"c{self.calls}", "type": "function",
                               "function": {"name": "get_time", "arguments": "{}"}}]}
        return {"message": msg, "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                "elapsed_ms": 0.0, "raw_request": {"messages": []},
                "raw_response": {"choices": [{"message": msg}]}, "error": None}


class _FinishLLM:
    """一次就 finish。"""

    def __init__(self, cfg, answer="搞定"):
        self.config = cfg
        self.answer = answer

    def chat(self, messages, tools=None, tool_choice="auto", **kwargs):
        msg = {"role": "assistant", "content": "",
               "tool_calls": [{"id": "c", "type": "function",
                               "function": {"name": "finish",
                                            "arguments": json.dumps({"answer": self.answer})}}]}
        return {"message": msg, "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                "elapsed_ms": 0.0, "raw_request": {"messages": []},
                "raw_response": {"choices": [{"message": msg}]}, "error": None}


def _rt(tmp_path, llm_cls=_NeverFinishLLM, **kw) -> Runtime:
    rt = Runtime(tmp_path, quiet=True, heartbeat=0, **kw)
    rt.config.api_key = rt.config.base_url = "stub"
    rt.config.model = "stub-model"
    rt.llm = llm_cls(rt.config)
    return rt


# ---------------------------------------------------------------- 边界读数
def test_max_rounds_default_and_source(make_agent):
    ag = make_agent()
    assert ag.max_rounds == DEFAULT_MAX_ROUNDS == 3
    assert ag.limits_stats()["max_rounds"] == 3
    assert ag.limits_stats()["source"]["max_rounds"] == "内核默认"


def test_max_rounds_settable_and_locked(make_agent):
    ag = make_agent()
    ok, msg = ag.set_limits(max_rounds=7)
    assert ok and ag.max_rounds == 7
    data = json.loads(ag.ws.limits_file.read_text(encoding="utf-8"))
    assert data["max_rounds"] == 7
    lk = make_agent(max_rounds=5)
    assert not lk.set_limits(max_rounds=9)[0]              # 显式锁定后改不动
    assert lk.max_rounds == 5


def test_max_rounds_rejects_zero(make_agent):
    ag = make_agent()
    assert not ag.set_limits(max_rounds=0)[0]


def test_max_rounds_from_file_clamped(make_agent):
    ag = make_agent()
    ag.ws.limits_file.write_text(json.dumps({"max_rounds": 0}), encoding="utf-8")
    ag.tick()
    assert ag.max_rounds == 1                              # 0 夹到至少 1 轮


# ---------------------------------------------------------------- 处境轮读数
def test_prompt_shows_round_and_step_line(ws, registry):
    block = build_runtime_block(
        ws, registry, model="m", iteration=7, max_iterations=200,
        round_stats={"index": 2, "max": 3, "remaining": 1,
                     "autonomous": True, "next": True})
    assert "这是第 2 轮（本场最多 3 轮）" in block
    assert "本轮第 7 步（上限 200）" in block


def test_prompt_round_line_backcompat_without_stats(ws, registry):
    block = build_runtime_block(ws, registry, model="m", iteration=1, max_iterations=200)
    assert "这是第 1 轮（本场最多 1 轮）" in block          # 无 stats 时退回 1/1


def test_prompt_limits_line_mentions_rounds(ws, registry):
    block = build_runtime_block(
        ws, registry, model="m", iteration=1, max_iterations=200,
        limits_stats={"max_iterations": 200, "max_rounds": 3, "max_tokens": 8192,
                      "tool_result_limit": 4000, "temperature": 0.3, "locked": False})
    assert "最多 3 轮" in block


# ---------------------------------------------------------------- 撞步数上限自动续轮
def test_single_run_continues_round_on_step_limit(tmp_path):
    """核心回归：单次任务撞步数上限不再直接死，而是在轮预算内自动开下一轮。"""
    rt = _rt(tmp_path, max_iterations=3, max_rounds=2)
    doc = rt.run("长任务")
    assert doc["rounds_run"] == 2                          # 续了一轮
    assert doc["step_limit_hit"] is True                   # 最后一轮仍没跑完
    assert len(doc["round_answers"]) == 2
    # 每轮都拿到独立的一份步预算：总步数 = 轮数 × 每轮上限
    assert doc["stats"]["steps"] == 3                      # 最后一轮自己的步数
    assert doc["max_rounds"] == 2


def test_single_run_stops_at_max_rounds(tmp_path):
    rt = _rt(tmp_path, max_iterations=2, max_rounds=1)
    doc = rt.run("长任务")
    assert doc["rounds_run"] == 1                          # 轮预算=1，不续
    assert doc["step_limit_hit"] is True


def test_single_run_finish_means_no_extra_round(tmp_path):
    """正常 finish = 整件事收尾，不该被 step_limit_hit 拖着再开一轮。"""
    rt = _rt(tmp_path, llm_cls=_FinishLLM, max_iterations=50, max_rounds=5)
    doc = rt.run("小事一桩")
    assert doc["rounds_run"] == 1
    assert doc["step_limit_hit"] is False
    assert doc["answer"] == "搞定"


def test_single_run_own_session_is_discarded(tmp_path):
    rt = _rt(tmp_path, max_iterations=2, max_rounds=2)
    rt.run("长任务")
    assert rt._session_active is False                     # 自带会话已关
    assert rt._session_turns == []                         # 不残留到下一次任务
    assert len(rt.ws.read_turns(-1)) == 2                  # 但每轮都入档


def test_run_session_true_defers_to_caller(tmp_path):
    """session=True 时不接管会话：跨调用累积（-i 语义不受影响）。"""
    rt = _rt(tmp_path, llm_cls=_FinishLLM)
    rt.run("第一句", session=True)
    rt.run("第二句", session=True)
    assert rt._session_active is True
    assert len(rt._session_turns) == 2


def test_run_auto_reports_step_limit_hit(tmp_path):
    """--auto 路径：撞上限也算「没做完」，自动接力（auto_answers 多轮）。"""
    rt = _rt(tmp_path, max_iterations=2, max_rounds=2)
    doc = rt.run_auto("长任务", rounds=2)
    assert doc["auto_rounds"] == 2
    assert len(doc["auto_answers"]) == 2


# ---------------------------------------------------------------- next_round
def _agent(ws, registry, llm, *, round_index=1, autonomous=False, max_rounds=3):
    rec = Recorder(ws, user_input="t", model="s")
    from openthree.agent import Agent
    ag = Agent(ws, registry, llm, rec, round_index=round_index,
               autonomous=autonomous, max_rounds=max_rounds)
    KERNEL.workspace = ws
    KERNEL.registry = registry
    KERNEL.recorder = rec
    KERNEL.agent = ag
    return ag


def test_round_stats_shape(ws, registry, make_agent):
    ag = make_agent(max_rounds=3)
    st = ag.round_stats()
    assert st["index"] == 1 and st["max"] == 3 and st["remaining"] == 2
    assert st["autonomous"] is False and st["next"] is False


def test_next_round_marks_wants_next(ws, registry):
    from openthree.llm import LLM, LLMConfig
    ag = _agent(ws, registry, LLM(LLMConfig(api_key="k", base_url="u")),
                round_index=1, autonomous=True, max_rounds=3)
    out = next_round(summary="做完一半")
    assert ag.wants_next_round is True and ag.finished is True
    assert "下一轮" in out and "第 1 轮" in out


def test_next_round_without_autonomy_says_so(ws, registry):
    from openthree.llm import LLM, LLMConfig
    ag = _agent(ws, registry, LLM(LLMConfig(api_key="k", base_url="u")),
                round_index=1, autonomous=False, max_rounds=3)
    out = next_round(summary="x")
    assert "没有下一轮" in out
    assert ag.wants_next_round is True
