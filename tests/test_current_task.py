"""『读取用户输入』读到的是干净原文，绝不是文件的存储格式 / 别的输入。

被保护的不变量（12 个 case A–L 实测得出）：
  * 写走 `ws.submit`（干净原文 + 固定头尾），读**不许**走 `ws.read_user_prompt`
    （那是**整份 markdown 文件**）—— 口径必须一致。
  * `current-task.json` 是**结构化真源**（机器读），`user-prompts.md` 只是**渲染层**；
  * `_run_round` 把 `user_input=None` 的两种语义拆开（显式沿用 vs 本轮无新输入）；
  * `has_task` / `submit(append)` 用结构化判据，不靠「等待用户输入」这四个字；
  * 多轮任务同一用户输入只入档一次。
"""
from __future__ import annotations

import json

import pytest

from openthree.runtime import Runtime
from openthree.workspace import Workspace

PNG_BYTES = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 40)


@pytest.fixture
def png(tmp_path):
    p = tmp_path / "pic.png"
    p.write_bytes(PNG_BYTES)
    return p


# ---------------------------------------------------------------- 结构化真源
def test_submit_writes_clean_text_to_structured_source(ws: Workspace):
    ws.submit("把 README 改一下")
    assert ws.current_task_text() == "把 README 改一下"        # 干净原文
    assert ws.read_current_task()["text"] == "把 README 改一下"
    # 渲染层仍带 markdown 头尾（给人看），但**不再被当作输入**
    md = ws.read_user_prompt()
    assert md.startswith("# 当前任务") and "_提交于" in md


def test_current_task_text_never_leaks_markdown_scaffold(ws: Workspace):
    ws.submit("任务正文里有 --- 与 # 号也无妨")
    text = ws.current_task_text()
    assert "# 当前任务" not in text
    assert "_提交于" not in text
    assert text == "任务正文里有 --- 与 # 号也无妨"


def test_read_user_prompt_is_render_layer_not_input(ws: Workspace):
    """两个口径职责分明：read_user_prompt = 渲染层；current_task_text = 输入。"""
    ws.submit("干活")
    assert ws.read_user_prompt().strip() != ws.current_task_text()   # 渲染层 ≠ 输入
    assert ws.read_user_prompt().startswith("# 当前任务")
    assert ws.current_task_text() == "干活"


def test_current_task_roundtrips_images_in_structured_source(ws, png):
    ws.submit("看图", images=[str(png)])
    assert ws.current_task_images() == [str(png)]
    assert ws.current_task_text() == "看图"                    # 正文不含图片标记


def test_seed_workspace_has_no_task(ws: Workspace):
    assert ws.has_task() is False
    assert ws.current_task_text() == ""
    assert ws.current_task_images() == []


# ---------------------------------------------------------------- C：结构化判据
def test_has_task_not_fooled_by_literal_phrase(ws: Workspace):
    """任务正文**含**『等待用户输入』四个字时仍应判为有任务 —— 判据是结构，不是字面。"""
    ws.submit("请处理『等待用户输入』这个字样，它是正文的一部分")
    assert ws.has_task() is True


def test_submit_append_keeps_old_task_with_literal_phrase(ws: Workspace):
    """append 时不能因为旧任务含那四个字就把它整条丢弃。"""
    ws.submit("旧任务，正文含『等待用户输入』四个字")
    ws.submit("新任务", append=True)
    md = ws.read_user_prompt()
    assert "旧任务" in md and "新任务" in md          # 渲染层保留历史
    assert ws.current_task_text() == "新任务"          # 真源只有当前任务


def test_submit_append_drops_seed_placeholder(ws: Workspace):
    """appending 到种子占位符上，不该把「（等待用户输入……）」当历史任务。"""
    ws.submit("第一件事", append=True)
    assert "等待用户输入" not in ws.read_user_prompt()
    assert ws.current_task_text() == "第一件事"
    assert ws.has_task() is True


# ---------------------------------------------------------------- A1/B：不读渲染层
class _RecordingLLM:
    """记下每次发给模型的 messages，然后 finish。"""

    def __init__(self, cfg, answer="收到"):
        self.config = cfg
        self.answer = answer
        self.calls: list = []

    def chat(self, messages, tools=None, tool_choice="auto", **kwargs):
        self.calls.append(messages)
        msg = {"role": "assistant", "content": "",
               "tool_calls": [{"id": "c1", "type": "function",
                               "function": {"name": "finish",
                                            "arguments": json.dumps({"answer": self.answer})}}]}
        return {"message": msg, "usage": {}, "elapsed_ms": 1.0,
                "raw_request": {"model": "stub", "messages": messages},
                "raw_response": {"choices": [{"message": msg}]}, "error": None}


def _runtime(tmp_path, **kw) -> Runtime:
    rt = Runtime(tmp_path, quiet=True, heartbeat=0, **kw)
    rt.config.api_key = rt.config.base_url = "stub"
    rt.config.model = "stub-model"
    rt.llm = _RecordingLLM(rt.config)
    return rt


def _task_in_system(rt: Runtime, idx: int = -1) -> str:
    for m in rt.llm.calls[idx]:
        if m.get("role") == "system" and isinstance(m.get("content"), str):
            marker = "# 当前任务（三）"
            if marker in m["content"]:
                return m["content"].split(marker, 1)[1].strip()
    return ""


def test_run_none_reuses_clean_text_not_markdown(tmp_path):
    """`run(None)` 沿用任务时，读的是**干净原文**，不是整份 markdown。"""
    rt = _runtime(tmp_path)
    rt.run("上一次的任务：把 README 改一下")
    rt.run(None)
    task = _task_in_system(rt)
    assert task == "上一次的任务：把 README 改一下"        # 一字不差，无头无尾
    assert "# 当前任务" not in task
    assert "_提交于" not in task


def test_run_none_on_unfinished_round_does_not_reseed(tmp_path):
    """多轮任务续轮时，同一用户输入只播种一次、只入档一次。"""
    import json as _json

    class _DripLLM:
        def __init__(self, cfg):
            self.config = cfg
            self.n = 0
            self.calls = []

        def chat(self, messages, tools=None, tool_choice="auto", **kw):
            self.calls.append(messages)
            self.n += 1
            msg = {"role": "assistant", "content": "",
                   "tool_calls": [{"id": f"c{self.n}", "type": "function",
                                   "function": {"name": "get_time", "arguments": "{}"}}]}
            return {"message": msg, "usage": {}, "elapsed_ms": 0.0,
                    "raw_request": {}, "raw_response": {"choices": [{"message": msg}]},
                    "error": None}

    rt = _runtime(tmp_path, max_iterations=1)
    rt.llm = _DripLLM(rt.config)
    doc = rt.run("唯一的一次任务", rounds=3)
    assert doc["rounds_run"] == 3
    # 同一用户输入只在档案里出现一次（续轮记空 user + 结论）
    turns = rt.ws.read_turns(-1)
    assert [t["user"] for t in turns] == ["唯一的一次任务", "", ""]
    assert len(turns) == doc["rounds_run"]
    # 每一轮的「当前任务」都是干净原文（无 markdown 头）
    for i in range(doc["rounds_run"]):
        assert _task_in_system(rt, i) == "唯一的一次任务"


def test_run_none_images_does_not_leak_seed_placeholder(tmp_path, png):
    """全新工作区只给图：模型不该把「（等待用户输入……）」当任务。"""
    rt = _runtime(tmp_path)
    rt.run(None, images=[str(png)])
    task = _task_in_system(rt)
    assert "等待用户输入" not in task
    assert task == "（空）"                       # 没有任务正文，段头之外无内容
    blob = json.dumps(rt.llm.calls[-1], ensure_ascii=False)
    assert "图片输入" in blob                     # 图片作为正文注入


def test_resume_reads_clean_transcript(tmp_path):
    """多轮任务写出的档案是干净的：只有用户原文，没有 markdown 存储格式。"""
    rt = _runtime(tmp_path)
    rt.run("第一件事")
    rt.run("第二件事")
    turns = rt.ws.read_turns(-1)
    assert [t["user"] for t in turns] == ["第一件事", "第二件事"]
    assert all("# 当前任务" not in t["user"] for t in turns)


# ---------------------------------------------------------------- 兼容：旧工作区迁移
def test_legacy_workspace_migrates_from_markdown(tmp_path):
    """旧工作区只有 user-prompts.md：init() 一次性迁进结构化真源。"""
    prompts = tmp_path / ".openthree" / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "user-prompts.md").write_text(
        "# 当前任务\n\n旧工作区的任务：修个东西\n\n_提交于 2026-01-01T00:00:00_\n",
        encoding="utf-8")
    ws = Workspace(tmp_path).init()
    assert ws.current_task_text() == "旧工作区的任务：修个东西"
    assert ws.has_task() is True


def test_legacy_append_block_takes_latest_task(tmp_path):
    """旧工作区的累积块：迁移取**最后一块**（append 语义下最新的才是当前任务）。"""
    prompts = tmp_path / ".openthree" / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "user-prompts.md").write_text(
        "# 当前任务\n\n旧任务一\n\n_提交于 2026-01-01T00:00:00_\n\n---\n\n"
        "# 当前任务\n\n旧任务二\n\n_提交于 2026-01-02T00:00:00_\n",
        encoding="utf-8")
    ws = Workspace(tmp_path).init()
    assert ws.current_task_text() == "旧任务二"


def test_write_user_prompt_syncs_structured_source(ws: Workspace):
    """模型用 write_user_prompt 手改任务时，真源也同步（下一步读到的是新正文）。"""
    ws.write_user_prompt("# 当前任务\n\n模型改过的新任务\n")
    assert ws.current_task_text() == "模型改过的新任务"
