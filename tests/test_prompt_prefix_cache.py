"""提示词前缀：内核不许在 system 里放「每步都会变」的值。

为什么这条值得单独一个文件：服务端的前缀缓存从**第一个不同的字节**开始断开，
而一次请求的序列化顺序是 system → tools → messages。于是 system 里只要有
一个每秒都在变的值（时间、步数、上次行动），它之后的工具 schema（一万多
token）与全部历史就每步都要重算 —— 实测命中率从 ~92% 跌到 ~6%。

四条被保护的不变量：
  I1  同一份状态连续组装两次，system 逐字节相同。
  I2  每步刷新的字段只出现在**最后一条**消息里，不在 system 里。
  I3  自维护段与此刻段都不写回 history —— 下一步它们会被真实的一步取代。
  I4  模型改计划不会改变 system 的字节（否则写一次计划就重算一次工具 schema）。
"""
from __future__ import annotations

import io

from openthree.prompt import (build_context_block, build_messages,
                              build_runtime_block, build_situation_block,
                              build_static_block)

from conftest import *  # noqa: F401,F403  复用 ws / registry 夹具

HISTORY = [{"role": "user", "content": "帮我修个 typo"}]

# 这些是「此刻」的字段行标记 —— 只在最后一条消息里出现。
# 用整行标记而不是裸词，是因为工具清单里也有「获取当前时间」这种正常描述。
PER_STEP_FIELDS = ("- 当前时间：", "- 这是第", "# 你上次的行动")


def _frozen_datetime():
    """一个 `datetime` 替身：`now()` 恒返回同一时刻。

    处境段里的时间戳每到一步都在变，于是「两次组装必须相等」这类断言天然
    带一条秒级竞态。把时钟钉住，断言才只检验结构、不检验运气。
    """
    import datetime as _dt

    class _Frozen:
        @staticmethod
        def now():
            return _dt.datetime(2026, 1, 1, 12, 0, 0)

        def __getattr__(self, name):
            return getattr(_dt.datetime, name)

    return _Frozen


def _stats_kw(iteration: int) -> dict:
    """这一步的读数 —— 发送路径与兼容壳必须拿同一份，否则比的不是同一件事。"""
    return dict(
        fold_range="1-2",
        context_stats={"used_chars": iteration * 100, "budget_chars": 100000,
                       "ratio": iteration / 1000.0, "fold_range": "1-2",
                       "auto_fold": True, "compactions": 0},
        limits_stats={"max_iterations": 200, "max_rounds": 3, "max_tokens": 8192,
                      "tool_result_limit": 4000, "temperature": 0.3, "locked": False},
        round_stats={"index": 1, "max": 3, "next": True},
    )


def _assemble(ws, registry, *, iteration: int, last_action: str):
    return build_messages(
        ws, registry, HISTORY, model="m", iteration=iteration, max_iterations=200,
        last_action=last_action, **_stats_kw(iteration),
    )


# ---------------------------------------------------------------- I1：逐字节稳定
def test_system_is_byte_identical_across_steps(ws, registry):
    """内核往 system 里塞任何每步变的值，这条就会红。"""
    a = _assemble(ws, registry, iteration=1, last_action="读了 a.py")
    b = _assemble(ws, registry, iteration=2, last_action="读了 b.py")
    assert a[0]["content"] == b[0]["content"]


def test_static_block_is_byte_identical_on_repeat(ws, registry):
    assert (build_static_block(ws, registry, model="m")
            == build_static_block(ws, registry, model="m"))


# ---------------------------------------------------------------- I2：动态只在末尾
def test_per_step_fields_live_in_the_tail_message(ws, registry):
    msgs = _assemble(ws, registry, iteration=7, last_action="读了 a.py")
    tail = msgs[-1]["content"]
    system = msgs[0]["content"]

    assert tail.startswith("# 此刻的处境")
    assert "当前时间" in tail and "本轮第 7 步" in tail and "读了 a.py" in tail
    for field in PER_STEP_FIELDS:
        assert field not in system, f"{field} 不该出现在 system 里（会让缓存每步断开）"


def test_situation_block_changes_with_step(ws, registry):
    one = build_situation_block(iteration=1, max_iterations=200, last_action="a")
    two = build_situation_block(iteration=2, max_iterations=200, last_action="b")
    assert one != two
    assert "本轮第 1 步" in one and "本轮第 2 步" in two


# ---------------------------------------------------------------- I3：不写回 history
def test_tail_messages_are_not_written_back_to_history(ws, registry):
    msgs = _assemble(ws, registry, iteration=3, last_action="x")
    assert len(msgs) == len(HISTORY) + 3          # system + history + 自维护段 + 处境
    assert msgs[1:1 + len(HISTORY)] == HISTORY    # 原样不动
    assert msgs[-2]["content"].startswith("# 你的计划")
    assert all(m["content"] != msgs[-1]["content"] for m in HISTORY)


def test_system_still_carries_the_stable_parts(ws, registry):
    """拆成三段之后，内核常量（工作目录 / 工具清单）仍在 system 里。

    任务正文**不在** system 里：它每换一条消息就变（会让工具 schema 的缓存
    前缀每轮失效），而且它是用户说的话，位置该是 history 末尾那条 user 消息。
    """
    system = build_static_block(ws, registry, model="m")
    assert "# 你的工具（二）" in system
    assert "# 当前任务（三）" not in system


def test_current_input_is_a_user_message_after_history(ws, registry):
    """本轮输入必须是 history 里的一条 user 消息，紧接在上一轮之后。

    位置就是语义：它若埋在 system 深处，而消息层尾部停在上一轮的一问一答，
    模型读到的「最后一句人话」就变成了上一轮 —— 于是像在处理上一条消息。
    """
    prev = [{"role": "user", "content": "上一轮的问题"},
            {"role": "assistant", "content": "上一轮的回答"}]
    msgs = build_messages(ws, registry, prev, model="m", iteration=1,
                          max_iterations=200, **_stats_kw(1))
    assert msgs[1:3] == prev                       # 上一轮原样保留在前
    # build_messages 只管拼接既有 history；本轮输入由 Agent 注入到 history 末尾，
    # 端到端的位置不变量见 tests/test_current_input_position.py。
    assert [m["role"] for m in msgs[:3]] == ["system", "user", "assistant"]


def test_self_maintained_parts_live_after_history(ws, registry):
    """计划 / 记忆 / 演化史在 system 之外 —— 它们是模型自己会改的文件。"""
    system = build_static_block(ws, registry, model="m")
    context = build_context_block(ws)
    assert "# 你的计划" not in system
    assert "# 你的长期记忆" not in system
    assert "# 你的计划" in context and "# 你的长期记忆" in context


# ---------------------------------------------------------------- I4：改计划不动 system
def test_rewriting_plan_does_not_change_system(ws, registry):
    """写一次计划就重算一次工具 schema 是这轮要消灭的浪费。

    `write_plan` 是模型在长任务里最常做的事之一；只要计划还在 system 里，
    每写一次就要把一万多 token 的工具 schema 重新算一遍。
    """
    before = build_static_block(ws, registry, model="m")
    before_ctx = build_context_block(ws)

    plan_path = ws.plan_file
    original = io.open(plan_path, encoding="utf-8", newline="").read() if plan_path.exists() else None
    try:
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        with io.open(plan_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("# 另一份计划\n\n- [ ] 完全不同的事\n")

        assert build_static_block(ws, registry, model="m") == before
        assert build_context_block(ws) != before_ctx
    finally:
        if original is None:
            plan_path.unlink(missing_ok=True)
        else:
            with io.open(plan_path, "w", encoding="utf-8", newline="") as fh:
                fh.write(original)


# ---------------------------------------------------------------- 兼容壳不许漂移
def test_runtime_block_equals_what_is_actually_sent(ws, registry, monkeypatch):
    """`build_runtime_block` 不是第二条组装路径，而是发送内容的**同一次拼装**。

    它是给「想一眼看全处境」的调用方用的（诊断、自省、人肉排查），生产路径
    只走 `build_messages`。既然是兼容壳，就必须始终等于真正发出去的那三段，
    否则看诊断的人和看请求的人会看到两份不同的处境 —— 而这份壳没有任何生产
    调用方，漂移了也不会有别的东西来撞它。

    时钟被钉住：三条路径各自取一次 `datetime.now()`，跨过秒边界就会让这份
    「相等」断言偶发失败 —— 那是测试的抖动，不是兼容壳漂移。
    """
    monkeypatch.setattr("openthree.prompt.datetime", _frozen_datetime())
    kw = _stats_kw(1)
    msgs = build_messages(ws, registry, HISTORY, model="m", iteration=1,
                          max_iterations=200, last_action="", **kw)
    static = build_static_block(ws, registry, model="m")
    context = build_context_block(ws)
    situation = build_situation_block(iteration=1, max_iterations=200,
                                      last_action="", **kw)

    # 发送路径：system 以内核常量段结尾，随后是自维护段与此刻段
    assert msgs[0]["content"].endswith(static)
    assert msgs[-2]["content"] == context
    assert msgs[-1]["content"] == situation

    # 兼容壳：同一组三段按同样顺序拼回
    assert build_runtime_block(ws, registry, model="m", iteration=1,
                               max_iterations=200, **kw) == \
        f"{static}\n\n{context}\n\n{situation}"
