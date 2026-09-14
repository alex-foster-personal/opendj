"""Unit tests for CloudSync runtime defer gates (CLOUDSYNC-14 part 1)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from apps.shared.sync_runtime_gates import (
    DEFER_REASON_DECK_PLAYING,
    DEFER_REASON_GIG,
    any_deck_playing,
    refuse_sync_round,
)
from apps.sync_hub import status as sync_status

pytestmark = pytest.mark.requirement("CLOUDSYNC-14")


def _write_gig_prefs(data_dir: Path) -> None:
    prefs_dir = data_dir / "state"
    prefs_dir.mkdir(parents=True, exist_ok=True)
    (prefs_dir / "ui-prefs.json").write_text(
        json.dumps({"app_posture": "gig"}), encoding="utf-8"
    )


def test_refuse_sync_round_returns_gig_posture(tmp_path: Path) -> None:
    """[if] app_posture is gig [then] refuse_sync_round returns gig_posture."""
    _write_gig_prefs(tmp_path)
    assert refuse_sync_round(tmp_path, None) == DEFER_REASON_GIG


def test_refuse_sync_round_returns_deck_playing(tmp_path: Path) -> None:
    """[if] a deck is playing [then] refuse_sync_round returns deck_playing."""
    mirror = {"decks": {"1": {"playing": True}}}
    assert refuse_sync_round(tmp_path, mirror) == DEFER_REASON_DECK_PLAYING


def test_refuse_sync_round_allows_prep_with_idle_decks(tmp_path: Path) -> None:
    """[if] prep posture and idle decks [then] refuse_sync_round returns None."""
    assert refuse_sync_round(tmp_path, None) is None
    mirror = {"decks": {"1": {"playing": False}}}
    assert refuse_sync_round(tmp_path, mirror) is None


def test_refuse_sync_round_defaults_missing_prefs_to_prep(tmp_path: Path) -> None:
    """[if] ui-prefs is absent [then] posture defaults to prep and sync is allowed."""
    assert refuse_sync_round(tmp_path, None) is None


def test_refuse_sync_round_gig_wins_over_missing_mirror(tmp_path: Path) -> None:
    """[if] gig posture with no mirror [then] gig_posture is returned."""
    _write_gig_prefs(tmp_path)
    assert refuse_sync_round(tmp_path, None) == DEFER_REASON_GIG


def test_missing_mirror_does_not_refuse_for_deck_playing(tmp_path: Path) -> None:
    """[if] mirror is absent [then] deck-playing gate reads as not playing."""
    assert any_deck_playing(None) is False
    assert refuse_sync_round(tmp_path, None) is None


def test_refuse_sync_round_honors_force_override(tmp_path: Path) -> None:
    """[if] force is true [then] gig posture does not defer (part 2 forward-compat)."""
    _write_gig_prefs(tmp_path)
    assert refuse_sync_round(tmp_path, None, force=True) is None


def test_cli_sync_exits_3_when_gig_posture(tmp_path: Path) -> None:
    """[if] CLI sync runs under gig posture [then] exit code is 3 with gig_posture."""
    _write_gig_prefs(tmp_path)
    dead_hub = "http://127.0.0.1:9"
    result = subprocess.run(
        [sys.executable, "-m", "apps.sync_hub", "sync", "--data-dir", str(tmp_path), "--hub", dead_hub],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 3
    assert DEFER_REASON_GIG in result.stderr
    assert sync_status.read_results(tmp_path) == ()
