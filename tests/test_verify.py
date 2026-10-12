"""验收闭环：静态审计、子进程验收（verify_tool）、代码大纲、历史版本、签名错误提示。

对应用户诉求「保持鲁棒性/健壮性，在内核极简、有效的基础上完善功能，
最大化模型能力」——这些是「能力自生长」的护栏与感知带宽。
"""
from __future__ import annotations

import pytest

from openthree.agent import Agent
from openthree.builtins import (BUILTIN_FUNCTIONS, _static_audit, code_outline,
                                list_functions, list_prompt_versions, read_prompt_version,
                                read_prompts, restore_system_prompt, verify_tool,
                                write_function, write_system_prompt)
from openthree.kernel import KERNEL
from openthree.llm import LLM, LLMConfig
from openthree.recorder import Recorder


# ---------------------------------------------------------------- 静态审计
def test_static_audit_lists_public_functions():
    a = _static_audit("def a(x: int) -> int:\n    return x\n\n"
                      "def _hidden():\n    pass\n")
    assert a["functions"] == ["a"]
    assert a["all_functions"] == ["a", "_hidden"]
    assert a["syntax_error"] == ""


def test_static_audit_reports_syntax_error():
    a = _static_audit("def broken(:\n    pass\n")
    assert "line" in a["syntax_error"]
    assert a["functions"] == []


def test_static_audit_flags_dangerous_calls():
    a = _static_audit("import os\nimport subprocess\n\n"
                      "def go():\n"
                      "    os.system('ls')\n"
                      "    subprocess.run(['x'])\n"
                      "    eval('1')\n")
    assert "os.system" in a["dangerous"]
    assert "subprocess.run" in a["dangerous"]
    assert "eval" in a["dangerous"]


def test_static_audit_flags_shadowing_builtin():
    builtins = set(BUILTIN_FUNCTIONS)
    a = _static_audit("def read_file(path: str) -> str:\n    return ''\n\n"
                      "def my_own() -> str:\n    return ''\n", builtin_names=builtins)
    assert a["shadows"] == ["read_file"]


def test_static_audit_ignores_async_and_nested():
    a = _static_audit("class C:\n    def m(self):\n        pass\n\n"
                      "async def top() -> None:\n    pass\n")
    # 只有顶层函数会被注册；类的方法不是顶层函数
    assert a["functions"] == ["top"]


# ---------------------------------------------------------------- write_function 接审计
def test_write_function_warns_on_shadow(ws, registry):
    KERNEL.workspace = ws
    KERNEL.registry = registry
    out = write_function("shadow_demo",
                         "def list_dir(path: str = '.') -> str:\n    return ''\n")
    assert "覆盖内置" in out
    assert "list_dir" in out


def test_write_function_no_public_function_warns(ws, registry):
    KERNEL.workspace = ws
    KERNEL.registry = registry
    out = write_function("only_private", "def _inner():\n    pass\n")
    assert "没有可注册的公开函数" in out


def test_write_function_dangerous_call_is_noted(ws, registry):
    KERNEL.workspace = ws
    KERNEL.registry = registry
    out = write_function("uses_shell",
                         "import subprocess\n\n"
                         "def probe(host: str) -> str:\n"
                         "    return subprocess.run(['echo', host]).stdout\n")
    assert "高危调用" in out


# ---------------------------------------------------------------- verify_tool 子进程验收
def test_verify_tool_accepts_good_module(ws, registry):
    KERNEL.workspace = ws
    KERNEL.registry = registry
    write_function("good", "def hello(name: str = 'world') -> str:\n"
                           "    \"\"\"打个招呼。\"\"\"\n"
                           "    return 'hi ' + name\n")
    out = verify_tool("good")
    assert "验收通过" in out
    assert "hello" in out


def test_verify_tool_detects_import_failure(ws, registry):
    KERNEL.workspace = ws
    KERNEL.registry = registry
    write_function("bad", "import module_that_does_not_exist_xyz\n\n"
                          "def f() -> str:\n    return 'x'\n")
    out = verify_tool("bad")
    assert "验收未通过" in out


def test_verify_tool_reports_missing(ws, registry):
    KERNEL.workspace = ws
    KERNEL.registry = registry
    out = verify_tool("never_written")
    assert "找不到" in out


def test_verify_tool_warns_when_no_public_function(ws, registry):
    KERNEL.workspace = ws
    KERNEL.registry = registry
    write_function("priv", "def _only():\n    pass\n")
    out = verify_tool("priv")
    assert "验收通过" in out
    assert "没有任何可注册的公开函数" in out


# ---------------------------------------------------------------- code_outline
def test_code_outline_is_builtin():
    assert "code_outline" in BUILTIN_FUNCTIONS


def test_code_outline_extracts_signatures(tmp_path):
    p = tmp_path / "sample.py"
    p.write_text(
        '"""模块文档。"""\n'
        "import os\n\n"
        "class Foo:\n"
        '    """一个类。"""\n'
        "    def bar(self, x: int = 1) -> str:\n"
        '        """方法。"""\n'
        "        return ''\n\n"
        "def top(a: str, b: int) -> None:\n"
        '    """顶层函数。"""\n'
        "    pass\n",
        encoding="utf-8",
    )
    out = code_outline(str(p))
    assert "class Foo" in out
    assert "def bar" in out
    assert "def top(a: str, b: int)" in out
    assert "L" in out                       # 带行号


def test_code_outline_reports_syntax_error(tmp_path):
    p = tmp_path / "broken.py"
    p.write_text("def bad(:\n", encoding="utf-8")
    assert "语法解析失败" in code_outline(str(p))


# ---------------------------------------------------------------- 历史版本（自省）
def test_read_prompt_version_negative_is_overview(ws, registry):
    KERNEL.workspace = ws
    KERNEL.registry = registry
    write_system_prompt("第二版")
    out = read_prompt_version("-1")
    assert "回看最近" in out


def test_read_prompt_version_by_index(ws, registry):
    KERNEL.workspace = ws
    KERNEL.registry = registry
    original_first = ws.read_system_prompt().splitlines()[0]
    write_system_prompt("第二版")
    out = read_prompt_version("1")          # 1 = 最新存档 = 覆写前的原稿
    assert original_first in out


def test_read_prompt_version_missing(ws, registry):
    KERNEL.workspace = ws
    KERNEL.registry = registry
    write_system_prompt("有新版本了")
    out = read_prompt_version("99")
    assert "找不到版本" in out


def test_read_prompt_version_without_history(ws, registry):
    KERNEL.workspace = ws
    KERNEL.registry = registry
    assert "还没有历史版本" in read_prompt_version("1")


def test_negative_index_reads_older_version(ws):
    """负数序号 = 用来回看更早的版本（-N ↔ 正数序号 N+1）。"""
    ws.write_system_prompt("v2")
    ws.write_system_prompt("v3")
    assert len(ws.list_prompt_versions()) == 2
    # -1 应为「第二新」= 最早那一版（种子原稿）
    older = ws.read_prompt_version("-1")
    assert older is not None
    assert "你是谁" in older                # 种子提示词首行


def test_versions_sorted_by_time_not_raw_name(ws):
    """同一秒内多次覆写：`system-<秒>-1.md` 必须比 `system-<秒>.md` 新。

    裸按文件名字符串排序会因 '.' < '-' 而把顺序排反，「1 = 最新」随之指错版本。
    """
    ws.write_system_prompt("v2")
    ws.write_system_prompt("v3")
    versions = ws.list_prompt_versions()
    assert len(versions) == 2
    # 最新一版存的是「写 v3 之前」的 v2
    assert "v2" in versions[0].read_text(encoding="utf-8")
    assert "你是谁" in versions[1].read_text(encoding="utf-8")


def test_restore_writes_back_a_recoverable_snapshot(ws):
    """回退也是覆写：回退前会把「被顶掉的现稿」再存一份，故回退本身可逆。"""
    ws.write_system_prompt("坏稿")
    assert ws.restore_system_prompt("1")            # 1 = 原稿
    assert "坏稿" not in ws.read_system_prompt()
    # 回退时「坏稿」已被存档 —— 通过 search_files / read_file 能找回，且是有效存档
    versions = ws.list_prompt_versions()
    assert any("坏稿" in v.read_text(encoding="utf-8") for v in versions)


def test_read_prompts_starts_with_section_index(ws, registry):
    KERNEL.workspace = ws
    out = read_prompts()
    assert "章节索引" in out
    assert "# 你是谁" in out


# ---------------------------------------------------------------- 签名错误提示
def _stub_agent(ws, registry) -> Agent:
    rec = Recorder(ws, user_input="t", model="stub")
    ag = Agent(ws, registry, LLM(LLMConfig(api_key="k", base_url="u")), rec)
    KERNEL.workspace = ws
    KERNEL.registry = registry
    KERNEL.recorder = rec
    KERNEL.agent = ag
    return ag


def test_tool_type_error_includes_signature(ws, registry):
    ag = _stub_agent(ws, registry)
    out, err = ag._execute("read_file", {"wrong_param": 1})
    assert err
    assert "正确签名" in err
    assert "read_file(" in err
    assert "必填" in err


def test_reload_functions_lists_tools(ws, registry):
    KERNEL.workspace = ws
    KERNEL.registry = registry
    from openthree.builtins import reload_functions
    out = reload_functions()
    assert "当前工具清单" in out
