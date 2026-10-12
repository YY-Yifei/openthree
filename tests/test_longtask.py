"""长任务：柔性提示词种子、计划（plan.md）、提示词版本回滚、自主多轮推进。

对应用户诉求（「种子 prompt 应柔性、引导模型完善；支持长任务 / 自改提示词 /
自主多轮推进 / 长时间工作」）的回归测试。
"""
from __future__ import annotations

import json

from openthree.builtins import (list_prompt_versions, read_plan,
                                restore_system_prompt, write_plan,
                                write_system_prompt)
from openthree.kernel import KERNEL
from openthree.llm import LLMConfig
from openthree.prompt import build_runtime_block
from openthree.runtime import Runtime
from openthree.workspace import Workspace


# ---------------------------------------------------------------- 柔性种子
def test_seed_prompt_is_soft_and_invites_self_edit(ws: Workspace):
    seed = ws.read_system_prompt()
    # 明确邀请模型自己去完善它，而不是一份刚性定论
    assert "第一稿" in seed
    assert "不是你的枷锁" in seed
    # 长任务机制被写进种子，模型一上来就知道有这些手
    for tool_name in ("write_plan", "read_plan", "write_system_prompt",
                      "list_prompt_versions", "restore_system_prompt"):
        assert tool_name in seed


def test_init_seeds_plan_file(ws: Workspace):
    assert ws.plan_file.exists()
    assert "当前计划" in ws.read_plan()


# ---------------------------------------------------------------- 计划
def test_plan_open_items_counts_only_unchecked(ws: Workspace):
    assert ws.plan_open_items() == 0            # 种子占位符不算
    ws.write_plan("# 计划\n\n- [x] 做完的\n- [ ] 没做完的\n- [ ] 也没做完\n纯文本行\n- [X] 大写的也算完成\n")
    assert ws.plan_open_items() == 2


def test_write_plan_tool_roundtrip(ws: Workspace):
    KERNEL.workspace = ws
    out = write_plan("- [ ] 其一\n- [ ] 其二")
    assert "未完成 2 项" in out
    text = read_plan()
    assert "其一" in text and "未完成条目：2" in text


# ---------------------------------------------------------------- 提示词版本/回滚
def test_write_system_prompt_snapshots_old_version(ws: Workspace):
    original = ws.read_system_prompt()
    ws.write_system_prompt("改坏的稿子")
    versions = ws.list_prompt_versions()
    assert len(versions) == 1
    assert original.splitlines()[0] in versions[0].read_text(encoding="utf-8")


def test_restore_system_prompt_rolls_back(ws: Workspace):
    original = ws.read_system_prompt()
    ws.write_system_prompt("改坏的稿子")
    name = ws.restore_system_prompt("1")            # 1 = 最新存档 = 原稿
    assert name and name.endswith(".md")
    assert "改坏的稿子" not in ws.read_system_prompt()
    assert original.splitlines()[0] in ws.read_system_prompt()
    # 恢复动作本身也留了后路：现稿（改坏的那版）被存了一份
    assert len(ws.list_prompt_versions()) == 2


def test_restore_unknown_version_returns_none(ws: Workspace):
    assert ws.restore_system_prompt("99") is None
    assert ws.restore_system_prompt("不存在.md") is None


def test_prompt_version_tools(ws: Workspace):
    KERNEL.workspace = ws
    assert "还没有历史版本" in list_prompt_versions()
    write_system_prompt("新版")
    listing = list_prompt_versions()
    assert "1." in listing and "restore_system_prompt" in listing
    out = restore_system_prompt("1")
    assert out.startswith("[ok]") and "回退" in out
    assert "找不到版本" in restore_system_prompt("zzz")


def test_init_is_idempotent_keeps_plan(tmp_path):
    ws = Workspace(tmp_path).init()
    ws.write_plan("- [ ] 保留我")
    Workspace(tmp_path).init()
    assert "保留我" in ws.read_plan()


# ---------------------------------------------------------------- 计划注入上下文
def test_runtime_block_includes_plan(ws, registry):
    ws.write_plan("- [ ] 待办甲")
    block = build_runtime_block(ws, registry, model="m", iteration=1, max_iterations=200)
    assert "# 你的计划" in block
    assert "待办甲" in block
    assert "还有 1 项未完成" in block


def test_runtime_block_marks_done_plan(ws, registry):
    ws.write_plan("- [x] 干完了")
    block = build_runtime_block(ws, registry, model="m", iteration=1, max_iterations=200)
    assert "所有条目都已勾选" in block


# ---------------------------------------------------------------- 自主多轮推进
class _PlanStubLLM:
    """每调用一次就写一份计划 + finish；用 plan 的未完成项驱动自动接力。"""

    def __init__(self, cfg, plans):
        self.config = cfg
        self.plans = list(plans)
        self.round = 0

    def chat(self, messages, tools=None, tool_choice="auto"):
        i = min(self.round, len(self.plans) - 1)
        plan = self.plans[i]
        self.round += 1
        calls = [
            {"id": f"p{self.round}", "type": "function",
             "function": {"name": "write_plan",
                          "arguments": json.dumps({"content": plan}, ensure_ascii=False)}},
            {"id": f"f{self.round}", "type": "function",
             "function": {"name": "finish",
                          "arguments": json.dumps({"answer": f"第{self.round}轮结论"},
                                                  ensure_ascii=False)}},
        ]
        msg = {"role": "assistant", "content": "", "tool_calls": calls}
        return {"message": msg, "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                "elapsed_ms": 0.0, "raw_request": {"messages": []},
                "raw_response": {"choices": [{"message": msg}]}, "error": None}


def _rt(tmp_path, plans, turns=4) -> Runtime:
    rt = Runtime(tmp_path, quiet=True, heartbeat=0, turns=turns)
    rt.config.api_key = rt.config.base_url = "stub"
    rt.config.model = "stub-model"
    rt.llm = _PlanStubLLM(rt.config, plans)
    return rt


def test_run_auto_keeps_going_until_plan_done(tmp_path):
    rt = _rt(tmp_path, [
        "- [ ] 甲\n- [ ] 乙",     # 2 未完成 → 继续
        "- [x] 甲\n- [ ] 乙",     # 1 未完成 → 继续
        "- [x] 甲\n- [x] 乙",     # 0 未完成 → 收工
    ])
    doc = rt.run_auto("长任务", rounds=5)
    assert doc["auto_rounds"] == 3
    assert doc["auto_answers"] == ["第1轮结论", "第2轮结论", "第3轮结论"]
    assert rt.ws.plan_open_items() == 0


def test_run_auto_stops_at_round_cap(tmp_path):
    rt = _rt(tmp_path, ["- [ ] 永远做不完"])
    doc = rt.run_auto("长任务", rounds=2)
    assert doc["auto_rounds"] == 2                 # 被上限拦住，不是无限跑
    assert rt.ws.plan_open_items() == 1


def test_run_auto_single_round_when_plan_done(tmp_path):
    rt = _rt(tmp_path, ["- [x] 一次就好"])
    doc = rt.run_auto("长任务", rounds=5)
    assert doc["auto_rounds"] == 1


def test_run_auto_produces_multiturn_memory(tmp_path):
    """自动接力的轮次也进 transcript —— 下一轮记得住上一轮说过什么。"""
    rt = _rt(tmp_path, ["- [ ] 甲", "- [x] 甲"])
    rt.run_auto("长任务", rounds=3)
    turns = rt.ws.read_turns(-1)
    assert len(turns) == 2
    assert turns[0]["answer"] == "第1轮结论"


def test_run_auto_uses_and_discards_its_own_session(tmp_path):
    """一次 --auto = 一个进程内会话：接力中累积，收尾后即弃。"""
    rt = _rt(tmp_path, ["- [ ] 甲", "- [x] 甲"])
    doc = rt.run_auto("长任务", rounds=3)
    assert doc["auto_rounds"] == 2
    assert rt._session_active is False          # 自己开的会话已关闭
    assert rt._session_turns == []              # 记忆不残留
    assert len(rt.ws.read_turns(-1)) == 2       # 但档案照写


def test_run_auto_keeps_caller_session_open(tmp_path):
    """-i 里调用：沿用已有会话，接力结束后它继续存在（跨命令记忆）。"""
    rt = _rt(tmp_path, ["- [x] 一次就好"])
    rt.begin_session()
    rt.run_auto("长任务", rounds=2)
    assert rt._session_active is True                    # 未被 run_auto 关掉
    assert any("长任务" in t["user"] for t in rt._session_turns)
