"""`-i` / `--auto` / CLI 里四条容易被改坏的不变量。

本文件用 stub LLM + 临时工作区覆盖它们，不联网、不依赖真实模型。

* **纯图片输入不落到磁盘任务上**：`-i` 里 `:img X` + 空回车（只有图、没有文字），
  以及 `openthree --image a.png`（不给任务）—— 二者都必须把「本轮任务」当成
  「看这张图」，而不是 `None`。`None` 是「沿用磁盘上的当前任务」的信号，
  传错会把图片附到磁盘上那条任务上、transcript 里同一条任务被写两次。
* **退出必收会话**：`_interactive` 退出（`:q` / EOF / Ctrl-C）时必须调 `end_session()`，
  否则 Runtime 被复用（嵌入 / 测试 / 同一进程再跑单次任务）时会莫名继承本会话记忆。
* **未完成项只以接力期间写过的计划为准**：`--auto` 不能只看 `plan.md` 里有没有 `- [ ]` ——
  磁盘上残留的勾选项未必属于这次接力，不该把接力拖满轮数。
* **没做完就非 0**：`--auto` 跑到轮数上限却仍有活时，退出码必须非 0，
  否则 CI 判不出「没做完」。
"""
from __future__ import annotations

import builtins
import io
import json
from contextlib import redirect_stdout

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


def _rt(tmp_path, **kw) -> Runtime:
    rt = Runtime(tmp_path, quiet=True, heartbeat=0, **kw)
    rt.config.api_key = rt.config.base_url = "stub"
    rt.config.model = "stub-model"
    rt.llm = _FinishLLM(rt.config)
    return rt


def _task_in_system(call) -> str:
    """从模型收到的一整组 messages 里取出「当前任务（三）」的正文。"""
    for m in call:
        if m.get("role") == "system" and isinstance(m.get("content"), str):
            if TASK_MARK in m["content"]:
                return m["content"].split(TASK_MARK, 1)[1].strip()
    return ""


def _user_texts(call) -> list[str]:
    out = []
    for m in call:
        if m.get("role") != "user":
            continue
        c = m.get("content")
        out.append(c if isinstance(c, str)
                   else "".join(p.get("text", "[img]") for p in c if isinstance(p, dict)))
    return out


def _fake_stdin(seq):
    it = iter(seq)

    def _input(prompt=""):
        try:
            return next(it)
        except StopIteration as exc:      # 模拟 EOF，让 -i 干净退出
            raise EOFError from exc
    return _input


# ================================================================ 不变量 1：纯图片输入
def test_image_only_interactive_does_not_reuse_task_on_disk(tmp_path, monkeypatch):
    """`:img X` + 空回车 = 纯图片任务，绝不沿用磁盘上的那条任务。"""
    rt = _rt(tmp_path)
    png = tmp_path / "a.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40)

    monkeypatch.setattr(builtins, "input",
                        _fake_stdin(["TASK-A: 第一条任务", f":img {png}", "", ":q"]))
    with redirect_stdout(io.StringIO()):
        cli._interactive(rt, "", False, [], auto=0, resume=0)

    tasks = [_task_in_system(c) for c in rt.llm.calls]
    assert tasks[0] == "TASK-A: 第一条任务"
    # 关键：第二条（纯图）不是磁盘上那条任务，而是「空任务 + 图片输入」
    assert tasks[1] != "TASK-A: 第一条任务"
    assert any("图片输入" in u for u in _user_texts(rt.llm.calls[1]))

    users = [t["user"] for t in rt.ws.read_turns(-1)]
    assert users[0] == "TASK-A: 第一条任务"
    assert users.count("TASK-A: 第一条任务") == 1      # 磁盘上那条任务只入档一次
    assert users[1] == ""                              # 纯图轮记空 user + 结论


def test_image_only_cli_does_not_reuse_task_on_disk(tmp_path, monkeypatch):
    """`openthree --image a.png`（不给任务）同样不得沿用磁盘上的任务。"""
    rt = _rt(tmp_path)
    rt.run("STALE-TASK: 磁盘上的任务")                 # 先落一条磁盘任务
    png = tmp_path / "a.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40)

    calls = []
    stub = _FinishLLM(rt.config)

    class _Rec(_FinishLLM):
        def chat(self, messages, **kw):
            calls.append(messages)
            return stub.chat(messages, **kw)

    monkeypatch.setattr("openthree.runtime.LLM", lambda cfg: _Rec(cfg))
    with redirect_stdout(io.StringIO()):
        code = cli.main(["--dir", str(tmp_path), "-q", "--image", str(png)])

    assert code == 0
    assert calls, "模型应被调用一次"
    assert _task_in_system(calls[-1]) != "STALE-TASK: 磁盘上的任务"
    assert any("图片输入" in u for u in _user_texts(calls[-1]))


def test_no_arg_run_still_reuses_task_on_disk(tmp_path, monkeypatch):
    """反证：`--no-interactive` 无参数时「沿用磁盘任务」这条路径照旧有效。

    注：不带参数的 `openthree` 是「进交互模式」，单次沿用要走 --no-interactive
    （或 stdin 不是终端）。这里两条都显式给出，免得测试依赖 pytest 的 stdin 形态。
    """
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
    rt = _rt(tmp_path)
    rt.run("STALE-TASK: 磁盘上的任务")

    calls = []
    stub = _FinishLLM(rt.config)

    class _Rec(_FinishLLM):
        def chat(self, messages, **kw):
            calls.append(messages)
            return stub.chat(messages, **kw)

    monkeypatch.setattr("openthree.runtime.LLM", lambda cfg: _Rec(cfg))
    with redirect_stdout(io.StringIO()):
        code = cli.main(["--dir", str(tmp_path), "-q", "--no-interactive"])

    assert code == 0
    assert _task_in_system(calls[-1]) == "STALE-TASK: 磁盘上的任务"


# ================================================================ 不变量 2：退出必收会话
def test_interactive_quit_closes_session(tmp_path, monkeypatch):
    """`:q` 退出时必须 end_session —— 否则 Runtime 复用会继承旧会话记忆。"""
    rt = _rt(tmp_path)
    monkeypatch.setattr(builtins, "input", _fake_stdin(["第一句", ":q"]))
    with redirect_stdout(io.StringIO()):
        cli._interactive(rt, "", False, [])
    assert rt._session_active is False
    assert rt._session_turns == []


def test_interactive_eof_closes_session(tmp_path, monkeypatch):
    """EOF 退出同理。"""
    rt = _rt(tmp_path)
    monkeypatch.setattr(builtins, "input", _fake_stdin(["第一句"]))   # 随后 EOF
    with redirect_stdout(io.StringIO()):
        cli._interactive(rt, "", False, [])
    assert rt._session_active is False
    assert rt._session_turns == []


def test_no_session_leak_into_later_single_run(tmp_path, monkeypatch):
    """会话退出后，紧接着的单次任务不该看到上一次会话的用户消息。"""
    rt = _rt(tmp_path)
    monkeypatch.setattr(builtins, "input", _fake_stdin(["上个会话的问题", ":q"]))
    with redirect_stdout(io.StringIO()):
        cli._interactive(rt, "", False, [])

    rt.llm = _FinishLLM(rt.config)
    rt.run("新的单次任务")
    roles = [m["role"] for m in rt.llm.calls[-1]]
    assert roles == ["system"]                          # 没有播种旧 user/assistant
    assert not any("上个会话的问题" in json.dumps(m, ensure_ascii=False)
                   for m in rt.llm.calls[-1])


# ================================================================ 不变量 3：计划归属
class _PlanStubLLM:
    """每轮 finish；按 plans 序列写计划，用 plan 的未完成项驱动接力。"""

    def __init__(self, cfg, plans):
        self.config = cfg
        self.plans = list(plans)
        self.round = 0

    def chat(self, messages, tools=None, tool_choice="auto", **kwargs):
        i = min(self.round, len(self.plans) - 1)
        plan = self.plans[i] if self.plans[i] is not None else None
        self.round += 1
        calls = [{"id": "f", "type": "function",
                  "function": {"name": "finish",
                               "arguments": json.dumps({"answer": f"第{self.round}轮结论"})}}]
        if plan is not None:
            calls.insert(0, {"id": "p", "type": "function",
                             "function": {"name": "write_plan",
                                          "arguments": json.dumps({"content": plan})}})
        msg = {"role": "assistant", "content": "", "tool_calls": calls}
        return {"message": msg, "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                "elapsed_ms": 0.0, "raw_request": {"messages": []},
                "raw_response": {"choices": [{"message": msg}]}, "error": None}


def test_stale_plan_does_not_force_extra_rounds(tmp_path):
    """磁盘上残留的计划（接力期间没动过）不该把 --auto 拖满轮数。"""
    rt = _rt(tmp_path)
    rt.ws.write_plan("# 与这次接力无关的计划\n\n- [ ] 残留未完成项\n")
    rt.llm = _PlanStubLLM(rt.config, [None])          # 接力期间不碰计划

    doc = rt.run_auto("完全无关的新任务", rounds=3)
    assert doc["auto_rounds"] == 1                    # 只跑一轮就收工
    assert doc["auto_unfinished"] is False


def test_touched_plan_still_drives_relay(tmp_path):
    """反证：接力期间自己写的计划，未完成项照常驱动接力。"""
    rt = _rt(tmp_path)
    rt.llm = _PlanStubLLM(rt.config, ["- [ ] 甲\n- [ ] 乙", "- [x] 甲\n- [x] 乙"])
    doc = rt.run_auto("长任务", rounds=5)
    assert doc["auto_rounds"] == 2
    assert doc["auto_open_items"] == 0
    assert doc["auto_unfinished"] is False


# ================================================================ 不变量 4：没做完就非 0
class _NeverDoneLLM:
    """每轮都写一条永远做不完的计划 + finish。"""

    def __init__(self, cfg):
        self.config = cfg

    def chat(self, messages, tools=None, tool_choice="auto", **kwargs):
        calls = [
            {"id": "p", "type": "function",
             "function": {"name": "write_plan",
                          "arguments": json.dumps({"content": "- [x] 做完的\n- [ ] 永远做不完的\n"})}},
            {"id": "f", "type": "function",
             "function": {"name": "finish",
                          "arguments": json.dumps({"answer": "本轮收尾，计划仍开着"})}},
        ]
        msg = {"role": "assistant", "content": "", "tool_calls": calls}
        return {"message": msg, "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                "elapsed_ms": 0.0, "raw_request": {"messages": []},
                "raw_response": {"choices": [{"message": msg}]}, "error": None}


def test_run_auto_marks_unfinished_when_hitting_round_cap(tmp_path):
    rt = _rt(tmp_path)
    rt.llm = _NeverDoneLLM(rt.config)
    doc = rt.run_auto("做不完的长任务", rounds=2)
    assert doc["auto_rounds"] == 2                    # 撞上限
    assert doc["auto_open_items"] == 1
    assert doc["auto_unfinished"] is True


def test_cli_exit_code_nonzero_when_auto_unfinished(tmp_path, monkeypatch):
    """CI 判据：--auto 没做完 → 退出码非 0。"""
    rt = _rt(tmp_path)
    rt.ws  # noqa: B018 —— 只是确保工作区就绪

    class _Rec(_NeverDoneLLM):
        pass

    monkeypatch.setattr("openthree.runtime.LLM", lambda cfg: _Rec(cfg))
    with redirect_stdout(io.StringIO()):
        code = cli.main(["--dir", str(tmp_path), "-q", "--auto", "2", "长任务"])
    assert code != 0


def test_cli_exit_code_zero_when_auto_done(tmp_path, monkeypatch):
    """反证：--auto 计划勾完正常收工 → 退出码 0。"""
    rt = _rt(tmp_path)

    class _Done(_PlanStubLLM):
        pass

    monkeypatch.setattr("openthree.runtime.LLM",
                        lambda cfg: _Done(cfg, ["- [x] 干完了"]))
    with redirect_stdout(io.StringIO()):
        code = cli.main(["--dir", str(tmp_path), "-q", "--auto", "3", "小事一桩"])
    assert code == 0
