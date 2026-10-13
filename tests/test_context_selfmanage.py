"""上下文自管：策略解析、每轮重读、用量读数、折叠语义、prompt 注入。

这里的核心主张是：上下文管理从「内核单方面」变成「模型的一只手」——
模型能用 context_budget 改策略、用 context_usage 看读数、用 compact_context 折/丢；
内核的自动折叠降级为可被模型关闭的「安全网」，环境变量是它绕不过的硬闸。
"""
from __future__ import annotations

from openthree.agent import resolve_context_policy
from openthree.builtins import BUILTIN_FUNCTIONS, context_budget, context_usage
from openthree.prompt import build_runtime_block
from openthree.workspace import Workspace

from conftest import step


# ---------------------------------------------------------------- 策略解析
def test_policy_derives_from_window_by_default():
    assert resolve_context_policy(None, None, window_tokens=128_000,
                                  env_value="") == (128_000, True)


def test_policy_explicit_beats_file_budget():
    assert resolve_context_policy(500, None, explicit=2000, env_value="") == (2000, True)


def test_policy_zero_budget_disables_autofold():
    budget, allow = resolve_context_policy(0, None, env_value="")
    assert budget == 0 and allow is False


def test_policy_autofold_flag_honored():
    assert resolve_context_policy(1000, False, env_value="") == (1000, False)
    assert resolve_context_policy(1000, True, env_value="") == (1000, True)


def test_policy_env_is_hard_gate():
    """环境变量 = 真·硬闸：即便策略说开，env=0 也关。"""
    _, allow = resolve_context_policy(1000, True, env_value="0")
    assert allow is False
    _, allow = resolve_context_policy(1000, True, env_value="off")
    assert allow is False
    # 非空且非关值（如 "1"）不锁
    assert resolve_context_policy(1000, None, env_value="1") == (1000, True)


# ---------------------------------------------------------------- 策略文件
def test_workspace_seeds_context_policy(ws: Workspace):
    assert ws.context_policy_file.exists()


def test_agent_reads_policy_and_tick_reacts(make_agent):
    ag = make_agent()
    # 种子是「null 预算 + 自动折叠开」→ 按窗口推导（默认 1M 窗口 → 100 万字符）
    assert ag.context_budget_chars == 1_000_000
    assert ag._auto_fold_enabled is True

    # 模型把预算写进策略文件 → tick 重读即生效（改一下下一轮生效）
    ag.ws.context_policy_file.write_text(
        '{"budget_chars": 12345, "auto_fold": false}', encoding="utf-8")
    ag.tick()
    assert ag.context_budget_chars == 12345
    assert ag._auto_fold_enabled is False


def test_set_context_budget_writes_file_and_applies(make_agent):
    ag = make_agent()
    ok, msg = ag.set_context_budget(budget_chars=2000)
    assert ok and "2000" in msg
    assert ag.context_budget_chars == 2000
    assert "2000" in ag.ws.context_policy_file.read_text(encoding="utf-8")


def test_set_context_budget_rejects_empty(make_agent):
    ag = make_agent()
    ok, msg = ag.set_context_budget()
    assert not ok and "至少" in msg


def test_set_context_budget_honestly_refuses_locked_budget(make_agent):
    """显式 --context-budget 锁定预算时，诚实拒绝（与 set_limits 同一契约）。"""
    ag = make_agent(context_budget_chars=5000)
    assert ag.context_budget_chars == 5000
    ok, msg = ag.set_context_budget(budget_chars=999999)
    assert not ok and "锁定" in msg
    assert ag.context_budget_chars == 5000          # 没被改
    # 自动折叠仍可单独调（锁的是预算，不是开关）
    ok2, _ = ag.set_context_budget(auto_fold=False)
    assert ok2 and ag._auto_fold_enabled is False


def test_env_lock_freezes_autofold(make_agent, monkeypatch):
    monkeypatch.setenv("OPENTHREE_CONTEXT_AUTOFOLD", "0")
    ag = make_agent()
    assert ag._auto_fold_enabled is False
    # 模型想把自动折叠打开，也绕不过环境硬闸
    ag.set_context_budget(auto_fold=True)
    assert ag._auto_fold_enabled is False
    assert ag.history_stats()["auto_fold_env_locked"] is True


# ---------------------------------------------------------------- 用量读数
def test_history_stats_reports_usage_and_fold_range(make_agent):
    ag = make_agent()
    for i in range(3):
        step(ag, i)
    s = ag.history_stats()
    assert s["used_chars"] > 0
    assert s["steps"] == 3
    # 最后一步不许折，可折范围只到 2
    assert s["fold_range"] == "1-2"
    assert s["auto_fold"] is True


def test_auto_compact_respects_policy_off(make_agent):
    """策略关掉自动折叠后，即便爆仓内核也不再兜底。"""
    ag = make_agent()
    ag.set_context_budget(budget_chars=100, auto_fold=False)
    for i in range(10):
        step(ag, i, tool_chars=500)
    n = len(ag.history)
    ag._auto_compact()
    assert len(ag.history) == n          # 没动


def test_auto_compact_still_works_when_on(make_agent):
    ag = make_agent()
    ag.set_context_budget(budget_chars=900)   # 预算很小，自动折叠仍开
    for i in range(12):
        step(ag, i, tool_chars=300)
    before = len(ag.history)
    ag._auto_compact()
    assert len(ag.history) < before


# ---------------------------------------------------------------- 内置工具
def test_context_tools_registered():
    assert "context_usage" in BUILTIN_FUNCTIONS
    assert "context_budget" in BUILTIN_FUNCTIONS


def test_context_usage_and_budget_tools(make_agent):
    ag = make_agent()
    for i in range(2):
        step(ag, i)
    out = context_usage()
    assert "已用" in out and "可折叠" in out
    msg = context_budget(budget_chars=3000)
    assert msg.startswith("[ok]")
    assert ag.context_budget_chars == 3000
    assert "3000" in context_usage()


def test_context_budget_tool_noop_without_args(make_agent):
    make_agent()
    assert "错误" in context_budget()


# ---------------------------------------------------------------- prompt 注入
def test_prompt_injects_usage_line(ws, registry):
    block = build_runtime_block(
        ws, registry, model="m", iteration=1, max_iterations=200,
        context_stats={"used_chars": 123, "budget_chars": 1000, "ratio": 0.123,
                       "fold_range": "1-2", "auto_fold": True, "compactions": 2})
    assert "已用 123/1000" in block
    assert "可折叠 step 范围 1-2" in block


def test_prompt_backcompat_without_stats(ws, registry):
    block = build_runtime_block(ws, registry, model="m", iteration=1,
                                max_iterations=200, fold_range="1-2")
    assert "可折叠 step 范围 1-2" in block
