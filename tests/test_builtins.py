"""内置工具 + 注册器：编码、语法预检、冲突记录、路径约束。"""
from __future__ import annotations

from pathlib import Path

from openthree.builtins import _decode, _syntax_error, run_shell
from openthree.registry import Registry

from conftest import *  # noqa: F401,F403  复用夹具


# ---------------------------------------------------------------- run_shell 编码
def test_decode_utf8_bytes():
    assert _decode("中文".encode("utf-8")) == "中文"


def test_decode_windows_bytes():
    text = "系统找不到指定的文件。"
    assert _decode(text.encode("gbk")) == text


def test_decode_none():
    assert _decode(None) == ""


def test_run_shell_reports_exit_code():
    out = run_shell("python -c \"print('hello')\"")
    assert "exit=0" in out and "hello" in out


# ---------------------------------------------------------------- write_function
def test_syntax_error_detected():
    assert "line" in _syntax_error("def broken(:\n  pass\n")
    assert _syntax_error("def ok(a: int) -> int:\n    return a\n") == ""


def test_write_function_rejects_bad_syntax(make_agent, ws):
    from openthree.builtins import write_function
    make_agent()
    msg = write_function("broken_tool", "def broken(:\n  pass\n")
    assert msg.startswith("[错误]") and "line" in msg


def test_write_function_accepts_good_syntax(make_agent, ws):
    from openthree.builtins import write_function
    make_agent()
    msg = write_function("good_tool", "def good(a: int) -> str:\n    '''ok'''\n    return str(a)\n")
    assert msg.startswith("[ok]")
    assert (ws.functions_dir / "good_tool.py").exists()


# ---------------------------------------------------------------- 注册器
def test_registry_records_name_conflict(ws):
    (ws.functions_dir / "clash.py").write_text(
        "def read_file(path: str):\n    '''shadow'''\n    return 'shadow'\n", encoding="utf-8")
    reg = Registry(ws)
    from openthree.builtins import BUILTIN_FUNCTIONS
    for name, fn in BUILTIN_FUNCTIONS.items():
        reg.register_builtin(name, fn)
    reg.freeze_builtins()
    reg.reload()
    assert any("冲突" in e.error for e in reg.load_errors)
    assert reg.get("read_file").origin == "user"


def test_registry_loads_user_tool(ws):
    (ws.functions_dir / "hello.py").write_text(
        "def hello(name: str = '世界') -> str:\n"
        "    '''\'打招呼\'\n\n    Args:\n        name: 名字\n    '''\n"
        "    return f'你好 {name}'\n",
        encoding="utf-8",
    )
    reg = Registry(ws)
    reg.reload()
    tool = reg.get("hello")
    assert tool is not None and tool.origin == "user"
    assert "name" in tool.parameters
