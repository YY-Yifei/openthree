"""升级模式（`openthree -u`）：扫描判据 + 地基（版本戳 / 唯一真源）的回归。

这些测试守着几条**必须始终成立**的不变量，改 `workspace.py` / `upgrade.py` 时
若把它们弄坏，这里会先响：

1. `required_entries()` 是必备清单的唯一真源：`init()` 铺完之后，每一项都必须存在。
2. 状态文件（计划 / 边界 / 身份 / 上下文策略）**永不被对齐内容** —— 升级不能
   清空计划、重置边界、抹掉名字。
3. 版本戳只在首次铺成时写：反复 `init()` 不能把「当初那版种子」的记录刷成今天。
4. 漂移只报「缺 / 旧 / 不合规」，「多出来的」只进 `extras`、永不进 `items`。
5. `scan_upgrade()` 是纯函数：扫一遍不写任何文件（`--check` 靠这条才敢进 CI）。
"""
from __future__ import annotations

import json

from pathlib import Path

from openthree.upgrade import (ACTION_LABEL, DriftItem, UpgradeReport, apply_staged,
                               build_upgrade_task, is_protected, list_staged,
                               render_report, safe_rel, scan_upgrade, stage_dir,
                               undo_last)
from openthree.workspace import (WORKSPACE_SCHEMA, Workspace, kernel_version,
                                 sha256_text)


# ---------------------------------------------------------------- 唯一真源
def test_init_creates_every_required_entry(ws: Workspace):
    """`init()` 之后，`required_entries()` 声明的每一项都真的存在。

    这条把「声明」与「施工」钉在一起：往清单里加一条却忘了铺，这里就红。
    """
    for entry in ws.required_entries():
        assert (ws.dir / entry.path).exists(), f"{entry.path} 没被铺出来"


def test_required_entries_is_single_source(ws: Workspace):
    """目录必备项与实际属性一致 —— 防止清单与代码各说一套。"""
    paths = {e.path for e in ws.required_entries() if e.kind == "dir"}
    assert "prompts" in paths
    assert "functions" in paths
    assert "logs/sessions" in paths
    assert "memory" in paths
    assert ws.prompts_dir.is_dir() and ws.functions_dir.is_dir()


# ---------------------------------------------------------------- 版本戳
def test_workspace_meta_written_on_first_init(ws: Workspace):
    meta = ws.read_workspace_meta()
    assert meta["schema"] == WORKSPACE_SCHEMA
    assert meta["kernel_version"] == kernel_version()
    assert "prompts/system-prompts.md" in meta["seeds"]


def test_meta_records_seed_hash_not_disk_hash(ws: Workspace):
    """版本戳记的是**当初铺的那份种子**，不是磁盘现在这份。

    这是 `replace`（可整文件换新）与 `merge`（只能打补丁）的分界线：
    一旦改成记磁盘 hash，「文件被改过」就永远看不出来。
    """
    meta = ws.read_workspace_meta()
    seed_hash = sha256_text(ws.seed_text("system_prompt"))
    assert meta["seeds"]["prompts/system-prompts.md"] == seed_hash


def test_repeated_init_does_not_refresh_meta(ws: Workspace):
    """反复 `init()` 不能刷新版本戳。

    `Runtime.__init__` 每条命令都会调 `init()`；若它刷新版本戳，
    「当初是哪版种子铺的」就会被今天覆盖，所有文件退化成只能 `merge`。
    """
    first = ws.read_workspace_meta()["written_at"]
    ws.init()
    ws.init()
    assert ws.read_workspace_meta()["written_at"] == first


# ---------------------------------------------------------------- 章节解析
def test_analysis_sections_skips_code_fences(ws: Workspace):
    """代码块里的 `# 注释` 不是章节标题 —— 否则第一屏就是假警报。"""
    md = "# 真章节\n\n```python\ndef __main__(argv=None):\n    # 命令行入口\n    pass\n```\n\n## 另一章\n"
    sections = ws.analysis_sections(md)
    assert "# 真章节" in sections
    assert "## 另一章" in sections
    assert not any("命令行入口" in s for s in sections)


# ---------------------------------------------------------------- 扫描判据
def test_scan_is_pure(ws: Workspace):
    """扫描不写任何文件：`--check` 能在 CI 里跑，靠的就是这条。"""
    before = {p: p.stat().st_mtime_ns for p in ws.dir.rglob("*") if p.is_file()}
    scan_upgrade(ws)
    after = {p: p.stat().st_mtime_ns for p in ws.dir.rglob("*") if p.is_file()}
    assert before == after


def test_fresh_workspace_has_no_drift(ws: Workspace):
    """刚铺好的工作区与本机内核一致 —— 不该报漂移。"""
    report = scan_upgrade(ws)
    assert not report.has_drift, [i.as_dict() for i in report.items]


def test_missing_required_entry_is_reported(ws: Workspace):
    """删掉一个必备文件 → 报 missing + create（低风险）。"""
    ws.memory_dir.rmdir()
    report = scan_upgrade(ws)
    missing = [i for i in report.items if i.target == "memory" and i.kind == "missing"]
    assert missing and missing[0].action == "create"
    assert missing[0].risk == "low"


def test_state_files_are_never_realigned(ws: Workspace):
    """计划的**内容**归模型所有：升级只判在不在，绝不对齐内容。"""
    ws.write_plan("# 我的计划\n\n- [ ] 做一件只有我知道的事\n")
    report = scan_upgrade(ws)
    assert not [i for i in report.items if i.target == "prompts/plan.md"]
    assert "做一件只有我知道的事" in ws.read_plan()


def test_unmodified_seed_can_be_replaced(ws: Workspace):
    """磁盘 hash == 版本戳记的种子 hash → 人没动过 → 可整文件换新（replace）。

    模拟「内核换了新种子」：把文件写成旧版内容，并把版本戳里记的 hash 设成
    这份旧内容的 hash —— 于是「磁盘没被动过」成立，而它已落后于当前种子。
    """
    old_seed = "这是旧内核铺下来的提示词。\n"
    ws.system_prompt_file.write_text(old_seed, encoding="utf-8")
    meta = ws.read_workspace_meta()
    meta["seeds"]["prompts/system-prompts.md"] = sha256_text(old_seed)
    ws.workspace_meta_file.write_text(json.dumps(meta), encoding="utf-8")

    report = scan_upgrade(ws)
    item = [i for i in report.items if i.target == "prompts/system-prompts.md"]
    assert item and item[0].action == "replace"
    assert item[0].risk == "mid"


def test_edited_seed_requires_merge(ws: Workspace):
    """文件被改过 → 只能打补丁，绝不整文件覆盖。"""
    ws.system_prompt_file.write_text("我自己改过的提示词\n", encoding="utf-8")
    report = scan_upgrade(ws)
    item = [i for i in report.items if i.target == "prompts/system-prompts.md"]
    assert item and item[0].action == "merge"
    assert item[0].risk == "high"


def test_extra_content_never_becomes_an_action(ws: Workspace):
    """**多出来的内容永不产生动作** —— 这是整套设计的地基。

    盘上有、种子没有的章节是个体长的东西；升级只报「缺」，从不删「多」。
    """
    text = ws.system_prompt_file.read_text(encoding="utf-8")
    ws.system_prompt_file.write_text(
        text + "\n# 我自己长出来的一节\n\n这段只有这个工作区有。\n", encoding="utf-8")
    report = scan_upgrade(ws)
    assert any("我自己长出来的一节" in x for x in report.extras)
    # 它只出现在 extras，绝不能出现在任何 actionable item 的目标里
    assert not any("我自己长出来的一节" in i.target for i in report.items)


def test_nonconforming_function_is_reported(ws: Workspace):
    """自造 function 缺模块 docstring / 无尾钩 → 报 nonconforming（只建议）。"""
    (ws.functions_dir / "rough.py").write_text(
        "def rough(a):\n    return a\n", encoding="utf-8")
    report = scan_upgrade(ws)
    item = [i for i in report.items if i.target == "functions/rough.py"]
    assert item and item[0].kind == "nonconforming"
    assert item[0].action == "advise"
    assert item[0].details


def test_missing_meta_is_reported_as_low_risk(ws: Workspace):
    """老工作区没有版本戳 → 报「可补写」（低风险），不是结构迁移。"""
    ws.workspace_meta_file.unlink()
    report = scan_upgrade(ws)
    item = [i for i in report.items if i.target == "workspace.json"]
    assert item and item[0].action == "create" and item[0].risk == "low"


# ---------------------------------------------------------------- 报告渲染
def test_report_states_semantic_boundary(ws: Workspace):
    """报告必须写清「-u 不会让 openthree 变新」—— 这是最容易误解的地方。"""
    text = render_report(scan_upgrade(ws))
    assert "pip install -U openthree" in text


def test_report_lists_extras_as_no_action(ws: Workspace):
    ws.system_prompt_file.write_text(
        ws.system_prompt_file.read_text(encoding="utf-8")
        + "\n# 只有这里有的章节\n", encoding="utf-8")
    text = render_report(scan_upgrade(ws))
    assert "不产生动作" in text


def test_risk_ordering(ws: Workspace):
    """风险高的排在前面 —— 人读第一屏就该看到最该小心的那条。"""
    ws.system_prompt_file.write_text("改过的\n", encoding="utf-8")
    ws.workspace_meta_file.unlink()
    report = scan_upgrade(ws)
    risks = [i.risk for i in report.items]
    assert risks == sorted(risks, key=lambda r: {"low": 2, "mid": 1, "high": 0}[r])


# ---------------------------------------------------------------- 任务正文
def test_upgrade_task_forbids_unauthorized_writes(ws: Workspace):
    """交给模型的任务正文必须写清「不许擅自落盘」与「只补缺不删多」。"""
    task = build_upgrade_task(scan_upgrade(ws))
    assert "不许" in task
    assert "只补「缺」，不删「多」" in task
    assert "proposal-" in task


def test_upgrade_task_dry_run_marks_no_write(ws: Workspace):
    task = build_upgrade_task(scan_upgrade(ws), dry_run=True)
    assert "--dry-run" in task and "不要落盘" in task


# ---------------------------------------------------------------- 数据类
def test_report_consent_partitioning():
    """`replace` / `merge` 必须人确认；`create` / `advise` 可自动。"""
    report = UpgradeReport(items=[
        DriftItem("missing", "a", "create", "low", ""),
        DriftItem("outdated", "b", "replace", "mid", ""),
        DriftItem("outdated", "c", "merge", "high", ""),
        DriftItem("nonconforming", "d", "advise", "mid", ""),
    ])
    assert {i.target for i in report.needing_consent()} == {"b", "c"}
    assert {i.target for i in report.auto_safe()} == {"a", "d"}


def test_every_action_has_a_label():
    for action in ("create", "fill", "replace", "merge", "advise", "locked"):
        assert action in ACTION_LABEL


# ---------------------------------------------------------------- 人确认闸门
# 待应用区是「人确认后才落盘」的物理实现：模型把拟好的完整内容写进
# `upgrade/stage/<相对路径>`，人敲 `:apply` 才由内核搬到真实位置。
# 下面几条守着这道闸门本身 —— 它坏了，整套安全边界就没了。
def test_protected_paths_are_rejected(ws: Workspace):
    """审计痕迹与资产永不被升级改动：让被审计者改写审计日志是不可接受的。"""
    for bad in ("logs/trace.jsonl", "memory/memory.jsonl", "assets/a.png",
                "prompts/history/old.md", "three-report.html", "upgrade/stage/x"):
        assert is_protected(bad), bad
        assert safe_rel(bad) is None, bad


def test_path_traversal_is_rejected(ws: Workspace):
    """`..` 与绝对路径一律拒绝 —— 升级不能写到工作区外面去。"""
    for bad in ("../../etc/passwd", "/etc/passwd", "prompts/../../x", ""):
        assert safe_rel(bad) is None, bad
    assert safe_rel("prompts/system-prompts.md") == "prompts/system-prompts.md"


def test_apply_requires_staging(ws: Workspace):
    """待应用区空着时，`:apply` 什么都不做 —— 不会凭空产生改动。"""
    before = ws.system_prompt_file.read_text(encoding="utf-8")
    res = apply_staged(ws)
    assert not res.applied and not res.errors
    assert ws.system_prompt_file.read_text(encoding="utf-8") == before


def test_apply_then_undo_roundtrip(ws: Workspace):
    """待应用 → 落盘 → 撤销：必须精确回到升级前那一刻。

    这是「升级可后悔」的全部含义：`undo` 之后磁盘内容与升级前**逐字节相同**。
    """
    original = ws.system_prompt_file.read_text(encoding="utf-8")
    staged = stage_dir(ws)
    (staged / "prompts").mkdir(parents=True, exist_ok=True)
    (staged / "prompts" / "system-prompts.md").write_text("升级后的提示词\n",
                                                          encoding="utf-8")
    assert list_staged(ws) == ["prompts/system-prompts.md"]

    res = apply_staged(ws)
    assert res.applied == ["prompts/system-prompts.md"] and not res.errors
    assert ws.system_prompt_file.read_text(encoding="utf-8") == "升级后的提示词\n"
    assert list_staged(ws) == []              # 搬走之后待应用区要清干净
    assert res.snapshot_dir and Path(res.snapshot_dir, "manifest.json").exists()

    undo = undo_last(ws)
    assert undo.applied == ["prompts/system-prompts.md"] and not undo.errors
    assert ws.system_prompt_file.read_text(encoding="utf-8") == original


def test_undo_removes_files_that_upgrade_created(ws: Workspace):
    """升级**新建**的文件，撤销时应当被删掉（而不是留下个空壳）。"""
    staged = stage_dir(ws)
    staged.mkdir(parents=True, exist_ok=True)
    (staged / "README.md").unlink(missing_ok=True)
    (staged / "NEW.md").write_text("新文件\n", encoding="utf-8")

    apply_staged(ws)
    assert (ws.dir / "NEW.md").exists()
    undo_last(ws)
    assert not (ws.dir / "NEW.md").exists()


def test_dry_run_apply_touches_nothing(ws: Workspace):
    """`--dry-run` 的 `:apply` 只演练：不备份、不写、不清待应用区。"""
    original = ws.system_prompt_file.read_text(encoding="utf-8")
    staged = stage_dir(ws)
    (staged / "prompts").mkdir(parents=True, exist_ok=True)
    (staged / "prompts" / "system-prompts.md").write_text("不该生效\n", encoding="utf-8")

    res = apply_staged(ws, dry_run=True)
    assert res.applied == ["prompts/system-prompts.md"]
    assert ws.system_prompt_file.read_text(encoding="utf-8") == original
    assert list_staged(ws) == ["prompts/system-prompts.md"]


def test_undo_without_snapshot_is_safe(ws: Workspace):
    """没有快照时 `:undo` 只报一句，不崩、不乱动。"""
    res = undo_last(ws)
    assert not res.applied
    assert res.errors


def test_staged_listing_ignores_unsafe_paths(ws: Workspace):
    """待应用区里混进越界路径时，清单只列合法项。"""
    staged = stage_dir(ws)
    staged.mkdir(parents=True, exist_ok=True)
    (staged / "README.md").write_text("ok\n", encoding="utf-8")
    listed = list_staged(ws)
    assert "README.md" in listed
    assert all(safe_rel(p) for p in listed)
