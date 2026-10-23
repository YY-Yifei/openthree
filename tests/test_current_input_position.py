"""本轮输入的位置：它必须是「本步 ReAct 之前最后一条真实的用户消息」。

为什么值得单独一个文件守：任务正文曾经埋在 system 深处，而消息层尾部停在
上一轮的一问一答。模型对最近一条人话最敏感，于是它读到的「用户刚说的话」
就成了上一轮的内容 —— 表现为「在处理上一次消息」。修法不是调措辞，而是把
本轮输入放回它本该在的位置：history 里的一条 user 消息，紧接在上一轮之后。

被保护的不变量：
  P1  会话里连发两条消息，任务段在**第二条**输入上（不是第一条）。
  P2  任务段是一条 user 消息，且排在自维护段 / 此刻段之前（即属于 history）。
  P3  system 里不再有任务正文（它变了会让工具 schema 的缓存前缀每轮失效）。
  P4  `write_user_prompt` 改了任务 → 同一条消息原地刷新，不新增重复消息。
  P5  纯图片输入不回退到磁盘上的旧任务。
"""
from __future__ import annotations

import json

import pytest

from openthree.agent import Agent
from openthree.llm import LLM, LLMConfig
from openthree.recorder import Recorder
from openthree.runtime import Runtime
from openthree.workspace import Workspace

TASK_MARK = "# 当前任务（三）"

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40


@pytest.fixture
def png(tmp_path):
    p = tmp_path / "pic.png"
    p.write_bytes(PNG_BYTES)
    return p


# ---------------------------------------------------------------- stub LLM
class _FinishLLM:
    def __init__(self, cfg, answer="ok"):
        self.config = cfg
        self.answer = answer
        self.calls = []

    def chat(self, messages, tools=None, tool_choice="auto", **kwargs):
        self.calls.append(messages)
        msg = {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c", "type": "function",
             "function": {"name": "finish",
                          "arguments": json.dumps({"answer": self.answer})}}]}
        return {"message": msg, "usage": {}, "elapsed_ms": 0.0,
                "raw_request": {}, "raw_response": {"choices": [{"message": msg}]},
                "error": None}


def _runtime(tmp_path, **kw) -> Runtime:
    rt = Runtime(tmp_path, quiet=True, heartbeat=0, **kw)
    rt.config.api_key = rt.config.base_url = "stub"
    rt.config.model = "stub-model"
    rt.llm = _FinishLLM(rt.config)
    return rt


def _flatten(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def _task_slots(call) -> list[int]:
    """哪几条 **history** 消息带任务段（正常应恰好 1 条）。

    刻意只扫 history 区段（system 之后、自维护段之前）：计划段里的正文也是
    模型自己写的话，它完全可能字面写到「当前任务（三）」这几个字 —— 那是
    内容，不是位置。按位置判断才不会被内容污染。
    """
    end = _context_start(call)
    return [i for i, m in enumerate(call[:end])
            if i > 0 and TASK_MARK in _flatten(m.get("content"))]


def _context_start(call) -> int:
    """自维护段的起点（`# 你的计划`）—— 它之前是 history，之后是运行期拼的尾巴。"""
    for i, m in enumerate(call):
        if _flatten(m.get("content")).startswith("# 你的计划"):
            return i
    return len(call)


def _task_body(msg) -> str:
    return _flatten(msg.get("content")).split(TASK_MARK, 1)[1].strip()


# ================================================================ P1 / P2 / P3
def test_current_input_is_the_latest_user_message_in_history(tmp_path):
    """连发两条：任务段落在第二条输入上，且属于 history。"""
    rt = _runtime(tmp_path)
    rt.begin_session()
    rt.run("第一问：读一下 A 文件")
    rt.run("第二问：改一下 B 文件")

    call = rt.llm.calls[-1]
    slots = _task_slots(call)
    assert len(slots) == 1, f"任务段应恰好出现 1 次，实际 {slots}"

    idx = slots[0]
    assert call[idx]["role"] == "user"                 # P2：它是用户说的话
    assert "第二问" in _task_body(call[idx])            # P1：是本轮，不是上一轮
    assert "第一问" not in _task_body(call[idx])

    # P2：它排在自维护段之前 —— 即属于 history，而不是末尾那条「此刻的处境」
    ctx_idx = next(i for i, m in enumerate(call)
                   if _flatten(m.get("content")).startswith("# 你的计划"))
    assert idx < ctx_idx

    # 上一轮仍在上下文里（当上下文保留，不被本轮覆盖）
    assert any("第一问" in _flatten(m.get("content")) for m in call)

    # P3：system 里没有任务正文
    assert TASK_MARK not in _flatten(call[0].get("content"))


def test_current_input_sits_right_after_previous_turn(tmp_path):
    """它紧接在上一轮的 user/assistant 之后 —— 位置就是语义。"""
    rt = _runtime(tmp_path)
    rt.begin_session()
    rt.run("第一问")
    rt.run("第二问")

    roles = [m["role"] for m in rt.llm.calls[-1]]
    assert roles == ["system", "user", "assistant", "user", "user", "user"]
    # [3] 是本轮输入，[4]/[5] 是运行期拼的自维护段与此刻段
    assert TASK_MARK in _flatten(rt.llm.calls[-1][3]["content"])
    assert _flatten(rt.llm.calls[-1][4]["content"]).startswith("# 你的计划")
    assert _flatten(rt.llm.calls[-1][5]["content"]).startswith("# 此刻的处境")


# ================================================================ P4：原地刷新
def _agent(ws, registry, **kw) -> Agent:
    rec = Recorder(ws, user_input="t", model="stub")
    return Agent(ws, registry, LLM(LLMConfig(api_key="k", base_url="u")), rec, **kw)


def test_syncing_twice_does_not_duplicate(ws, registry):
    ag = _agent(ws, registry)
    ag.task_text = "同一句话"
    ag._sync_current_input()
    n = len(ag.history)
    ag._sync_current_input()
    assert len(ag.history) == n                        # 幂等
    assert ag.history[-1]["role"] == "user"


def test_rewriting_task_refreshes_the_same_message(ws, registry):
    """模型用 write_user_prompt 改任务：原地刷新，不新增第二条。"""
    ag = _agent(ws, registry)
    ag.task_text = "旧任务"
    ag._sync_current_input()
    n = len(ag.history)

    ag.task_text = "改过的任务"
    ag._sync_current_input()

    assert len(ag.history) == n                        # 没有重复消息
    assert "改过的任务" in _flatten(ag.history[-1]["content"])
    assert "旧任务" not in _flatten(ag.history[-1]["content"])


# ================================================================ P5：纯图片
def test_image_only_turn_does_not_fall_back_to_disk_task(ws, registry, png):
    """有图无字：记成「（图片输入）」，绝不回退到磁盘上的旧任务。"""
    ws.submit("磁盘上的旧任务")
    ag = _agent(ws, registry)
    ag.task_text = ""
    ag.current_images = [str(png)]
    ag._sync_current_input()

    body = _task_body(ag.history[-1])
    assert body == "（图片输入）"
    assert "磁盘上的旧任务" not in body
