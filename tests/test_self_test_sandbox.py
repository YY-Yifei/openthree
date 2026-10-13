"""self_test 沙箱目录的清理行为 —— 别让回退目录无声堆积。"""
from __future__ import annotations

from pathlib import Path

import self_test


def test_fresh_dir_clears_stale_fallback_dirs(tmp_path: Path) -> None:
    """起手应把上次回退遗留的 _sandbox-<ts> 兄弟目录清掉。"""
    base = tmp_path / "_sandbox"
    stale = tmp_path / "_sandbox-1234567890"
    (stale / "deep").mkdir(parents=True)
    (stale / "deep" / "x.txt").write_text("残留", encoding="utf-8")

    out = self_test._fresh_dir(base)

    assert out == base
    assert base.is_dir()
    assert not stale.exists()
    assert list(tmp_path.glob("_sandbox-*")) == []


def test_fresh_dir_recreates_existing_sandbox(tmp_path: Path) -> None:
    """已存在的沙箱应被清空重建，不残留旧内容。"""
    base = tmp_path / "_sandbox"
    (base / "junk").mkdir(parents=True)
    (base / "junk" / "old.txt").write_text("old", encoding="utf-8")

    out = self_test._fresh_dir(base)

    assert out == base
    assert base.is_dir()
    assert not (base / "junk").exists()
