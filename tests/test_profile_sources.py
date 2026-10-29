"""`-profile`：额外的 function 来源。

守住的几条不变量（改这里之前先读一遍）：

1. **点名即授权**：`-profile` 列出的目录会被**自动加载**；用户目录不会（那要
   `register_function`）。这是「存在 ≠ 可用」唯一还在守的那一档。
2. **距离决定谁赢**：project 恒 0，profile 按命令行顺序 1,2,…；裁决只比这个数，
   不拿 `kind` 字符串做相等比较。
3. **profile 只贡献 functions**：memory / prompts / config / limits / plan 一概
   不读 —— 它们是「这具躯体的经历与设置」，换到另一个项目就不成立。
4. **写入必须自知生效范围**：落到 profile 的写入，返回里必须说明它在本项目里
   会不会生效（被更近的同名工具盖住是常态）。
5. **校验早于落盘**：`-profile` 打错路径时返回 exit 2，且**不许**顺手把项目
   工作区建出来 —— `Runtime.__init__` 会 `ws.init()`，所以校验必须在它之前。
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from openthree import cli
from openthree.builtins import (BUILTIN_FUNCTIONS, list_functions,
                                write_function)
from openthree.kernel import KERNEL
from openthree.llm import load_config
from openthree.prompt import build_context_block, build_static_block
from openthree.registry import Registry
from openthree.workspace import Workspace


def _tool(name: str, body: str) -> str:
    return (f'"""测试用 function。"""\n\n\n'
            f'def {name}(text: str = "{body}") -> str:\n'
            f'    """回声测试。\n\n    Args:\n        text: 内容\n\n'
            f'    Returns:\n        原样返回\n    """\n'
            f'    return text\n')


@pytest.fixture
def proj(tmp_path):
    """一个项目 + 两个候选 profile（`base` / `team`），各自 functions 就绪。"""
    root = tmp_path / "proj"
    (root / ".openthree" / "functions").mkdir(parents=True)
    for name in ("base", "team"):
        (tmp_path / name / ".openthree" / "functions").mkdir(parents=True)
    return root, tmp_path


def _fn(profile: Path, file: str) -> Path:
    return profile / ".openthree" / "functions" / file


@pytest.fixture
def make_kernel(proj, monkeypatch):
    """构造内核，可挂若干 profile（绝对路径）；不传 = 一个都不挂。"""
    root, _ = proj

    def _make(profiles=()):
        ws = Workspace(root, profiles=tuple(profiles)).init()
        reg = Registry(ws)
        for name, fn in BUILTIN_FUNCTIONS.items():
            reg.register_builtin(name, fn)
        reg.freeze_builtins()
        reg.reload()
        monkeypatch.setattr(KERNEL, "workspace", ws, raising=False)
        monkeypatch.setattr(KERNEL, "registry", reg, raising=False)
        monkeypatch.setattr(KERNEL, "recorder", None, raising=False)
        return ws, reg, root

    return _make


# ---------------------------------------------------------------- 1. 点名即加载
def test_profile_tool_is_loaded_at_distance_1(make_kernel, proj):
    _, base = proj[0], proj[1]
    _fn(base / "base", "hello.py").write_text(_tool("hello", "hi"), encoding="utf-8")
    ws, reg, _ = make_kernel([base / "base"])

    tool = reg.get("hello")
    assert tool is not None, "profile 点名的目录必须自动加载"
    assert tool.source_distance == 1
    # `source_kind` 存的是给人看的 label（`FunctionSource.label`），形如 `profile:<目录>`；
    # 判据永远是 distance，这里只确认标签没写错来源。
    assert tool.source_kind.startswith("profile:")
    assert "hello" in list_functions()
    assert "来自 profile" in list_functions(), "读数要跟上：它单独一桶，不是「外部登记」"


# ---------------------------------------------------------------- 2. project 优先
def test_project_wins_over_profile(make_kernel, proj):
    root, base = proj
    _fn(base / "base", "hello.py").write_text(_tool("hello", "from-profile"),
                                              encoding="utf-8")
    _fn(root, "hello.py").write_text(_tool("hello", "from-project"), encoding="utf-8")
    ws, reg, _ = make_kernel([base / "base"])

    assert reg.get("hello").func() == "from-project"
    assert any(o.tool == "hello" and o.winner_kind == "project"
               for o in reg.overrides), "覆盖要如实记录"


# ---------------------------------------------------------------- 3. 先写先赢
def test_first_profile_wins_and_order_flips_result(make_kernel, proj):
    _, base = proj[0], proj[1]
    for name, body in (("base", "from-base"), ("team", "from-team")):
        _fn(base / name, "hello.py").write_text(_tool("hello", body), encoding="utf-8")

    _, reg, _ = make_kernel([base / "base", base / "team"])
    assert reg.get("hello").func() == "from-base", "先写的更近（PATH 语义）"

    _, reg2, _ = make_kernel([base / "team", base / "base"])
    assert reg2.get("hello").func() == "from-team", "顺序反过来，结论也反过来"


# ---------------------------------------------------------------- 4. 坏文件不致命
def test_broken_profile_file_is_reported_not_fatal(make_kernel, proj):
    _, base = proj[0], proj[1]
    _fn(base / "base", "broken.py").write_text("def oops(:\n", encoding="utf-8")
    _fn(base / "base", "ok.py").write_text(_tool("ok", "fine"), encoding="utf-8")
    ws, reg, _ = make_kernel([base / "base"])

    assert reg.get("ok") is not None, "同目录里好的那个照样加载"
    assert any("broken.py" in e.file for e in reg.load_errors)


# ---------------------------------------------------------------- 5. N=0 无差异
def test_no_profile_keeps_tool_list_identical(make_kernel, proj):
    """不挂 profile 时，工具清单与「有没有 profile 这个概念」无关。"""
    _, base = proj[0], proj[1]
    _fn(base / "base", "hello.py").write_text(_tool("hello", "hi"), encoding="utf-8")
    _, reg_none, _ = make_kernel()
    names_none = set(reg_none.tools)

    assert "hello" not in names_none, "没点名的目录里那个工具绝不该出现"
    _, reg_one, _ = make_kernel([base / "base"])
    assert "hello" in reg_one.tools, "点名后才出现 —— 差别只在于此"
    assert names_none <= set(reg_one.tools)


def test_no_profile_static_block_is_byte_identical(make_kernel, proj):
    """HEAD/TAIL 拆分的唯一承诺：不挂 profile 时，静态块**一个字节都不多**。

    这比「工具清单相同」更强 —— 它守的是结构与字节：多一行、少一个换行都会
    打破缓存前缀，而工具清单相同掩盖不了这件事。

    用一个**空的** profile（不贡献工具），于是剩下的差异只可能来自
    「挂了哪个 profile」那一行本身。
    """
    _, base = proj[0], proj[1]
    ws_none, reg_none, _ = make_kernel()
    ws_one, reg_one, _ = make_kernel([base / "base"])

    with_one = build_static_block(ws_one, reg_one, model="m")
    assert "- profile：" in with_one

    stripped = [ln for ln in with_one.splitlines() if not ln.startswith("- profile：")]
    plain = build_static_block(ws_none, reg_none, model="m")
    assert "- profile：" not in plain
    assert stripped == plain.splitlines()


# ---------------------------------------------------------------- 6. 只贡献 functions
def test_profile_contributes_functions_only(make_kernel, proj):
    """挂一个塞满别的东西的 profile：那些**一个都不许被读**。"""
    root, base = proj
    p = base / "base"
    (p / ".openthree" / "memory").mkdir(parents=True, exist_ok=True)
    (p / ".openthree" / "memory" / "memory.jsonl").write_text(
        '{"ts": "2026-01-01", "content": "SECRET_MEMORY", "tags": ""}\n',
        encoding="utf-8")
    (p / ".openthree" / "prompts").mkdir(parents=True, exist_ok=True)
    (p / ".openthree" / "prompts" / "system-prompts.md").write_text(
        "SECRET_PROMPT", encoding="utf-8")
    (p / ".openthree" / "prompts" / "plan.md").write_text("SECRET_PLAN", encoding="utf-8")
    (p / ".openthree" / "config.yaml").write_text(
        "model: SECRET_MODEL\n", encoding="utf-8")
    (p / ".openthree" / "limits.json").write_text(
        '{"max_iterations": 3}', encoding="utf-8")

    ws, reg, _ = make_kernel([p])
    blob = build_static_block(ws, reg, model="m") + build_context_block(ws)
    for secret in ("SECRET_MEMORY", "SECRET_PROMPT", "SECRET_PLAN", "SECRET_MODEL"):
        assert secret not in blob, f"profile 里的 {secret} 混进了上下文"
    assert ws.read_memory() == [], "记忆不合并 —— 别人的经历不是你的经历"
    # config 也不兜底：`load_config` 只读项目与用户层，profile 那份不在它的分层里
    assert "SECRET_MODEL" not in load_config(root).model


# ---------------------------------------------------------------- 7. 写到 profile
def test_write_function_to_profile(make_kernel, proj):
    root, base = proj
    p = base / "base"
    ws, reg, _ = make_kernel([p])

    out = write_function("hello", _tool("hello", "hi"), target=str(p))
    target_file = _fn(p, "hello.py")
    assert "已写入" in out, out
    assert target_file.is_file(), out
    assert not _fn(root, "hello.py").exists(), "默认不许写到项目里"
    assert reg.get("hello") is None, "没 reload 就不该生效"

    reg.reload()
    assert reg.get("hello") is not None
    assert reg.get("hello").source_distance == 1


# ---------------------------------------------------------------- 8. 未挂载就拒写
def test_write_function_rejects_unmounted_target(make_kernel, proj):
    root, base = proj
    unmounted = base / "team"        # 目录在，但没被 -profile 点名
    ws, reg, _ = make_kernel([base / "base"])

    out = write_function("hello", _tool("hello", "hi"), target=str(unmounted))
    assert out.startswith("[错误]"), out
    assert str(base / "base") in out, "要告诉人可写的位置有哪些（列出已挂载的那些）"
    assert not _fn(unmounted, "hello.py").exists(), "报错就不许落盘"


# ---------------------------------------------------------------- 9. 影子要说清楚
def test_shadow_write_says_it_will_not_take_effect(make_kernel, proj):
    root, base = proj
    p = base / "base"
    _fn(root, "hello.py").write_text(_tool("hello", "from-project"), encoding="utf-8")
    ws, reg, _ = make_kernel([p])

    out = write_function("hello", _tool("hello", "from-profile"), target=str(p))
    assert "不会" in out and "生效" in out, out
    assert "hello" in out and "hello.py" in out, "要指出是谁盖住了它"
    assert _fn(p, "hello.py").is_file(), "影子写入本身不拒绝 —— 只是要说清楚"


# ---------------------------------------------------------------- 10. 校验早于落盘
def test_bad_profile_exits_2_without_creating_workspace(tmp_path, capsys):
    proj = tmp_path / "brand_new"
    missing = tmp_path / "no_such_profile"

    code = cli.main(["--dir", str(proj), "-profile", str(missing), "--init"])

    assert code == 2
    assert not (proj / ".openthree").exists(), "校验失败时不许顺手建出一个工作区"
    assert "不是一个工作区" in capsys.readouterr().err


def test_profile_is_rejected_by_upgrade_and_check(tmp_path, capsys):
    """`-profile` 与 `-u` / `--check` 互斥。

    那两个模式扫的是「本项目对本机内核」的漂移，profile 不是本项目的一部分；
    组合起来只会得到没人定义过的语义，所以在起 Runtime 之前就该拒掉。
    """
    proj = tmp_path / "proj"
    prof = tmp_path / "prof"
    (prof / ".openthree" / "functions").mkdir(parents=True)

    for extra in (["--check"], []):
        code = cli.main(["--dir", str(proj), "-profile", str(prof), "-u", *extra])
        assert code == 2, (extra, code)
        assert "不接受 -profile" in capsys.readouterr().err


# ---------------------------------------------------------------- 11. 指到自己
def test_profile_pointing_at_project_is_deduped(make_kernel, proj):
    root, base = proj
    _fn(root, "hello.py").write_text(_tool("hello", "hi"), encoding="utf-8")
    ws, reg, _ = make_kernel([root, root])        # 重复点名自己

    srcs = ws.function_sources(include_missing=True)
    own = [s for s in srcs if s.path == ws.functions_dir]
    assert len(own) == 1, "同一个目录被点名两次只该出现一次"
    assert own[0].distance == 0, "project 恒 0"
    assert reg.get("hello").source_distance == 0


# ---------------------------------------------------------------- 12. 参数拼装
def test_collect_profiles_supports_comma_and_repeat():
    args = argparse.Namespace(profile=["a,b", " c ", "a"])
    assert cli._collect_profiles(args) == ["a", "b", "c", "a"]


# ---------------------------------------------------------------- 13. 术语归零
def test_no_ancestor_terminology_left():
    """源码层不许再出现 ancestor / 祖先：「逐级向上」那套已被 profile 取代。

    这条断言是「删干净」的可执行判据 —— 只查 `openthree/` 的源码，不查文档与
    测试里的历史说明（那些要能被讲清楚）。
    """
    pkg = Path(__file__).resolve().parents[1] / "openthree"
    hits = []
    for f in pkg.rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        for word in ("ancestor", "祖先"):
            if word in text:
                hits.append(f"{f.relative_to(pkg)}: {word}")
    assert not hits, hits
