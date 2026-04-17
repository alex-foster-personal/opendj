"""suggest_next end-to-end tests (AI-01)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from apps.dj_copilot.session_context import PlayedTrack, SessionContext
from apps.dj_copilot.suggester import suggest_next
from apps.shared.harmonic import TrackFeature

pytestmark = pytest.mark.requirement("AI-01")


def _ctx(recent: list[PlayedTrack]) -> SessionContext:
    return SessionContext(
        recent=recent, source="manual", captured_at=datetime.now(timezone.utc)
    )


def _lib_small() -> list[TrackFeature]:
    return [
        TrackFeature("cur", "Cur", 120.0, "8A", 5),
        TrackFeature("t-hit", "Hit", 121.0, "8A", 5),
        TrackFeature("t-adj", "Adj", 121.0, "9A", 5),
        TrackFeature("t-far", "Far", 140.0, "12B", 2),
    ]


def test_empty_when_current_unknown(tmp_path) -> None:
    db = tmp_path / "s.db"
    conn = sqlite3.connect(str(db), isolation_level=None)
    try:
        out = suggest_next(
            conn=conn,
            current_stable_id="unknown",
            library=_lib_small(),
            top_n=5,
        )
        assert out == []
    finally:
        conn.close()


def test_end_to_end_local_ranks_hits_first(tmp_path) -> None:
    db = tmp_path / "s.db"
    conn = sqlite3.connect(str(db), isolation_level=None)
    try:
        out = suggest_next(
            conn=conn,
            current_stable_id="cur",
            library=_lib_small(),
            top_n=3,
        )
        ids = [s.stable_id for s in out]
        # "t-hit" (same key + close bpm + same energy) ranks first;
        # "t-far" is dropped by BPM window.
        assert ids[0] == "t-hit"
        assert "t-far" not in ids


    finally:
        conn.close()


def test_rationale_tags_populated(tmp_path) -> None:
    db = tmp_path / "s.db"
    conn = sqlite3.connect(str(db), isolation_level=None)
    try:
        out = suggest_next(
            conn=conn,
            current_stable_id="cur",
            library=_lib_small(),
            top_n=3,
        )
        top = out[0]
        assert top.rationale_tags  # non-empty
        assert "camelot" in top.rationale_numbers
    finally:
        conn.close()


def test_explain_stub_text(tmp_path) -> None:
    db = tmp_path / "s.db"
    conn = sqlite3.connect(str(db), isolation_level=None)
    try:
        out = suggest_next(
            conn=conn,
            current_stable_id="cur",
            library=_lib_small(),
            top_n=1,
            explain=True,
        )
        assert out[0].explain_text is not None
        assert len(out[0].explain_text) > 0
    finally:
        conn.close()


def test_explain_default_none(tmp_path) -> None:
    db = tmp_path / "s.db"
    conn = sqlite3.connect(str(db), isolation_level=None)
    try:
        out = suggest_next(
            conn=conn,
            current_stable_id="cur",
            library=_lib_small(),
            top_n=1,
        )
        assert out[0].explain_text is None
    finally:
        conn.close()


def test_deterministic(tmp_path) -> None:
    db = tmp_path / "s.db"
    conn = sqlite3.connect(str(db), isolation_level=None)
    try:
        r1 = suggest_next(
            conn=conn, current_stable_id="cur", library=_lib_small(), top_n=5
        )
        r2 = suggest_next(
            conn=conn, current_stable_id="cur", library=_lib_small(), top_n=5
        )
        assert [s.stable_id for s in r1] == [s.stable_id for s in r2]
    finally:
        conn.close()
