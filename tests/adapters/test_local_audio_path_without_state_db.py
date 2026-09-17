"""Regression for the trunk red after PR #2930 (issue #2917).

``_resolve_local_audio_path`` started opening ``config.STATE_DB`` for every
listed track, so any route that lists tracks answered 500 STATE_DB_UNAVAILABLE
on a checkout with no ``data/state/state.db`` (fresh worktrees, CI runners).
The sibling ``bulk_rb_meta`` already reads a missing state db as "no rows";
the artwork tri-state must do the same and answer False, never raise.

- [if] config.STATE_DB does not exist [then] local_artwork_available answers False [⛔️ if it raises]
- [if] config.STATE_DB is absent [then] _resolve_local_audio_path answers None [⛔️ if it raises]
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.adapters.rekordbox import paths as rb_paths


@pytest.fixture
def missing_state_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    absent = tmp_path / "state" / "state.db"
    assert not absent.exists()
    monkeypatch.setattr(rb_paths.config, "STATE_DB", absent)
    return absent


def test_artwork_available_is_false_without_a_state_db(missing_state_db: Path) -> None:
    assert rb_paths.local_artwork_available("0" * 40) is False


def test_resolve_local_audio_path_is_none_without_a_state_db(missing_state_db: Path) -> None:
    assert rb_paths._resolve_local_audio_path("0" * 40) is None
