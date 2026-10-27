"""自省：智能体与人都要知道「我跑在哪套内核上、差什么」。

两条通道各管一位读者，互不重叠：

* `whoami`（智能体）—— 内核版本、结构 schema、工作区完整性，与名字同属「我是谁」。
  返回值不进提示词，所以这里能写全；进提示词的那一行保持简短。
* `--help` 的 epilog（人）—— 只说「怎么查」，不含此刻的状态。

被保护的不变量：

1. 老工作区（没有 `workspace.json`）调 `whoami` **不抛异常**，且不把缺失的
   schema 报成 0 —— 0 会被读成「结构停在第 0 版」，而真相是「这版内核还没管过它」。
2. `--help` 的输出**不随 cwd 变**。它要能进文档、能被 diff；一旦掺入运行期
   状态，文档就腐、`--help | diff` 就失效。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from openthree.builtins import whoami
from openthree.cli import build_parser
from openthree.kernel import KERNEL
from openthree.workspace import WORKSPACE_SCHEMA, Workspace, kernel_version

ROOT = Path(__file__).resolve().parents[1]


def _bind(ws: Workspace) -> None:
    KERNEL.workspace = ws


# ---------------------------------------------------------------- whoami
def test_whoami_reports_kernel_and_schema(ws: Workspace, make_agent):
    """新工作区：说得出内核版本、结构 schema、必备项齐备。"""
    make_agent()
    out = whoami()
    assert kernel_version() in out
    assert f"schema {WORKSPACE_SCHEMA}/{WORKSPACE_SCHEMA}" in out
    assert f"{len(ws.required_entries())}/{len(ws.required_entries())} 项齐备" in out


def test_whoami_degrades_without_workspace_meta(ws: Workspace, make_agent):
    """老工作区没有版本戳：如实说「无版本戳」，绝不把缺失报成 schema 0。"""
    make_agent()
    ws.workspace_meta_file.unlink()
    out = whoami()
    assert "无版本戳" in out
    assert "schema 0" not in out


def test_whoami_lists_missing_entries(ws: Workspace, make_agent):
    """缺文件时点名说缺什么 —— 这是「差什么」里 whoami 负责的那半句。"""
    make_agent()
    (ws.dir / "limits.json").unlink()
    out = whoami()
    assert "limits.json" in out
    assert "缺 1/" in out


def test_whoami_still_answers_identity(ws: Workspace, make_agent):
    """加了内核信息不挤掉原有身份信息：名字 / 格言 / 起名时间都还在。"""
    make_agent()
    ws.write_identity("小问", "先看再用")
    out = whoami()
    assert "小问" in out and "先看再用" in out
    assert "起名于：" in out and "改名" in out


def test_whoami_does_not_write(ws: Workspace, make_agent):
    """`whoami` 是只读的一次查询。"""
    make_agent()
    before = {p for p in ws.dir.rglob("*")}
    whoami()
    whoami()
    assert {p for p in ws.dir.rglob("*")} == before


# ---------------------------------------------------------------- --help
def test_help_mentions_the_three_self_inspect_surfaces():
    """人要知道该敲哪三条：我是谁 / 我差什么 / 版本几。"""
    text = build_parser().format_help()
    assert "--inspect" in text
    assert "-u --check" in text or "--check" in text
    assert "pip install -U" in text          # 语义边界必须在场


def test_help_is_cwd_independent(tmp_path: Path):
    """同一个内核，在两个无关目录里 `--help` 必须逐字节相同。"""
    outs = []
    for i in range(2):
        d = tmp_path / f"cwd{i}"
        d.mkdir()
        res = subprocess.run(
            [sys.executable, "-c",
             "from openthree.cli import _force_utf8, build_parser;"
             "_force_utf8(); print(build_parser().format_help(), end='')"],
            cwd=str(d), capture_output=True, timeout=180,
            env=dict(os.environ, PYTHONPATH=str(ROOT), PYTHONUTF8="1"),
        )
        assert res.returncode == 0, res.stderr.decode("utf-8", "replace")
        outs.append(res.stdout)
    assert outs[0] == outs[1]


def test_help_epilog_has_no_runtime_state():
    """`--help` 里不该出现「此刻」—— 它答怎么查，不答现在是什么。

    判据：epilog 里不出现版本号本身。版本是运行期事实，由 `-V` / `whoami` 回答。
    """
    from openthree.cli import _EPILOG

    assert kernel_version() not in _EPILOG
