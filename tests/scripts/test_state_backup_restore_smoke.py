"""Hermetic tests for scripts/state_backup_restore_smoke.py (issue #12952).

[if] backup restore smoke regresses [then] acceptance tests fail, [else stop].
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from apps.shared import sqlite_verified_copy as svc
from scripts import state_backup_restore_smoke as smoke

pytestmark = pytest.mark.requirement("PLATFORM-14433")

FIXED_NOW = datetime(2026, 11, 13, 12, 0, 0, tzinfo=UTC)


def test_disposable_state_database_is_backed_up_and_restored(tmp_path: Path) -> None:
    """[if] a disposable state database is backed up and restored [then] row counts match, [else stop]."""
    data_dir = tmp_path / "data"
    smoke.seed_disposable_library(data_dir)
    backup_path = smoke.backup_disposable_library(data_dir, now=FIXED_NOW)
    backup_counts = svc.verify_backup(backup_path)
    assert backup_counts.get("tracks", 0) >= 1
    smoke.wipe_live_state_db(data_dir)
    live = smoke._state_path(data_dir)
    restored_counts = smoke.restore_full_state_db(live, backup_path)
    assert restored_counts == backup_counts


def test_restored_database_is_queried_through_real_state_layer(tmp_path: Path) -> None:
    """[if] the restored database is queried through the real state layer [then] the seed row reads, [else stop]."""
    data_dir = tmp_path / "data"
    smoke.seed_disposable_library(data_dir)
    backup_path = smoke.backup_disposable_library(data_dir, now=FIXED_NOW)
    smoke.wipe_live_state_db(data_dir)
    smoke.restore_full_state_db(smoke._state_path(data_dir), backup_path)
    assert smoke.read_seed_track(data_dir) == smoke.SEED_TITLE


def test_write_is_made_after_restoration(tmp_path: Path) -> None:
    """[if] a write is made after restoration [then] the new track persists, [else stop]."""
    data_dir = tmp_path / "data"
    report = smoke.run_drill(data_dir, now=FIXED_NOW)
    assert report.post_restore_stable_id == smoke.POST_RESTORE_STABLE_ID
    assert smoke.read_post_restore_track(data_dir) == smoke.POST_RESTORE_TITLE


def test_main_exits_zero_on_disposable_run(capsys: pytest.CaptureFixture[str]) -> None:
    """[if] the CLI runs on a temp data dir [then] it prints ok and exits 0, [else stop]."""
    assert smoke.main(["--json"]) == 0
    out = capsys.readouterr().out
    assert smoke.SEED_STABLE_ID.split("-")[0] in out or "backup_path" in out
