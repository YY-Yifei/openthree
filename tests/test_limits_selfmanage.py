"""运行边界自管：limits.json 策略、每轮重读、读数、写入、环境硬闸、显式锁定。

核心主张：**运行边界**（步数 / 生成长度 / 工具结果宽度 / 温度）从「内核单方面」
变成「模型的一只手」——模型能用 `set_limits` 改、用 `limits_usage` 看；
内核默认只是起点，人类显式参数与 `OPENTHREE_LIMITS_LOCK` 才是它绕不过的闸。
"""
from __future__ import annotations

import json

from openthree.builtins import BUILTIN_FUNCTIONS, limits_usage, set_limits
from openthree.prompt import build_runtime_block
from openthree.workspace import Workspace

from conftest import step


def _write(ws, **kw):
    ws.limits_file.write_text(json.dumps(kw), encoding="utf-8")


# ---------------------------------------------------------------- 种子 & 默认
def test_workspace_seeds_limits(ws: Workspace):
    assert ws.limits_file.exists()
    data = json.loads(ws.limits_file.read_text(encoding="utf-8"))
    assert set(data) == {"max_iterations", "max_tokens",
                         "tool_result_limit", "temperature"}
    assert all(v is None for v in data.values())        # 种子 = 全交给内核默认


def test_agent_defaults_when_file_null(make_agent):
    ag = make_agent()
    assert ag.max_iterations == 200
    assert ag.tool_result_limit == 4000
    assert ag.max_tokens == 8192                        # 来自 LLMConfig 默认
    assert ag.temperature == 0.3
    assert ag.limits_stats()["source"]["max_iterations"] == "内核默认"


# ---------------------------------------------------------------- 每轮重读
def test_tick_reacts_to_limits_file(make_agent):
    ag = make_agent()
    _write(ag.ws, max_iterations=50, max_tokens=1234,
           tool_result_limit=99, temperature=0.7)
    ag.tick()                                           # 改一下下一轮生效
    assert ag.max_iterations == 50
    assert ag.tool_result_limit == 99
    assert ag.max_tokens == 1234
    assert ag.temperature == 0.7
    # 生成参数被推到 LLM 客户端 —— 真正会被发出去
    assert ag.llm.config.max_tokens == 1234
    assert ag.llm.config.temperature == 0.7
    assert ag.limits_stats()["source"]["max_tokens"] == "模型自管"


def test_tool_result_limit_from_file_affects_spill(make_agent):
    ag = make_agent()
    _write(ag.ws, tool_result_limit=50)
    ag.tick()
    out = ag._spill("c", "x", "Z" * 500)
    assert out.startswith("Z" * 50) and out[50] != "Z"   # 正文被切到 50
    assert "500" in out and len(out) < 300               # 只留预览 + 提示


# ---------------------------------------------------------------- 显式锁定
def test_explicit_param_locks_against_file(make_agent):
    ag = make_agent(max_iterations=30)
    _write(ag.ws, max_iterations=999)
    ag.tick()
    assert ag.max_iterations == 30                      # 策略文件改不动显式锁
    assert ag.limits_stats()["source"]["max_iterations"] == "显式（锁定）"


def test_explicit_param_locks_against_set_limits(make_agent):
    ag = make_agent(max_tokens=4096)
    ok, msg = ag.set_limits(max_tokens=99999)
    assert not ok and "锁定" in msg
    assert ag.max_tokens == 4096


# ---------------------------------------------------------------- 写入
def test_set_limits_writes_and_applies(make_agent):
    ag = make_agent()
    ok, msg = ag.set_limits(tool_result_limit=123, temperature=0.1)
    assert ok
    assert ag.tool_result_limit == 123 and ag.temperature == 0.1
    data = json.loads(ag.ws.limits_file.read_text(encoding="utf-8"))
    assert data["tool_result_limit"] == 123 and data["temperature"] == 0.1
    assert ag.llm.config.temperature == 0.1


def test_set_limits_rejects_empty(make_agent):
    ag = make_agent()
    ok, msg = ag.set_limits()
    assert not ok and "至少" in msg


def test_set_limits_rejects_invalid(make_agent):
    ag = make_agent()
    assert not ag.set_limits(max_iterations=0)[0]
    assert not ag.set_limits(temperature=5)[0]


# ---------------------------------------------------------------- 环境硬闸
def test_env_lock_freezes_limits(make_agent, monkeypatch):
    monkeypatch.setenv("OPENTHREE_LIMITS_LOCK", "1")
    ag = make_agent()
    _write(ag.ws, max_iterations=5)
    ag.tick()
    assert ag.max_iterations == 200                     # 文件被冻结
    assert ag.limits_stats()["locked"] is True
    ok, msg = ag.set_limits(max_iterations=10)
    assert not ok and "冻结" in msg


# ---------------------------------------------------------------- 内置工具
def test_limits_tools_registered():
    assert "limits_usage" in BUILTIN_FUNCTIONS
    assert "set_limits" in BUILTIN_FUNCTIONS


def test_limits_usage_and_set_limits_tools(make_agent):
    ag = make_agent()
    out = limits_usage()
    assert "迭代上限" in out and "max_tokens" in out
    msg = set_limits(max_iterations=77)
    assert msg.startswith("[ok]") and ag.max_iterations == 77
    assert "77" in limits_usage()


# ---------------------------------------------------------------- prompt 注入
def test_prompt_injects_limits_line(ws, registry):
    block = build_runtime_block(
        ws, registry, model="m", iteration=1, max_iterations=200,
        limits_stats={"max_iterations": 200, "max_tokens": 8192,
                      "tool_result_limit": 4000, "temperature": 0.3,
                      "locked": False})
    assert "步数 ≤ 200" in block
    assert "生成 ≤ 8192 token" in block


def test_prompt_backcompat_without_limits(ws, registry):
    block = build_runtime_block(ws, registry, model="m",
                                iteration=1, max_iterations=200)
    assert "运行边界" in block and "步数上限 200" in block
