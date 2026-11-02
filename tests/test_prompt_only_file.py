"""纯提示词文件：只带 `__system_prompts__`、一个工具都没有。

这是**可选里的可选**（方案 §6）：只有真打算「把一段独立规范写成 py 文件、
随 profile 一起走」时才需要它。被保护的是判据的一致性 —— 注册器加载、
`function_conformance` 审计、`write_function` 落盘前检查、`register_function`
登记、`verify_tool` 验收、`cli_tool` 退出码，对「这个文件合不合法」必须给出
同一个答案。不一致的代价很具体：一句「没有可注册的公开函数」会让人以为这个
用法不成立，而它恰好是这个功能最好的用法。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from openthree.builtins import function_conformance
from openthree.cli_tool import run_cli
from openthree.kernel import KERNEL
from openthree.prompt import build_attached_block
from openthree.registry import PROMPT_ATTR

from conftest import *  # noqa: F401,F403  复用 ws / registry 夹具

ROOT = Path(__file__).resolve().parents[1]

SPEC_ONLY = '''\
"""发版规范 —— 只有一段说明，没有工具（可选用法）。"""

__system_prompts__ = """## 发版顺序

build → twine check → upload。upload 之后必须核对 sha256。
"""


def __main__(argv=None) -> int:
    """命令行入口。"""
    from openthree.cli_tool import run_cli
    return run_cli(globals(), argv=argv)


if __name__ == "__main__":
    raise SystemExit(__main__())
'''


def _write_spec(ws) -> Path:
    ws.functions_dir.mkdir(parents=True, exist_ok=True)
    path = ws.functions_dir / "release_spec.py"
    path.write_text(SPEC_ONLY, encoding="utf-8")
    return path


def test_reload_accepts_prompt_only_file(ws, registry):
    """注册器不把它当坏文件：没有工具是设计，不是失败。"""
    path = _write_spec(ws)
    registry.reload()

    assert not [e for e in registry.load_errors if e.file == str(path)]
    assert registry.prompts and "发版顺序" in registry.prompts[0].text
    assert "发版顺序" in build_attached_block(registry)


def test_conformance_has_no_issues(ws):
    """审计的结论与注册器一致：这份文件合规。"""
    path = _write_spec(ws)
    rep = function_conformance(path)

    assert rep["has_prompts"] is True
    assert rep["issues"] == []


def test_cli_prints_prompts_when_there_are_no_tools(ws, capsys):
    """没有工具、只有说明 ⇒ 打印正文、退出 0（不是「这个文件里没有函数」）。"""
    _write_spec(ws)
    funcs = {"__name__": "release_spec", PROMPT_ATTR: "## 发版顺序\nbuild → upload"}
    assert run_cli(funcs, argv=[]) == 0
    assert "发版顺序" in capsys.readouterr().out


def test_cli_print_prompts_flag(ws, capsys):
    """`--print-prompts` 让人和模型看到同一份东西（可 diff、可评审）。"""
    funcs = {"__name__": "release_spec", PROMPT_ATTR: ["## 第一段", "## 第二段"]}
    assert run_cli(funcs, argv=["--print-prompts"]) == 0
    out = capsys.readouterr().out
    assert "第一段" in out and "第二段" in out


def test_file_tail_hook_actually_works(ws):
    """真跑一次子进程：尾钩 + `--print-prompts` 在这条路径上也成立。"""
    path = _write_spec(ws)
    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8")
    proc = subprocess.run([sys.executable, str(path), "--print-prompts"],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "发版顺序" in proc.stdout


# ---------------------------------------------------------------- 查询语义
def test_cli_print_prompts_without_prompts_is_silent(capsys):
    """没有那段说明时，`--print-prompts` 打出零字节、退出 0。

    代价很具体：若只在「自己有提示词」时才认这个选项，同一句命令在别的文件上
    就变成「未识别的参数」—— 那是在说「这个选项不存在」，而事实是「这个文件
    没有那段说明」。查询永远该回答，答案可以是空。
    """
    assert run_cli({"__name__": "plain"}, argv=["--print-prompts"]) == 0
    assert capsys.readouterr().out == ""


def test_cli_reports_broken_prompt_instead_of_no_functions(capsys):
    """写坏了要说「哪里坏了」—— 不说「这个文件里没有可注册的公开函数」。"""
    assert run_cli({"__name__": "bad", PROMPT_ATTR: 42}, argv=["--print-prompts"]) == 1
    cap = capsys.readouterr()
    assert "只接受" in cap.err
    assert "没有可注册的公开函数" not in cap.err


def test_cli_help_is_not_the_body(capsys):
    """`--help` 问的是「这个程序怎么用」，不是「我的正文是什么」。"""
    assert run_cli({"__name__": "spec", PROMPT_ATTR: "## 发版顺序"}, argv=["--help"]) == 0
    out = capsys.readouterr().out
    assert "发版顺序" not in out
    assert "--print-prompts" in out


# ---------------------------------------------------------------- 登记 / 验收通道
@pytest.fixture
def user_home(tmp_path, monkeypatch):
    """把 user 档（`~/.openthree/functions`）搬进 tmp_path —— 只有这一档需要登记。"""
    home = tmp_path / "home"
    functions = home / ".openthree" / "functions"
    functions.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    return functions


def test_register_accepts_prompt_only_file(make_agent, ws, user_home):
    """登记通道的判据必须与注册器一致：纯提示词文件也能被授权进来。

    四种判据（注册器 / 审计 / `write_function` / CLI）说同一句话，第四条是
    登记 —— 它若继续回「没有可注册的公开函数」，这个用法在 user 档就不成立。
    """
    from openthree.builtins import register_function
    from openthree.registry import Registry

    make_agent()
    far = user_home / "spec_only.py"
    far.write_text(SPEC_ONLY, encoding="utf-8")
    reg: Registry = KERNEL.registry              # type: ignore[assignment]
    reg.reload()
    assert not reg.prompts, "没登记就不该进 system"

    out = register_function(str(far))
    assert "已登记" in out
    assert any(p.file == str(far) for p in reg.prompts), out
    assert "附随提示词" in out, "要让调用者知道这次登记确实带来了东西"


def test_verify_tool_reports_prompt(make_agent, ws):
    """验收通道也要说出「它带来了什么」：纯提示词文件不是「没有公开函数」。"""
    from openthree.builtins import verify_tool

    make_agent()
    _write_spec(ws)
    out = verify_tool("release_spec")
    assert "验收通过" in out
    assert "附随提示词" in out and "发版顺序" in out
    assert "没有任何可注册的公开函数" not in out
