"""Per-lane analyze-on-import backlog (issue #10259)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.analysis import backlog as backlog_mod
from apps.shared.state.db import open_rw as open_state_rw

_TS = "2026-09-01T00:00:00Z"
_ALL = frozenset(backlog_mod.AUTO_LANES)
_ANALYZER = frozenset({"grid", "key", "sections"})


@pytest.fixture
def state_db(tmp_path: Path) -> Path:
    path = tmp_path / "state" / "state.db"
    open_state_rw(path).close()
    return path


def _add_track(db: Path, sid: str, file_path: Path) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO tracks (stable_id, stable_id_tier, title, artists_json, "
        "file_path, created_at, updated_at, deleted_at) VALUES (?,?,?,?,?,?,?,?)",
        (sid, "inferred", f"title-{sid}", "[]", str(file_path), _TS, _TS, None),
    )
    conn.commit()
    conn.close()


def _scan(db: Path, *, lanes: frozenset[backlog_mod.AutoLane]) -> backlog_mod.Backlog:
    conn = sqlite3.connect(db)
    try:
        return backlog_mod.scan(conn, lanes=lanes)
    finally:
        conn.close()


def _audio(tmp_path: Path, name: str) -> Path:
    p = tmp_path / name
    p.write_bytes(b"\x00" * 4096)
    return p


@pytest.mark.requirement("ANALYSIS-11724")
def test_scan_lists_analyzer_lanes_without_vocals(state_db, tmp_path):
    """[if] vocals is off and one track pends [then] lanes name grid key sections only, [else stop]."""
    _add_track(state_db, "local001", _audio(tmp_path, "t.mp3"))
    result = _scan(state_db, lanes=_ANALYZER)
    assert result.lanes == ("grid", "key", "sections")
    assert "vocals" not in result.lanes
    assert result.pending_total == 1


@pytest.mark.requirement("ANALYSIS-11724")
def test_signature_changes_when_vocals_lane_turns_on(state_db, tmp_path):
    """[if] vocals is toggled on over the same queue [then] the signature changes, [else stop]."""
    _add_track(state_db, "local001", _audio(tmp_path, "t.mp3"))
    without = _scan(state_db, lanes=_ANALYZER)
    with_vocals = _scan(state_db, lanes=_ALL)
    assert without.signature != with_vocals.signature
    assert _scan(state_db, lanes=_ANALYZER).signature == without.signature


@pytest.mark.requirement("ANALYSIS-11724")
def test_every_lane_off_counts_lanes_off_not_pending(state_db, tmp_path):
    """[if] every lane is off [then] pending is empty and lanes_off holds the track, [else stop]."""
    _add_track(state_db, "local001", _audio(tmp_path, "t.mp3"))
    result = _scan(state_db, lanes=frozenset())
    assert result.pending_total == 0
    assert result.lanes_off == 1
    assert result.signature == ""
    assert (
        result.analyzed + result.unreachable + result.too_long + result.lanes_off + result.pending_total
        == result.unmapped
    )


@pytest.mark.requirement("ANALYSIS-11724")
def test_unknown_lane_raises(state_db):
    """[if] lanes names an unknown lane [then] ValueError is raised, [else stop]."""
    conn = sqlite3.connect(state_db)
    try:
        with pytest.raises(ValueError, match="unknown"):
            backlog_mod.scan(conn, lanes=frozenset({"bogus"}))  # type: ignore[arg-type]
    finally:
        conn.close()
