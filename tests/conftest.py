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
from openthree.recorder import Recorder  # noqa: E402
from openthree.registry import Registry  # noqa: E402
from openthree.workspace import Workspace  # noqa: E402


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
