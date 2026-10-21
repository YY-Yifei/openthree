"""身份：名字是智能体自己长出来的，不是内核常量。

核心主张：`.openthree/identity.json` 与 limits.json / context.json 同源 ——
磁盘即状态、每步重读；模型用 `set_identity` 写、`whoami` 读，
终端身份卡 / 处境注入 / 工作报告都读同一份真相。
"""
from __future__ import annotations

import json

from openthree.builtins import set_identity, whoami
from openthree.kernel import KERNEL
from openthree.report import build_html
from openthree.workspace import DEFAULT_AGENT_NAME, Workspace

from conftest import sent_block


def _bind(ws: Workspace) -> None:
    """把工作区挂进内核（内置工具的落盘路径依赖它）。"""
    KERNEL.workspace = ws


# ---------------------------------------------------------------- 种子 & 读写
def test_workspace_seeds_identity(ws: Workspace):
    assert ws.identity_file.exists()
    data = json.loads(ws.identity_file.read_text(encoding="utf-8"))
    assert set(data) == {"name", "motto", "born_at", "updated_at", "renames"}
    assert data["name"] is None                     # 最初没有名字
    assert data["renames"] == 0


def test_read_identity_tolerates_broken_file(ws: Workspace):
    ws.identity_file.write_text("{ 这不是 json", encoding="utf-8")
    identity = ws.read_identity()
    assert identity["name"] is None and identity["renames"] == 0


def test_write_identity_sets_born_at_and_renames(ws: Workspace):
    first = ws.write_identity("小问", "先看再用")
    assert first["name"] == "小问" and first["motto"] == "先看再用"
    assert first["born_at"] and first["renames"] == 0     # 首次起名不算改名
    born = first["born_at"]

    second = ws.write_identity("问二", "")
    assert second["name"] == "问二" and second["motto"] is None
    assert second["renames"] == 1                         # 改名 +1
    assert second["born_at"] == born                      # 出生时间不因改名而变
    assert ws.agent_name() == "问二"


# ---------------------------------------------------------------- set_identity
def test_set_identity_writes_and_records_evolution(ws: Workspace, make_agent):
    ag = make_agent()          # 挂了 KERNEL.recorder
    out = set_identity("小问", "落盘为证")
    assert "[ok]" in out and "小问" in out
    assert ws.read_identity()["name"] == "小问"

    evo = ws.logs_dir / "evolution.jsonl"
    assert evo.exists()
    lines = [json.loads(l) for l in evo.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert any(e["data"].get("action") == "set_identity" for e in lines)


def test_set_identity_rejects_bad_name(ws: Workspace, make_agent):
    make_agent()
    assert "[错误]" in set_identity("")
    assert "[错误]" in set_identity("  ")
    assert "[错误]" in set_identity("x" * 25)          # 超长
    assert "[错误]" in set_identity("坏\n名字")          # 控制字符
    assert ws.read_identity()["name"] is None          # 一律没落盘


def test_set_identity_is_idempotent(ws: Workspace, make_agent):
    make_agent()
    set_identity("小问", "格言")
    out = set_identity("小问", "格言")
    assert "名字没变" in out
    assert ws.read_identity()["renames"] == 0          # 同名不算一次改名


def test_whoami_reports_state(ws: Workspace, make_agent):
    make_agent()
    assert "还没有名字" in whoami()
    set_identity("小问", "落盘为证")
    out = whoami()
    assert "小问" in out and "落盘为证" in out


# ---------------------------------------------------------------- 处境注入
def test_prompt_injects_name(ws: Workspace, registry):
    _bind(ws)
    block = sent_block(ws, registry, model="m", iteration=1, max_iterations=200)
    assert "名字：（还没有）" in block                   # 未命名时如实说
    ws.write_identity("小问", "落盘为证")
    block = sent_block(ws, registry, model="m", iteration=1, max_iterations=200)
    assert "名字：小问 —— 落盘为证" in block


# ---------------------------------------------------------------- 展示层
def test_report_uses_agent_name(ws: Workspace):
    ws.write_identity("小问", "落盘为证")
    html = build_html(ws)
    assert "<h1>小问</h1>" in html
    assert "落盘为证" in html
    assert "三生万物" not in html and "道生一" not in html


def test_report_defaults_when_unnamed(ws: Workspace):
    html = build_html(ws)
    assert f"<h1>{DEFAULT_AGENT_NAME}（未命名）</h1>" in html


def test_runtime_name_flag_seeds_once(tmp_path):
    from openthree.runtime import Runtime

    rt = Runtime(tmp_path, name="小问")
    assert rt.agent_name() == "小问"
    assert rt.agent_label() == "小问"

    # 已经有名字 → 再给 --name 不覆盖（名字归智能体自己）
    rt2 = Runtime(tmp_path, name="别人")
    assert rt2.agent_name() == "小问"


def test_identity_card_shows_name_and_evolution(tmp_path):
    from openthree.runtime import Runtime

    rt = Runtime(tmp_path, name="小问")
    card = rt.identity_card()
    assert "小问" in card and "次演化" in card
    assert str(tmp_path) in card

def test_unnamed_card_is_honest(tmp_path):
    from openthree.runtime import Runtime

    rt = Runtime(tmp_path)
    assert rt.agent_label() == f"{DEFAULT_AGENT_NAME}（未命名）"
