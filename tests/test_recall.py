"""检索与长期记忆：search_files / recall_memory / 记忆注入 / 参数夹紧。

两条必须成立：
  * 模型要有 grep 式的感知带宽（search_files），而不是只能硬读整份文件；
  * 模型写下的长期记忆必须能被读回（recall_memory + 每轮注入），
    否则「记忆」就退化成只写不读的摆设。
"""
from __future__ import annotations

from pathlib import Path

from openthree.builtins import (BUILTIN_FUNCTIONS, _public_function_warning,
                                list_dir, read_file, recall_memory, remember,
                                search_files, write_function)
from openthree.workspace import Workspace

from conftest import *  # noqa: F401,F403  复用夹具


# ---------------------------------------------------------------- 工具已注册
def test_new_tools_are_builtin():
    assert "search_files" in BUILTIN_FUNCTIONS
    assert "recall_memory" in BUILTIN_FUNCTIONS


# ---------------------------------------------------------------- search_files
def test_search_files_finds_line_with_location(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\nneedle = 2\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.py").write_text("needle = 3\n", encoding="utf-8")
    out = search_files("needle", ".", glob="*.py")
    assert "命中 2 处" in out
    assert "a.py:2:" in out
    assert "b.py:1:" in out


def test_search_files_regex_and_ignore_case(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "m.txt").write_text("Hello World\n", encoding="utf-8")
    assert "无命中" in search_files("hello", ".", glob="*.txt")
    assert "m.txt" in search_files("hello", ".", glob="*.txt", ignore_case=True)
    assert "m.txt" in search_files(r"H\w+o", ".", glob="*.txt", regex=True)


def test_search_files_bad_regex_reports_error(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert search_files("(", ".", regex=True).startswith("[错误]")


def test_search_files_empty_pattern(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert "pattern 为空" in search_files("", ".")


def test_search_files_skips_noise_dirs_and_binaries(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "keep.py").write_text("needle\n", encoding="utf-8")
    noise = tmp_path / "node_modules"
    noise.mkdir()
    (noise / "junk.py").write_text("needle\n", encoding="utf-8")
    (tmp_path / "bin.dat").write_bytes(b"needle\x00\x01\x02")
    out = search_files("needle", ".")
    assert "keep.py" in out
    assert "node_modules" not in out
    assert "bin.dat" not in out


def test_search_files_can_see_own_workspace(tmp_path: Path, monkeypatch):
    """.openthree/{functions,prompts,memory} 是 agent 最该搜的地方，不能跳过。"""
    monkeypatch.chdir(tmp_path)
    wsdir = tmp_path / ".openthree"
    (wsdir / "functions").mkdir(parents=True)
    (wsdir / "functions" / "mine.py").write_text("MAGIC_TOKEN = 1\n", encoding="utf-8")
    (wsdir / "memory").mkdir(parents=True)
    (wsdir / "memory" / "memory.jsonl").write_text(
        '{"content":"MAGIC_TOKEN 的教训"}\n', encoding="utf-8")
    # 运行时 churn 的 logs/ 仍应被跳过
    (wsdir / "logs").mkdir(parents=True)
    (wsdir / "logs" / "junk.txt").write_text("MAGIC_TOKEN\n", encoding="utf-8")

    out = search_files("MAGIC_TOKEN", ".openthree")
    assert "functions" in out and "mine.py" in out
    assert "memory.jsonl" in out
    assert "logs" not in out


def test_search_files_missing_path(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert search_files("x", "does-not-exist").startswith("[错误]")


def test_search_files_respects_max_results(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "many.txt").write_text("\n".join("hit" for _ in range(50)),
                                       encoding="utf-8")
    out = search_files("hit", ".", max_results=5)
    assert "max_results=5" in out


def test_search_files_context_lines(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "c.txt").write_text("before\nneedle\nafter\n", encoding="utf-8")
    out = search_files("needle", ".", context=1)
    assert "before" in out and "after" in out


# ---------------------------------------------------------------- recall_memory
def test_recall_memory_reads_back_what_was_remembered(make_agent, ws: Workspace):
    make_agent()
    remember("第一条经验", ["a"])
    remember("第二条经验", ["b"])
    out = recall_memory()
    assert "第二条经验" in out and "第一条经验" in out
    assert "最近 2/2 条" in out


def test_recall_memory_filters_by_query(make_agent, ws: Workspace):
    make_agent()
    remember("关于 pytest 的坑", ["test"])
    remember("关于 asyncio 的坑", ["async"])
    out = recall_memory(query="pytest")
    assert "pytest" in out and "asyncio" not in out
    assert "匹配 1/2 条" in out


def test_recall_memory_query_matches_tags(make_agent, ws: Workspace):
    make_agent()
    remember("正文里没有关键词", ["特殊标签"])
    assert "特殊标签" in recall_memory(query="特殊")


def test_recall_memory_empty(make_agent, ws: Workspace):
    make_agent()
    assert "（空" in recall_memory()


def test_recall_memory_limit_clamped(make_agent, ws: Workspace):
    make_agent()
    for i in range(5):
        remember(f"记忆 {i}")
    out = recall_memory(limit=2)
    assert "最近 2/5 条" in out
    assert "记忆 4" in out and "记忆 0" not in out


# ---------------------------------------------------------------- workspace.read_memory
def test_read_memory_tolerates_corrupt_lines(ws: Workspace):
    ws.memory_file.parent.mkdir(parents=True, exist_ok=True)
    ws.memory_file.write_text(
        '{"ts":"t","content":"好的一条","tags":["x"]}\n'
        "这不是 json\n"
        '{"ts":"t2","content":"","tags":[]}\n'
        '{"ts":"t3","content":"另一条","tags":"坏标签类型"}\n',
        encoding="utf-8")
    entries = ws.read_memory()
    assert [e["content"] for e in entries] == ["好的一条", "另一条"]
    assert entries[1]["tags"] == []       # 标签类型不对时归一成空列表
    assert ws.count_memory() == 2


# ---------------------------------------------------------------- 记忆注入上下文
def test_runtime_block_injects_memory(ws, registry):
    ws.memory_file.parent.mkdir(parents=True, exist_ok=True)
    ws.memory_file.write_text('{"ts":"t","content":"可被注入的经验","tags":[]}\n',
                              encoding="utf-8")
    block = sent_block(ws, registry, model="m", iteration=1, max_iterations=200)
    assert "# 你的长期记忆" in block
    assert "可被注入的经验" in block
    assert "recall_memory" in block


def test_runtime_block_memory_is_absent_message_when_empty(ws, registry):
    block = sent_block(ws, registry, model="m", iteration=1, max_iterations=200)
    assert "# 你的长期记忆" in block
    assert "还没有长期记忆" in block


def test_runtime_block_caps_memory_chars(ws, registry):
    """单条记忆再长也不能把上下文挤爆：展示被截到 300 字符。"""
    ws.memory_file.parent.mkdir(parents=True, exist_ok=True)
    ws.memory_file.write_text(
        '{"ts":"t","content":"' + "长" * 5000 + '","tags":[]}\n', encoding="utf-8")
    block = sent_block(ws, registry, model="m", iteration=1, max_iterations=200)
    i = block.find("# 你的长期记忆")
    j = block.find("# 你上次的行动")
    assert j - i < 2000
    assert "…" in block[i:j]


# ---------------------------------------------------------------- 参数夹紧
def test_read_file_clamps_negative_offset(tmp_path: Path):
    p = tmp_path / "f.txt"
    p.write_text("l1\nl2\nl3\n", encoding="utf-8")
    out = read_file(str(p), offset=-5, limit=2)
    assert "显示 1-2" in out and "l1" in out


def test_read_file_non_positive_limit_reads_to_end(tmp_path: Path):
    p = tmp_path / "f.txt"
    p.write_text("a\nb\nc\n", encoding="utf-8")
    out = read_file(str(p), limit=-1)
    assert "显示 1-3" in out


def test_list_dir_clamps_zero_max_items(tmp_path: Path):
    (tmp_path / "one.txt").write_text("x", encoding="utf-8")
    out = list_dir(str(tmp_path), max_items=0)
    assert "one.txt" in out      # 至少显示 1 条，而不是空


# ---------------------------------------------------------------- write_function 告警
def test_public_function_warning_detects_private_only():
    assert _public_function_warning("def _inner():\n    pass\n") != ""
    assert _public_function_warning("def good():\n    pass\n") == ""
    assert _public_function_warning("X = 1\n") != ""


def test_write_function_warns_when_no_public_function(make_agent, ws: Workspace):
    make_agent()
    msg = write_function("only_private", "def _hidden():\n    return 1\n")
    assert msg.startswith("[ok]") and "没有可注册的公开函数" in msg
    assert (ws.functions_dir / "only_private.py").exists()


def test_write_function_no_warning_for_public(make_agent, ws: Workspace):
    make_agent()
    msg = write_function("fine", "def fine() -> str:\n    '''ok'''\n    return 'x'\n")
    assert msg.startswith("[ok]") and "没有可注册的公开函数" not in msg
