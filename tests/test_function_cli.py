"""function CLI 与注册判据的回归测试。

锁住三条**现在必须成立**的不变量，任何一条被改坏都说明「工具集」与
「命令行」开始发散：

1. 命名三档：`__x__` 协议/入口、`_x` 私有、`x` 公开 —— 只有第三种会成为工具。
2. CLI 契约：退出码 0/1/2、`--json` 单行纯 ASCII、「不传」与「传 0」可分。
3. 尾钩自洽：加了 `def __main__` 的文件，工具集一字不变，且 `--help` 真能跑。
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from openthree.builtins import _static_audit, audit_functions
from openthree.cli_tool import _build_from_globals, _returns_hint, run_tools
from openthree.tool import build_tool, is_registrable

ROOT = Path(__file__).resolve().parents[1]


def _load(tmp_path: Path, name: str, code: str):
    """把源码写成文件、按注册器的同一路径 exec_module，返回模块。"""
    path = tmp_path / f"{name}.py"
    path.write_text(code, encoding="utf-8")
    mod_name = f"openthree_user_functions.{name}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- 1. 命名三档
MIXED = '''
PUB_LAMBDA = lambda: "x"

def real(a: int = 0) -> str:
    """真的函数。"""
    return ""

def main(a: int = 0) -> str:
    """用了保留名。"""
    return ""

def _helper() -> str:
    return ""

def __main__(argv=None) -> int:
    return 0

class C:
    pass
'''


def test_only_plain_public_functions_are_registrable(tmp_path):
    mod = _load(tmp_path, "mixed", MIXED)
    names = {n for n in dir(mod) if is_registrable(mod.__name__, n, getattr(mod, n))}
    # lambda 的 __module__ 是本模块，所以它确实会被注册（工具名会变成 <lambda>）
    assert names == {"real", "main", "PUB_LAMBDA"}
    assert "__main__" not in names
    assert "_helper" not in names
    assert "C" not in names


def test_lambda_leak_produces_lambda_tool_name(tmp_path):
    """lambda 泄漏的后果：工具名变成 `<lambda>`，多个这样的名字会互相覆盖。"""
    mod = _load(tmp_path, "leak", MIXED)
    tool = build_tool(mod.PUB_LAMBDA)
    assert tool.name == "<lambda>"


def test_dunder_entry_is_never_registered(tmp_path):
    mod = _load(tmp_path, "entry", MIXED)
    assert callable(mod.__main__)
    assert not is_registrable(mod.__name__, "__main__", mod.__main__)


def test_cli_exposes_exactly_the_registrable_functions(tmp_path):
    mod = _load(tmp_path, "clicount", MIXED)
    tools = _build_from_globals(vars(mod))
    assert set(tools) == {"real", "main", "PUB_LAMBDA"}


# ---------------------------------------------------------------- 2. CLI 契约
@pytest.fixture
def two_fn_mod(tmp_path):
    return _load(tmp_path, "twofn", '''
def ok(a: int = 0) -> str:
    """总是成功。

    Args:
        a: 一个整数

    Returns:
        成功文本。
    """
    return f"[ok] a={a}"

def bad(reason: str = "原因") -> str:
    """总是失败。"""
    return f"[错误] {reason}"

def boom() -> str:
    """抛异常。"""
    raise ValueError("炸了")
''')


def _run(mod, argv):
    """在子进程里跑一次 CLI，拿到真实字节（编码断言必须看字节）。"""
    return subprocess.run(
        [sys.executable, "-c",
         "import sys, runpy; sys.argv=['x']+sys.argv[1:];"
         "exec(open(sys.argv[0]).read())"],
        capture_output=True, cwd=str(ROOT))


def test_returns_hint_extracts_returns_section():
    desc = "做某事。\n\nArgs:\n    a: 甲\n\nReturns:\n    一段返回值说明。\n\n参数：\n- a: 甲"
    assert _returns_hint(desc) == "一段返回值说明。"


def test_returns_hint_missing_returns():
    assert _returns_hint("做某事。\n\nArgs:\n    a: 甲") == ""


def test_exit_code_zero_on_success(two_fn_mod, capsys):
    rc = run_tools(_build_from_globals(vars(two_fn_mod)), argv=["ok", "--a", "3"])
    assert rc == 0
    assert "a=3" in capsys.readouterr().out


def test_exit_code_one_on_error_prefix(two_fn_mod, capsys):
    rc = run_tools(_build_from_globals(vars(two_fn_mod)), argv=["bad"])
    assert rc == 1
    assert "[错误]" in capsys.readouterr().err


def test_exit_code_two_on_exception(two_fn_mod, capsys):
    rc = run_tools(_build_from_globals(vars(two_fn_mod)), argv=["boom"])
    assert rc == 2
    assert "ValueError" in capsys.readouterr().err


def test_json_is_single_line_and_ascii(two_fn_mod, capsys):
    rc = run_tools(_build_from_globals(vars(two_fn_mod)),
                   argv=["bad", "--json", "--reason", "中文原因"])
    assert rc == 1
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(lines) == 1, "多行输出会打断行式消费者"
    out.encode("ascii")                     # 纯 ASCII：任何 locale 都解得动
    payload = json.loads(lines[0])
    assert payload["ok"] is False
    assert payload["fn"] == "bad"


def test_json_before_subcommand_is_not_overwritten(two_fn_mod, capsys):
    """子 parser 的默认值不能把顶层的 --json 覆盖掉（曾是一个真 bug）。"""
    rc = run_tools(_build_from_globals(vars(two_fn_mod)), argv=["--json", "ok"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is True


def test_quiet_suppresses_output(two_fn_mod, capsys):
    rc = run_tools(_build_from_globals(vars(two_fn_mod)), argv=["ok", "--quiet"])
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_missing_optional_int_is_not_zero(tmp_path, capsys):
    """「不传」必须是 None，「传 0」必须是 0 —— 两者不能压成一个。"""
    mod = _load(tmp_path, "optint", '''
def probe(n: int = 5, flag: bool = False) -> str:
    """回显。"""
    return f"[ok] n={n} flag={flag}"
''')
    tools = _build_from_globals(vars(mod))
    run_tools(tools, argv=["--n", "0"])
    assert "n=0" in capsys.readouterr().out
    run_tools(tools, argv=[])
    assert "n=5" in capsys.readouterr().out


# ---------------------------------------------------------------- 3. 静态审计
def test_static_audit_detects_main_guard():
    ok = 'def go() -> str:\n    """g。"""\n    return ""\n\n' \
         'if __name__ == "__main__":\n    pass\n'
    assert _static_audit(ok)["has_main_guard"] is True
    assert _static_audit('def go() -> str:\n    """g。"""\n    return ""\n')["has_main_guard"] is False


def test_static_audit_detects_lambda_leak_and_reserved():
    a = _static_audit(MIXED)
    assert a["leaked"] == ["PUB_LAMBDA"]
    assert a["reserved"] == ["main"]


# ---------------------------------------------------------------- 4. 真实文件
def test_safe_edit_has_main_hook_and_help_runs():
    """存量文件必须带尾钩，且 `python 本文件.py --help` 真能跑通。"""
    path = ROOT / ".openthree" / "functions" / "safe_edit.py"
    if not path.is_file():
        pytest.skip("本工作区没有 safe_edit.py")
    res = subprocess.run([sys.executable, str(path), "--help"],
                         capture_output=True, cwd=str(ROOT), timeout=120)
    assert res.returncode == 0, res.stderr.decode("utf-8", "replace")
    out = res.stdout.decode("utf-8")
    assert "replace_in_file" in out
    assert "insert_lines_after" in out
    assert "__main__" not in out          # 入口本身不该变成子命令


def test_builtins_cli_lists_all_tools():
    """包内模块只能用 `-m` 运行；这条同时守住「不能用 python 直跑」的事实。"""
    res = subprocess.run([sys.executable, "-m", "openthree.builtins", "--help"],
                         capture_output=True, cwd=str(ROOT), timeout=120)
    assert res.returncode == 0, res.stderr.decode("utf-8", "replace")
    out = res.stdout.decode("utf-8")
    assert "read_file" in out
    assert "__main__" not in out


def test_builtins_direct_run_fails_as_documented():
    """`python openthree/builtins.py` 必然失败（相对导入），文档里已写明。"""
    res = subprocess.run([sys.executable, str(ROOT / "openthree" / "builtins.py"),
                          "--help"], capture_output=True, cwd=str(ROOT), timeout=120)
    assert res.returncode != 0
    assert "relative import" in res.stderr.decode("utf-8", "replace")
