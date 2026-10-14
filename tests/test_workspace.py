"""Workspace / Recorder：工作区自举、节流落盘、事件带 session、报告生成。"""
from __future__ import annotations

import json

from openthree.recorder import Recorder
from openthree.report import write_report
from openthree.workspace import Workspace


# ---------------------------------------------------------------- Workspace
def test_init_is_idempotent(tmp_path):
    ws = Workspace(tmp_path).init()
    first = ws.system_prompt_file.read_text(encoding="utf-8")
    ws.init()
    assert ws.system_prompt_file.read_text(encoding="utf-8") == first


def test_has_task_ignores_seed(ws):
    assert ws.has_task() is False
    ws.submit("干点活")
    assert ws.has_task() is True


def test_submit_can_append(ws):
    ws.submit("第一件事")
    ws.submit("第二件事", append=True)
    text = ws.read_user_prompt()
    assert "第一件事" in text and "第二件事" in text


def test_function_path_sanitizes(ws):
    assert ws.function_path("my tool/../x").name == "my_tool_x.py"


# ---------------------------------------------------------------- Recorder
def test_events_carry_session_id(ws):
    rec = Recorder(ws, user_input="t", model="m")
    for i in range(3):
        rec.record("note", summary=f"e{i}")
    rec.finalize(answer="done")
    lines = [json.loads(x) for x in
             (ws.events_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 3
    assert all(l["session"] == rec.session_id for l in lines)


def test_session_json_written_on_finalize(ws):
    rec = Recorder(ws, user_input="t", model="m")
    rec.begin_step()
    rec.record("note", summary="一")
    doc = rec.finalize(answer="答案")
    saved = json.loads(rec.session_file.read_text(encoding="utf-8"))
    assert saved["status"] == "done"
    assert saved["answer"] == "答案"
    assert doc["stats"]["events"] == 1


def test_stats_counts_kinds(ws):
    rec = Recorder(ws)
    rec.record("tool", summary="a", elapsed_ms=10)
    rec.record("llm", summary="b", elapsed_ms=100)
    rec.record("evolution", summary="c")
    stats = rec.stats()
    assert stats["tool_calls"] == 1
    assert stats["tool_time_ms"] == 10
    assert stats["evolutions"] == 1


def test_step_files_written(ws):
    rec = Recorder(ws)
    rec.begin_step()
    rec.record("note", summary="a")
    rec.begin_step()
    rec.record("note", summary="b")
    rec.finalize()
    # 逐步文件按会话分目录，防止后一场会话覆盖前一场
    assert len(list(rec.steps_dir.glob("step-*.json"))) == 2
    assert rec.steps_dir.parent.name == rec.session_id


def test_step_files_survive_across_sessions(ws):
    """回归：两场会话的 step/llm 原文必须各留各的，不能跨会话覆盖。"""
    first = Recorder(ws, user_input="t1", model="m")
    first.begin_step()
    first.record("note", summary="会话一")
    first.record_llm({"model": "m", "messages": []}, {"choices": []}, 1.0)
    first.finalize(answer="一")

    second = Recorder(ws, user_input="t2", model="m")   # 新会话，序号从 0001 重来
    second.begin_step()
    second.record("note", summary="会话二")
    second.record_llm({"model": "m", "messages": []}, {"choices": []}, 1.0)
    second.finalize(answer="二")

    assert len(list(ws.sessions_dir.glob("*/steps/step-0001.json"))) == 2
    assert len(list(ws.sessions_dir.glob("*/llm/llm-0001.json"))) == 2


def test_finalize_keeps_real_status(ws):
    """回归：真实终态必须落盘，不能被压成 running（否则事后分不清真终态与被 kill）。"""
    for status in ("done", "unfinished", "interrupted", "error"):
        ws2 = Workspace(ws.root / status)
        rec = Recorder(ws2, user_input="t", model="m")
        rec.record("note", summary="x")
        rec.finalize(answer="a", status=status)
        saved = json.loads(rec.session_file.read_text(encoding="utf-8"))
        assert saved["status"] == status
        assert saved["ended_at"]


# ---------------------------------------------------------------- 报告
def test_report_renders_without_crash(ws):
    rec = Recorder(ws, user_input="任务", model="m")
    rec.begin_step()
    rec.record("note", summary="思考", data={"thought": "<script>alert(1)</script>"})
    rec.record("tool", summary="跑命令", data={"name": "run_shell",
                                             "output": "</style><script>x</script>"})
    rec.finalize(answer="结论")
    path = write_report(ws)
    html = path.read_text(encoding="utf-8")
    assert path.exists() and "three-report" not in html.split("<title>")[0]
    # 关键：模型产出的任意文本必须被转义，不能原样注入
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_report_includes_evolution(ws):
    rec = Recorder(ws)
    rec.record("evolution", summary="写了个工具", data={"action": "write_function"})
    rec.finalize()
    html = write_report(ws).read_text(encoding="utf-8")
    assert "写了个工具" in html
