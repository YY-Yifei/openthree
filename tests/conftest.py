"""pytest 共享夹具：不联网、不依赖真实模型。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openthree.agent import Agent  # noqa: E402
from openthree.builtins import installed_builtins  # noqa: E402
from openthree.decide import ENV_API_KEY, ENV_BASE_URL, ENV_MODEL  # noqa: E402
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


_JEV_KEYS = (ENV_BASE_URL, ENV_API_KEY, ENV_MODEL)


@pytest.fixture(autouse=True)
def _no_jev_env(monkeypatch):
    """默认在「JEV 未配置」的环境里跑：门控的默认路径就是「不注册」。

    不这么做，测试结果会随开发机上有没有 `JEV_*` 而变 —— 那是最难查的一类
    flaky。要测「配好就启用」的用例自己 monkeypatch.setenv 即可。
    变量名取自 `decide` 的常量，不在这里另抄一份字面量：改名时这里会跟着变，
    而不是静默变成空操作、把 flaky 又放回来。
    """
    for key in _JEV_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _reset_decide_registration(monkeypatch):
    """注册期快照要复位：`installed_builtins()` 冻结过就不再重判，测试之间会互相污染。

    一个测试在「配好了」的环境里走过注册期，下一个测试拿到的 `installed_builtins()`
    里仍然有 `decide` —— 那些断言「没配就不注册」的用例会随执行顺序变红或变绿。
    """
    from openthree import decide as _decide

    # 不给 `raising=False`：属性一旦改名，这里要**当场炸**，而不是静默新建一个
    # 同名属性、把复位变成空操作 —— 那正是本夹具要防的那类污染。
    monkeypatch.setattr(_decide, "_registered", None)
    monkeypatch.setattr(_decide, "_snapshot", None)


@pytest.fixture
def jev_free_env() -> dict:
    """给**子进程**用的环境：同样不含任何 `JEV_*`。

    上面那个夹具只清得动本进程的 `os.environ`；子进程继承的是真实环境，于是
    「没配就不启用」这类子进程断言，在配了 `JEV_*` 的开发机上会翻车。要用子进程
    测门控就传 `env=jev_free_env`（或它的副本）。
    """
    env = dict(os.environ)
    for key in _JEV_KEYS:
        env.pop(key, None)
    return env


@pytest.fixture
def registry(ws: Workspace) -> Registry:
    reg = Registry(ws)
    for name, fn in installed_builtins().items():
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
