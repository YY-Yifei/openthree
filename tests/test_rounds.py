"""「轮」是一等概念：步/轮/会话分层、每轮独立步预算、撞上限自动续轮、next_round。

三层定义（见 prompt.py）：
  * 步（step）= ReAct 的一次「思 → 行」；
  * 轮（round）= 一次完整 ReAct 循环 = 一个 Agent 的生命周期，**每轮独立一份步预算**；
  * 会话（session）= 进程内的多轮容器。

被保护的不变量：单次任务撞步数上限**不会直接死**，而是在轮预算内自动续下一轮。
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
    assert "这是第 2 轮（本场最多 3 轮，这一轮收尾后还有下一轮）" in block
    assert "本轮第 7 步（上限 200）" in block


def test_prompt_hides_next_hint_when_budget_exhausted(ws, registry):
    """轮预算用尽（next=False）时，处境不再写「还有下一轮」。"""
    block = build_runtime_block(
        ws, registry, model="m", iteration=1, max_iterations=200,
        round_stats={"index": 3, "max": 3, "remaining": 0,
                     "autonomous": True, "next": False})
    assert "这是第 3 轮（本场最多 3 轮）" in block
    assert "还有下一轮" not in block


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
# ---------------------------------------------------------------- 轮预算「一个真相」
class _CountRoundsLLM:
    """永远只调 get_time —— 必然撞步数上限，逼运行期开满轮预算。"""

    def __init__(self, cfg):
        self.config = cfg
        self.calls = 0

    def chat(self, messages, tools=None, tool_choice="auto", **kwargs):
        self.calls += 1
        self.last_messages = messages
        msg = {"role": "assistant", "content": "继续",
               "tool_calls": [{"id": f"c{self.calls}", "type": "function",
                               "function": {"name": "get_time", "arguments": "{}"}}]}
        return {"message": msg, "usage": {}, "elapsed_ms": 0.0, "raw_request": {},
                "raw_response": {"choices": [{"message": msg}]}, "error": None}


def test_rounds_arg_takes_precedence_over_knob(tmp_path):
    """显式 rounds 就是本场轮预算：`run(rounds=4)` 跑满 4 轮，旋钮默认不改它。"""
    rt = _rt(tmp_path, llm_cls=_CountRoundsLLM, max_iterations=1, max_rounds=2)
    doc = rt.run("长任务", rounds=4)
    assert doc["rounds_run"] == 4
    assert doc["max_rounds"] == 4        # 处境告诉模型的本场轮数也就是 4


def test_prompt_reports_effective_round_budget(tmp_path):
    """处境里的「本场最多 M 轮」= 运行期真正执行的轮数。"""
    import re
    rt = _rt(tmp_path, llm_cls=_CountRoundsLLM, max_iterations=1, max_rounds=2)
    rt.run("长任务", rounds=3)
    blob = json.dumps(rt.llm.last_messages, ensure_ascii=False)
    assert re.search(r"本场最多 3 轮", blob)                    # 与 rounds=3 一致
    assert re.search(r"最多 3 轮（整场）", blob)                # 运行边界读数同数


def test_run_auto_min_with_explicit_max_rounds(tmp_path):
    """`--max-rounds M` 与 `--auto N` 都在说「整场几轮」——同时给取较小者。"""
    rt = _rt(tmp_path, llm_cls=_NeverFinishLLM, max_iterations=1, max_rounds=4)
    rt.ws.write_plan("# P\n- [ ] 永远没完\n")
    doc = rt.run_auto("长任务", rounds=9)
    assert doc["auto_rounds"] == 4                              # min(9, 4)


def test_run_auto_without_explicit_max_rounds_uses_auto_n(tmp_path):
    """默认旋钮（非显式）不该悄悄把 `--auto 5` 截成 3。"""
    rt = _rt(tmp_path, llm_cls=_NeverFinishLLM, max_iterations=1)   # 未传 max_rounds
    rt.ws.write_plan("# P\n - [ ] 永远没完\n")
    doc = rt.run_auto("长任务", rounds=5)
    assert doc["auto_rounds"] == 5


def test_single_run_continues_round_on_step_limit(tmp_path):
    """核心不变量：单次任务撞步数上限不会直接死，而是在轮预算内自动开下一轮。"""
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
def _agent(ws, registry, llm, *, round_index=1, round_budget=None, max_rounds=3):
    rec = Recorder(ws, user_input="t", model="s")
    from openthree.agent import Agent
    ag = Agent(ws, registry, llm, rec, round_index=round_index,
               round_budget=round_budget, max_rounds=max_rounds)
    KERNEL.workspace = ws
    KERNEL.registry = registry
    KERNEL.recorder = rec
    KERNEL.agent = ag
    return ag


def test_round_stats_shape(ws, registry, make_agent):
    ag = make_agent(max_rounds=3)
    st = ag.round_stats()
    assert st["index"] == 1 and st["max"] == 3 and st["remaining"] == 2
    # 没有轮预算（单轮）时 next=False；给了预算就有下一轮
    assert st["autonomous"] is False and st["next"] is True


def test_round_stats_next_reflects_budget_not_autonomous(ws, registry):
    """next 只取决于「还有没有轮预算」—— 单次任务也有下一轮（不再绑 autonomous）。"""
    from openthree.llm import LLM, LLMConfig
    ag = _agent(ws, registry, LLM(LLMConfig(api_key="k", base_url="u")),
                round_index=1, round_budget=3)
    st = ag.round_stats()
    assert st["max"] == 3 and st["next"] is True and st["autonomous"] is True


def test_next_round_marks_wants_next(ws, registry):
    from openthree.llm import LLM, LLMConfig
    ag = _agent(ws, registry, LLM(LLMConfig(api_key="k", base_url="u")),
                round_index=1, round_budget=3, max_rounds=3)
    out = next_round(summary="做完一半")
    assert ag.wants_next_round is True and ag.finished is True
    assert "下一轮" in out and "第 1 轮" in out


def test_next_round_without_budget_says_no_next(ws, registry):
    """轮预算用尽（round_budget=1，已是第 1 轮）时，next_round 如实说没有下一轮。"""
    from openthree.llm import LLM, LLMConfig
    ag = _agent(ws, registry, LLM(LLMConfig(api_key="k", base_url="u")),
                round_index=1, round_budget=1, max_rounds=3)
    out = next_round(summary="x")
    assert "没有下一轮" in out
    assert ag.wants_next_round is True


# ---------------------------------------------------------------- 交接说明真能过轮
def test_next_round_summary_is_stored_round_write(ws, registry):
    """next_round(summary=…) 不只是记日志 —— 说明必须存下来，供下一轮取用。"""
    from openthree.llm import LLM, LLMConfig
    ag = _agent(ws, registry, LLM(LLMConfig(api_key="k", base_url="u")),
                round_index=1, round_budget=3)
    next_round(summary="做完一半，下一轮收尾")
    assert ag.next_round_summary == "做完一半，下一轮收尾"


def test_next_round_summary_injected_into_history(ws, registry):
    """Runtime 把上一轮的交接说明带来 → 本轮 history 开头出现一条 system 便条。"""
    from openthree.agent import Agent
    from openthree.llm import LLM, LLMConfig
    ag = Agent(ws, registry, LLM(LLMConfig(api_key="k", base_url="u")),
               Recorder(ws, user_input="t", model="s"),
               next_round_summary="上一轮到哪了")
    ag._inject_next_round_summary()
    assert ag.history[0]["role"] == "system"
    assert "上一轮到哪了" in ag.history[0]["content"]
    # 只注入一次（第二步不该再来一条）
    first = len(ag.history)
    ag._inject_next_round_summary()
    assert len(ag.history) == first


def test_run_carries_next_round_summary_across_rounds(tmp_path):
    """端到端：第一轮 next_round 的 summary，第二轮的历史开头真的能看到。"""
    seen = {}

    class _TwoRoundLLM:
        """第 1 轮 next_round 交代交接；第 2 轮检查 history 里有没有它，然后 finish。"""

        def __init__(self, cfg):
            self.config = cfg
            self.round = 0

        def chat(self, messages, tools=None, tool_choice="auto", **kwargs):
            self.round += 1
            if self.round == 1:
                name, args = "next_round", {"summary": "第一轮交接词"}
            else:
                blob = json.dumps(messages, ensure_ascii=False)
                seen["leaked"] = "第一轮交接词" in blob
                name, args = "finish", {"answer": "done"}
            msg = {"role": "assistant", "content": "",
                   "tool_calls": [{"id": f"c{self.round}", "type": "function",
                                   "function": {"name": name,
                                                "arguments": json.dumps(args)}}]}
            return {"message": msg, "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                    "elapsed_ms": 0.0, "raw_request": {"messages": []},
                    "raw_response": {"choices": [{"message": msg}]}, "error": None}

    rt = _rt(tmp_path, llm_cls=_TwoRoundLLM, max_rounds=2)
    doc = rt.run("开始长任务")
    assert doc["rounds_run"] == 2                      # 第 1 轮 next_round → 续第 2 轮
    assert seen.get("leaked") is True                  # 交接词跨轮进了第二轮的 prompt
