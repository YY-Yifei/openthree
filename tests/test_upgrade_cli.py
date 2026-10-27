"""`openthree -u` 的 CLI 层行为：参数校验、`--check`、升级会话里的人确认闸门。

`tests/test_upgrade_scan.py` 守的是判据与扫描；这里守的是**命令行这一层**：
从 `cli.main()` 进去，参数怎么校验、`--check` 真的不写盘、`-u` 真的进升级会话、
`:apply` 真的把待应用区的改动搬到位置。
"""
from __future__ import annotations

import builtins
import io
import json
from contextlib import redirect_stderr, redirect_stdout

import pytest

from openthree import cli
from openthree.upgrade import stage_dir
from openthree.workspace import Workspace


# ---------------------------------------------------------------- stub LLM
class _FinishLLM:
    """一律立刻 finish，并记下收到的 messages（与既有 CLI 测试同一条桩）。"""

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
    made = []

    def _factory(cfg):
        llm = _FinishLLM(cfg)
        made.append(llm)
        return llm

    monkeypatch.setattr("openthree.runtime.LLM", _factory)
    return made


def _fake_stdin(seq):
    it = iter(seq)

    def _input(prompt=""):
        try:
            return next(it)
        except StopIteration as exc:      # EOF：让 REPL 干净退出
            raise EOFError from exc

    return _input


def _run(argv, capsys):
    code = cli.main(argv)
    out, err = capsys.readouterr()
    return code, out, err


# ---------------------------------------------------------------- 参数校验
def test_upgrade_rejects_task_argument(tmp_path, capsys):
    """`-u` 自带升级任务 —— 再给任务参数是含糊的，直接拒。"""
    Workspace(tmp_path).init()
    code, _, err = _run(["--dir", str(tmp_path), "-u", "随便一个任务"], capsys)
    assert code == 2
    assert "不接受任务参数" in err


def test_check_requires_upgrade(tmp_path, capsys):
    """`--check` 不配 `-u` 是没有意义的，直接拒。"""
    Workspace(tmp_path).init()
    code, _, err = _run(["--dir", str(tmp_path), "--check"], capsys)
    assert code == 2
    assert "要与 -u 一起用" in err


def test_upgrade_rejects_init(tmp_path, capsys):
    """`--init` 只铺工作区，`-u` 要读工作区 —— 互斥。"""
    Workspace(tmp_path).init()
    code, _, err = _run(["--dir", str(tmp_path), "-u", "--init"], capsys)
    assert code == 2
    assert "互斥" in err


def test_upgrade_without_workspace_is_refused(tmp_path, capsys):
    """没有工作区时不许 `-u`。

    这条挡的是「升级」悄悄变成「初始化」：`Runtime.__init__` 会 `ws.init()`
    把工作区铺出来，于是用户以为在升级已有工作区，实际得到的是一个全新工作区。
    """
    code, _, err = _run(["--dir", str(tmp_path), "-u"], capsys)
    assert code == 2
    assert "还没有工作区" in err
    assert not (tmp_path / ".openthree").exists()   # 关键：什么都没被创建


# ---------------------------------------------------------------- --check
def test_check_reports_drift_and_exits_one(tmp_path, capsys):
    """有漂移 → 退出码 1，且报告里带上语义边界那句话。"""
    ws = Workspace(tmp_path).init()
    ws.system_prompt_file.write_text("我自己改过的提示词\n", encoding="utf-8")
    code, out, _ = _run(["--dir", str(tmp_path), "-u", "--check"], capsys)
    assert code == 1
    assert "升级检查" in out
    assert "pip install -U openthree" in out


def test_check_clean_workspace_exits_zero(tmp_path, capsys):
    """刚铺好的工作区没有漂移 → 退出码 0。"""
    Workspace(tmp_path).init()
    code, out, _ = _run(["--dir", str(tmp_path), "-u", "--check"], capsys)
    assert code == 0
    assert "没有发现漂移" in out


def test_check_writes_nothing(tmp_path, capsys):
    """`--check` 承诺「看一眼、不改动」—— 必须一个字节都不写。"""
    ws = Workspace(tmp_path).init()
    ws.system_prompt_file.write_text("改过的\n", encoding="utf-8")
    before = {p: p.stat().st_mtime_ns for p in ws.dir.rglob("*") if p.is_file()}

    _run(["--dir", str(tmp_path), "-u", "--check"], capsys)

    after = {p: p.stat().st_mtime_ns for p in ws.dir.rglob("*") if p.is_file()}
    assert before == after


# ---------------------------------------------------------------- 升级会话
def test_upgrade_enters_session_and_stages_playbook(tmp_path, monkeypatch, stub_llm, capsys):
    """`-u` 真的进升级会话，并把规程落成 `upgrade/PLAYBOOK.md`。"""
    Workspace(tmp_path).init()
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(builtins, "input", _fake_stdin([":q"]))

    code, out, _ = _run(["--dir", str(tmp_path), "-u"], capsys)

    assert code == 0
    assert "升级模式" in out
    playbook = tmp_path / ".openthree" / "upgrade" / "PLAYBOOK.md"
    assert playbook.exists()
    assert ":apply" in playbook.read_text(encoding="utf-8")
    # 首条任务真的交给了模型（不是空跑）
    assert stub_llm and stub_llm[0].calls


def test_upgrade_task_tells_model_not_to_write_directly(tmp_path, monkeypatch, stub_llm, capsys):
    """交给模型的升级任务必须写明「不要直接改既有文件」。"""
    Workspace(tmp_path).init()
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(builtins, "input", _fake_stdin([":q"]))

    _run(["--dir", str(tmp_path), "-u"], capsys)

    sent = "\n".join(str(m.get("content") or "") for m in stub_llm[0].calls[-1])
    assert "待应用区" in sent
    assert "不要" in sent and "write_system_prompt" in sent


def test_apply_lands_staged_change(tmp_path, monkeypatch, stub_llm, capsys):
    """升级会话里敲 `:apply` → 待应用区的改动真的落到目标文件上。"""
    ws = Workspace(tmp_path).init()
    staged = stage_dir(ws)
    (staged / "prompts").mkdir(parents=True, exist_ok=True)
    (staged / "prompts" / "system-prompts.md").write_text("新的提示词\n", encoding="utf-8")

    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(builtins, "input", _fake_stdin([":apply", ":q"]))

    code, out, _ = _run(["--dir", str(tmp_path), "-u"], capsys)

    assert code == 0
    assert "已处理 1 个文件" in out
    assert ws.system_prompt_file.read_text(encoding="utf-8") == "新的提示词\n"


def test_staged_command_lists_pending(tmp_path, monkeypatch, stub_llm, capsys):
    """`:staged` 列出待应用改动，人才能过目再决定 `:apply`。"""
    ws = Workspace(tmp_path).init()
    staged = stage_dir(ws)
    staged.mkdir(parents=True, exist_ok=True)
    (staged / "README.md").write_text("新的 README\n", encoding="utf-8")

    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(builtins, "input", _fake_stdin([":staged", ":q"]))

    code, out, _ = _run(["--dir", str(tmp_path), "-u"], capsys)

    assert code == 0
    assert "README.md" in out
    assert "待应用" in out


def test_undo_restores_after_apply(tmp_path, monkeypatch, stub_llm, capsys):
    """`:apply` 之后 `:undo` 能把工作区还原回升级前。"""
    ws = Workspace(tmp_path).init()
    original = ws.system_prompt_file.read_text(encoding="utf-8")
    staged = stage_dir(ws)
    (staged / "prompts").mkdir(parents=True, exist_ok=True)
    (staged / "prompts" / "system-prompts.md").write_text("升级后\n", encoding="utf-8")

    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(builtins, "input", _fake_stdin([":apply", ":undo", ":q"]))

    _run(["--dir", str(tmp_path), "-u"], capsys)

    assert ws.system_prompt_file.read_text(encoding="utf-8") == original


def test_dry_run_refuses_apply(tmp_path, monkeypatch, stub_llm, capsys):
    """`--dry-run` 下 `:apply` 被拒绝：只出方案，不落盘。"""
    ws = Workspace(tmp_path).init()
    original = ws.system_prompt_file.read_text(encoding="utf-8")
    staged = stage_dir(ws)
    (staged / "prompts").mkdir(parents=True, exist_ok=True)
    (staged / "prompts" / "system-prompts.md").write_text("不该生效\n", encoding="utf-8")

    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(builtins, "input", _fake_stdin([":apply", ":q"]))

    _run(["--dir", str(tmp_path), "-u", "--dry-run"], capsys)

    assert ws.system_prompt_file.read_text(encoding="utf-8") == original


def test_upgrade_commands_rejected_outside_upgrade_mode(tmp_path, monkeypatch, stub_llm, capsys):
    """普通交互会话里 `:apply` / `:undo` 不可用 —— 它们只属于升级模式。"""
    Workspace(tmp_path).init()
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(builtins, "input", _fake_stdin([":apply", ":q"]))

    code, out, _ = _run(["--dir", str(tmp_path), "-i"], capsys)

    assert code == 0
    assert "只在升级模式" in out


def test_upgrade_requires_tty(tmp_path, monkeypatch, capsys):
    """stdin 不是终端时拒进升级会话：无人应答的 REPL 毫无意义。"""
    Workspace(tmp_path).init()
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)

    code, _, err = _run(["--dir", str(tmp_path), "-u"], capsys)

    assert code == 2
    assert "-u --check" in err
