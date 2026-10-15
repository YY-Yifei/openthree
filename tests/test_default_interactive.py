"""`-i` 是默认模式 —— 不带参数在终端里直接进交互模式。

设计（2026-10-15）：

* **默认进交互**：不带任务参数、不带图片、不开 `--auto`、不 `--resume`，
  且 `stdin` 连着终端（`_stdin_is_tty()`）→ `openthree` 等价于 `openthree -i`。
* **非终端退回单次**：管道 / 重定向 / CI / pytest（`isatty()` 为假）下，REPL 会
  立刻 EOF、什么也不做就退出，比旧的单次语义更糟 —— 所以退回旧路径。
* **显式 `-i` 不受此限**：任何时候写 `-i` 都进交互（旧的行为一点没变）。
* **`--no-interactive`**：不带参数时强制退回单次（沿用磁盘上的当前任务跑一次）。
* **`--resume N`（无任务）不劫持**：它是「单次跑 + 读回档案」的文档化路径，
  要交互式续读请写 `-i --resume N`。
* **有任务 / 有图片 / 有 `--auto` 一律是单次路径**，不进交互。

本文件用 stub LLM + 临时工作区覆盖这些分支，不联网、不依赖真实模型。
"""
from __future__ import annotations

import builtins
import io
import json
from contextlib import redirect_stdout

import pytest

from openthree import cli
from openthree.runtime import Runtime

TASK_MARK = "# 当前任务（三）"


# ---------------------------------------------------------------- stub LLM
class _FinishLLM:
    """每次都直接 finish，并记下模型收到的 messages。"""

    def __init__(self, cfg, answer="ok"):
        self.config = cfg
        self.answer = answer
        self.calls = []

    def chat(self, messages, tools=None, tool_choice="auto", **kwargs):
        self.calls.append(messages)
        msg = {"role": "assistant", "content": "",
               "tool_calls": [{"id": "c", "type": "function",
                               "function": {"name": "finish",
                                            "arguments": json.dumps({"answer": self.answer})}}]}
        return {"message": msg, "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                "elapsed_ms": 0.0, "raw_request": {"messages": []},
                "raw_response": {"choices": [{"message": msg}]}, "error": None}


@pytest.fixture
def stub_llm(monkeypatch):
    """让 Runtime 造出来的 LLM 全部走桩（`cli.main` 内部会自己 new 一个 Runtime）。"""
    made = []

    def _factory(cfg):
        llm = _FinishLLM(cfg)
        made.append(llm)
        return llm

    monkeypatch.setattr("openthree.runtime.LLM", _factory)
    return made


def _rt(tmp_path, **kw) -> Runtime:
    """离线 Runtime：直连模型接口，用来预置磁盘状态。"""
    rt = Runtime(tmp_path, quiet=True, heartbeat=0, **kw)
    rt.config.api_key = rt.config.base_url = "stub"
    rt.config.model = "stub-model"
    rt.llm = _FinishLLM(rt.config)
    return rt


def _fake_stdin(seq):
    it = iter(seq)

    def _input(prompt=""):
        try:
            return next(it)
        except StopIteration as exc:      # 模拟 EOF，让 -i 干净退出
            raise EOFError from exc
    return _input


def _task_in_system(call) -> str:
    for m in call:
        if m.get("role") == "system" and isinstance(m.get("content"), str):
            if TASK_MARK in m["content"]:
                return m["content"].split(TASK_MARK, 1)[1].strip()
    return ""


# ------------------------------------------------ 1) 默认进交互（终端，无参数）
def test_default_no_args_enters_interactive_when_tty(tmp_path, monkeypatch, stub_llm):
    """stdin 是终端时，`openthree`（无参数）直接进交互模式。"""
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(builtins, "input", _fake_stdin([":q"]))
    out = io.StringIO()
    with redirect_stdout(out):
        code = cli.main(["--dir", str(tmp_path), "-q"])
    assert code == 0
    assert "交互模式。" in out.getvalue()          # 进的是 _interactive


def test_default_no_args_runs_task_inside_interactive(tmp_path, monkeypatch, stub_llm):
    """默认交互模式下敲的那句话会真的跑一次任务（走会话）。"""
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(builtins, "input",
                        _fake_stdin(["交互里的第一句", ":q"]))
    with redirect_stdout(io.StringIO()):
        code = cli.main(["--dir", str(tmp_path), "-q"])
    assert code == 0
    assert len(stub_llm) == 1
    assert _task_in_system(stub_llm[0].calls[-1]) == "交互里的第一句"


# ------------------------------------------------ 2) 非终端 / 显式退回 = 单次
def test_non_tty_falls_back_to_single_run(tmp_path, monkeypatch, stub_llm):
    """stdin 不是终端（管道 / CI）：退回单次，沿用磁盘上的当前任务。"""
    _rt(tmp_path).run("磁盘上的旧任务")
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
    monkeypatch.setattr(builtins, "input", _no_input_allowed)

    out = io.StringIO()
    with redirect_stdout(out):
        code = cli.main(["--dir", str(tmp_path), "-q"])
    assert code == 0
    assert "交互模式。" not in out.getvalue()
    assert _task_in_system(stub_llm[-1].calls[-1]) == "磁盘上的旧任务"


def test_no_interactive_flag_forces_single_run_even_on_tty(tmp_path, monkeypatch, stub_llm):
    """`--no-interactive`：即使在终端里也不进交互，退回单次沿用磁盘任务。"""
    _rt(tmp_path).run("磁盘上的旧任务")
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(builtins, "input", _no_input_allowed)

    out = io.StringIO()
    with redirect_stdout(out):
        code = cli.main(["--dir", str(tmp_path), "-q", "--no-interactive"])
    assert code == 0
    assert "交互模式。" not in out.getvalue()
    assert _task_in_system(stub_llm[-1].calls[-1]) == "磁盘上的旧任务"


def test_explicit_i_still_works_without_tty(tmp_path, monkeypatch, stub_llm):
    """反证：显式 `-i` 不受「非终端」限制 —— 旧行为一点没变。"""
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
    monkeypatch.setattr(builtins, "input", _fake_stdin([":q"]))
    out = io.StringIO()
    with redirect_stdout(out):
        code = cli.main(["--dir", str(tmp_path), "-q", "-i"])
    assert code == 0
    assert "交互模式。" in out.getvalue()


# ------------------------------------------------ 3) 有任务 / 有图 / --auto 仍是单次
def test_task_arg_never_enters_interactive(tmp_path, monkeypatch, stub_llm):
    """给了任务参数就是单次任务，绝不进交互（进了会先等 input）。"""
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(builtins, "input", _no_input_allowed)
    out = io.StringIO()
    with redirect_stdout(out):
        code = cli.main(["--dir", str(tmp_path), "-q", "单次跑的任务"])
    assert code == 0
    assert "交互模式。" not in out.getvalue()
    assert _task_in_system(stub_llm[-1].calls[-1]) == "单次跑的任务"


def test_auto_without_task_never_enters_interactive(tmp_path, monkeypatch, stub_llm):
    """`--auto N` 不给任务时仍是单次接力，不进交互。"""
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(builtins, "input", _no_input_allowed)
    out = io.StringIO()
    with redirect_stdout(out):
        code = cli.main(["--dir", str(tmp_path), "-q", "--auto", "1", "接力任务"])
    assert code == 0
    assert "交互模式。" not in out.getvalue()
    assert _task_in_system(stub_llm[-1].calls[-1]) == "接力任务"


def test_resume_without_task_stays_single_run(tmp_path, monkeypatch, stub_llm):
    """`--resume N`（无任务）保持文档化语义：单次跑 + 读回档案，不被劫持成交互。"""
    _rt(tmp_path).run("磁盘上的旧任务")
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(builtins, "input", _no_input_allowed)
    out = io.StringIO()
    with redirect_stdout(out):
        code = cli.main(["--dir", str(tmp_path), "-q", "--resume", "2"])
    assert code == 0
    assert "交互模式。" not in out.getvalue()
    assert "[会话] 已从对话档案续读最近 2 轮" in out.getvalue()


# ------------------------------------------------ 4) 无任务且没进交互 → 仍旧报用法
def test_no_task_no_tty_no_disk_task_prints_usage(tmp_path, monkeypatch, stub_llm):
    """既没任务、也没进交互（非终端 + 磁盘无任务）→ 打印用法、退出码非 0。"""
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
    out = io.StringIO()
    with redirect_stdout(out):
        code = cli.main(["--dir", str(tmp_path), "-q"])
    assert code == 1
    assert "没有任务" in out.getvalue()
    # Runtime 构造时总会 new 一个 LLM（这里被桩替换），但这条路径不该真的调它
    assert all(not l.calls for l in stub_llm), "不该调模型"


def _no_input_allowed(prompt=""):  # noqa: ARG001
    raise AssertionError("这一步不该进交互模式（不该有人来敲 input）")
