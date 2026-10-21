"""三层拆分之后，「自我修改下一步生效」这条承诺必须继续成立。

为什么这个文件与 `test_prompt_prefix_cache.py` 分开：那个文件保护的是**缓存
不变量**（system 逐字节稳定、此刻段在末尾、两段不写回 history）；而缓存优化
存在一个天然的诱惑——为了「让前缀更稳定」，把会变的东西藏起来、缓存起来、
或者干脆晚一步再让模型看见。

**但 openthree 的全部能力长在「改一下，下一步就生效」上**：模型能改自己的
提示词、任务、计划、记忆、名字、工具、运行边界。任何一条悄悄退化成「下一轮
才生效」或「根本看不见」，自我演化就断了，而缓存命中率反而会看起来很漂亮。

所以这里守住五条对照断言，每条都是「模型做了一次自我修改 → 下一步真的看见了」：

  E1  write_plan            → 计划出现在下一步的自维护段
  E2  remember              → 记忆出现在下一步的自维护段
  E3  write_system_prompt   → 新提示词出现在下一步的 system
  E4  write_function+reload → 新工具出现在下一步的 system 工具清单
  E5  set_identity          → 新名字出现在下一步的 system

与缓存不变量并置起来看，这五条正好构成这次改动的**双向约束**：
E1/E2 说明「可变的东西挪出了 system」，I1/I4 说明「挪出去之后 system 确实稳了」。
两边都不许单独立法。
"""
from __future__ import annotations

import json

from openthree.agent import Agent
from openthree.kernel import KERNEL
from openthree.llm import LLM, LLMConfig
from openthree.recorder import Recorder
from openthree.workspace import Workspace


def _ok(msg) -> dict:
    return {"message": msg, "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            "elapsed_ms": 0.0, "raw_request": {"messages": []},
            "raw_response": {"choices": [{"message": msg}]},
            "error": None, "finish_reason": "stop", "attempts": 1}


class _Scripted:
    """按剧本走：第 k 步调一个自我改造工具，之后 finish。记下每一步收到的 messages。"""

    def __init__(self, config, script):
        self.config = config
        self.script = script          # {step: (tool_name, args_dict)}
        self.calls = []
        self.step = 0

    def chat(self, messages, tools=None, tool_choice="auto", **kw):
        self.step += 1
        self.calls.append([dict(m) for m in messages])
        if self.step in self.script:
            name, args = self.script[self.step]
            return _ok({"role": "assistant", "content": f"第 {self.step} 步：{name}",
                        "tool_calls": [{"id": f"c{self.step}", "type": "function",
                                        "function": {"name": name,
                                                     "arguments": json.dumps(args,
                                                                             ensure_ascii=False)}}]})
        return _ok({"role": "assistant", "content": "",
                    "tool_calls": [{"id": f"c{self.step}", "type": "function",
                                    "function": {"name": "finish",
                                                 "arguments": json.dumps({"answer": "完"})}}]})

    # 便于断言：第 k 步（1-based）收到的 system / 自维护段
    def system(self, k: int) -> str:
        return self.calls[k - 1][0]["content"]

    def context(self, k: int) -> str:
        return self.calls[k - 1][-2]["content"]


def _run(ws: Workspace, registry, script) -> _Scripted:
    """把 Agent 跑完，并把各部件挂进 KERNEL（内置工具的自我演化依赖它）。"""
    llm = _Scripted(LLMConfig(api_key="k", base_url="u"), script)
    rec = Recorder(ws, user_input="t", model="stub")
    ag = Agent(ws, registry, LLM(LLMConfig(api_key="k", base_url="u")), rec)
    ag.llm = llm
    KERNEL.workspace, KERNEL.registry, KERNEL.recorder, KERNEL.agent = ws, registry, rec, ag
    ws.write_current_task("测试任务")
    ag.run()
    return llm


# ---------------------------------------------------------------- E1 / E2
def test_plan_and_memory_written_last_step_are_visible_next_step(ws, registry):
    """计划与记忆是模型自维护的文件 —— 写下的下一步必须看得见。"""
    script = {
        1: ("write_plan", {"content": "# 计划\n\n- [ ] 甲\n"}),
        2: ("remember", {"content": "记忆：三角洲协议", "tags": "probe"}),
    }
    llm = _run(ws, registry, script)

    # 第 2 步：计划已在自维护段里
    assert "- [ ] 甲" in llm.context(2)
    # 第 3 步：记忆也在
    assert "三角洲协议" in llm.context(3)
    # 而它们都不在 system 里 —— 那正是缓存不变量要求的位置
    assert "- [ ] 甲" not in llm.system(2)
    assert "三角洲协议" not in llm.system(3)


# ---------------------------------------------------------------- E3 / E5
def test_system_prompt_and_identity_written_last_step_take_effect_next_step(ws, registry):
    """改提示词、改名 → 下一步自己的 system 里就变了。"""
    script = {
        1: ("write_system_prompt", {"content": "# 世界观\n\n只信证据。\n"}),
        2: ("set_identity", {"name": "探针", "motto": "只信证据"}),
    }
    llm = _run(ws, registry, script)

    assert "只信证据" in llm.system(2)          # 第 2 步 system 已换新稿
    assert "只信证据" not in llm.system(1)      # 第 1 步还没有
    assert "名字：探针" in llm.system(3)        # 第 3 步名字已生效


# ---------------------------------------------------------------- E4
def test_new_function_is_in_tool_digest_next_step(ws, registry):
    """自造 + reload 之后，新工具必须出现在**下一步的 system 工具清单**里。"""
    code = ('def probe_double(n: int) -> str:\n'
            '    """把 n 翻倍。\n\n    Args:\n        n: 数字\n    """\n'
            '    return str(n * 2)\n')
    script = {
        1: ("write_function", {"name": "probe_double", "code": code}),
        2: ("reload_functions", {}),
    }
    llm = _run(ws, registry, script)

    assert "probe_double" in llm.system(3)      # 下一步的工具清单里有它
    assert "probe_double" not in llm.system(1)  # 造之前没有
    assert "probe_double" in registry.names()   # 真被注册了，不只是清单里印了一行


# ---------------------------------------------------------------- 反向：只写不生效才该红
def test_evolution_does_not_leak_into_the_tail_blocks(ws, registry):
    """自我改造的内容不许被抄进「此刻段」—— 那会让它每步重发一遍。

    自维护文件与 system 各有一份就够；此刻段只放每步不同的事实。
    """
    script = {1: ("write_plan", {"content": "# 计划\n\n- [ ] 独有的哨兵甲\n"})}
    llm = _run(ws, registry, script)
    tail = llm.calls[1][-1]["content"]          # 第 2 步的此刻段
    assert "独有哨兵甲" not in tail
