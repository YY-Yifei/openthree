"""pytest 共享夹具：不联网、不依赖真实模型。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openthree.agent import Agent  # noqa: E402
from openthree.builtins import BUILTIN_FUNCTIONS  # noqa: E402
from openthree.kernel import KERNEL  # noqa: E402
from openthree.llm import LLM, LLMConfig  # noqa: E402
from openthree.prompt import build_messages, build_static_block  # noqa: E402
from openthree.recorder import Recorder  # noqa: E402
from openthree.registry import Registry  # noqa: E402
from openthree.workspace import Workspace  # noqa: E402


def sent_block(ws: Workspace, reg: Registry, *, model: str = "m",
               iteration: int = 1, max_iterations: int = 200,
               history=None, **kw) -> str:
    """按**真正发请求的路径**组装一遍，把内核组装的那三段拼成一份文本返回。

    「处境里到底写了什么」这类断言，入口必须是 `build_messages` —— 那才是
    `Agent` 每一步交给 LLM 的东西。若测试改走另一条拼装函数（哪怕它现在
    与发送路径等价），那条拼装函数被单独改坏时测试依然会全绿，断言就失去了
    守卫对象。

    拼回去时只丢 `messages[0]` 里模型自己的 system 提示词（由
    `write_system_prompt` 维护，内容随任务变），保留紧随其后的内核常量段；
    自维护段与此刻段则原样取自 `build_messages` 的返回。
    """
    msgs = build_messages(ws, reg, list(history or []), model=model,
                          iteration=iteration, max_iterations=max_iterations, **kw)
    static = build_static_block(ws, reg, model=model)
    system = msgs[0]["content"]
    start = system.rfind(static)          # 内核常量段固定在 system 末尾
    kernel_part = system[start:] if start >= 0 else system
    return "\n\n".join([kernel_part] + [m["content"] for m in msgs[1:]])


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    return Workspace(tmp_path).init()


@pytest.fixture
def registry(ws: Workspace) -> Registry:
    reg = Registry(ws)
    for name, fn in BUILTIN_FUNCTIONS.items():
        reg.register_builtin(name, fn)
    reg.freeze_builtins()
    reg.reload()
    return reg


@pytest.fixture
def make_agent(ws: Workspace, registry: Registry):
    """构造一个 Agent，并把各部件挂进 KERNEL（内置工具的自我演化依赖它）。"""
    def _make(**kwargs) -> Agent:
        rec = Recorder(ws, user_input="test", model="stub")
        ag = Agent(ws, registry, LLM(LLMConfig(api_key="k", base_url="u")), rec, **kwargs)
        KERNEL.workspace = ws
        KERNEL.registry = registry
        KERNEL.recorder = rec
        KERNEL.agent = ag
        return ag
    return _make


def step(ag: Agent, i: int, tool_chars: int = 10) -> None:
    """往 history 追加一个完整的 step（assistant + tool 结果）。"""
    ag.history.append({
        "role": "assistant", "content": f"think {i}",
        "tool_calls": [{"id": f"c{i}", "type": "function",
                        "function": {"name": "x", "arguments": "{}"}}],
    })
    ag.history.append({
        "role": "tool", "tool_call_id": f"c{i}", "name": "x",
        "content": "T" * tool_chars,
    })
