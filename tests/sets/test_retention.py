"""Tests for :mod:`apps.sets.retention`."""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from apps.sets import retention


def _touch_with_age(path: Path, age_days: float) -> None:
    path.write_bytes(b"\x00" * 16)
    mtime = time.time() - age_days * 86400
    os.utime(path, (mtime, mtime))


@pytest.mark.requirement("SET-01")
def test_find_candidates_returns_only_old_mp3s(sets_root: Path):
    sess = sets_root / "s-1"
    sess.mkdir()
    old = sess / "audio_2025-12-01T00-00-00.mp3"
    fresh = sess / "audio_2026-04-17T21-00-00.mp3"
    timeline = sess / "timeline.jsonl"
    _touch_with_age(old, 120)
    _touch_with_age(fresh, 1)
    timeline.write_text("{}\n")

    candidates = retention.find_candidates(retention_days=90, root=sets_root)
    names = [c.path.name for c in candidates]
    assert old.name in names
    assert fresh.name not in names
    assert "timeline.jsonl" not in names


@pytest.mark.requirement("SET-01")
def test_prune_dry_run_does_not_delete(sets_root: Path):
    sess = sets_root / "s-1"
    sess.mkdir()
    old = sess / "audio_2025-12-01T00-00-00.mp3"
    _touch_with_age(old, 200)
    result = retention.prune(retention_days=90, dry_run=True, root=sets_root)
    assert [c.path for c in result] == [old]
    assert old.exists()


@pytest.mark.requirement("SET-01")
def test_prune_apply_deletes_old_segments_only(sets_root: Path):
    sess = sets_root / "s-1"
    sess.mkdir()
    old = sess / "audio_2025-12-01T00-00-00.mp3"
    timeline = sess / "timeline.jsonl"
    _touch_with_age(old, 200)
    timeline.write_text('{"x":1}\n')

    retention.prune(retention_days=90, dry_run=False, root=sets_root)
    assert not old.exists()
    assert timeline.exists(), "timeline.jsonl must survive retention pruning"


@pytest.mark.requirement("SET-01")
def test_find_candidates_handles_missing_root(tmp_path: Path):
    assert retention.find_candidates(root=tmp_path / "nope") == []
