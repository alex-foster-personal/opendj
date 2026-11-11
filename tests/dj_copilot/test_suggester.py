"""suggest_next end-to-end tests (AI-01)."""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest

from apps.dj_copilot.session_context import PlayedTrack, SessionContext
from apps.dj_copilot.suggester import suggest_next
from apps.shared.harmonic import TrackFeature
from tests.dj_copilot.conftest import FIXED_NOW

pytestmark = pytest.mark.requirement("AI-01")


def _ctx(recent: list[PlayedTrack]) -> SessionContext:
    return SessionContext(recent=recent, source="manual", captured_at=FIXED_NOW)


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


def test_explain_text_uses_scored_terms(tmp_path) -> None:
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
        assert out[0].explain_text == ("8A to 8A, same key; 120 to 121 BPM (+0.8%); energy 5 to 5")
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
        r1 = suggest_next(conn=conn, current_stable_id="cur", library=_lib_small(), top_n=5)
        r2 = suggest_next(conn=conn, current_stable_id="cur", library=_lib_small(), top_n=5)
        assert [s.stable_id for s in r1] == [s.stable_id for s in r2]
    finally:
        conn.close()


@pytest.mark.requirement("SETPREP-12113")
def test_played_recently_tag_with_history_weight(tmp_path) -> None:
    """[if] history weight 0.10 and recent play [then ⛔️] played_recently tag, [else stop]."""
    played_at = FIXED_NOW - timedelta(days=3)
    lib = [
        TrackFeature("cur", "Cur", 120.0, "8A", 5),
        TrackFeature("t-hit", "Hit", 121.0, "8A", 5, last_played_at=played_at),
        TrackFeature("t-fresh", "Fresh", 121.0, "8A", 5),
    ]
    db = tmp_path / "s.db"
    conn = sqlite3.connect(str(db), isolation_level=None)
    try:
        out = suggest_next(
            conn=conn,
            current_stable_id="cur",
            library=lib,
            context=_ctx([]),
            top_n=5,
            history_weight=0.10,
        )
        recent_row = next(s for s in out if s.stable_id == "t-hit")
        assert recent_row.rationale_tags[-1] == "played_recently"
        assert recent_row.rationale_numbers["days_since_played"] == 3.0
        out_default = suggest_next(
            conn=conn,
            current_stable_id="cur",
            library=lib,
            context=_ctx([]),
            top_n=5,
        )
        row_default = next(s for s in out_default if s.stable_id == "t-hit")
        assert "played_recently" not in row_default.rationale_tags
        assert "days_since_played" not in row_default.rationale_numbers
    finally:
        conn.close()


@pytest.mark.requirement("SETPREP-12113")
def test_session_play_never_suggested(tmp_path) -> None:
    """[if] candidate equals a session play [then ⛔️] it is absent from results, [else stop]."""
    lib = [
        TrackFeature("cur", "Cur", 120.0, "8A", 5),
        TrackFeature("played-id", "A", 121.0, "8A", 5, title="Song"),
        TrackFeature("copy-id", "ARTIST", 121.0, "8A", 5, title="Song Name"),
    ]
    recent = [
        PlayedTrack(
            stable_id="played-id",
            artist="A",
            bpm=120.0,
            key_camelot="8A",
            energy=5,
            played_at=FIXED_NOW,
            title="Song",
        ),
        PlayedTrack(
            stable_id="p-copy",
            artist="Artist feat. X",
            bpm=120.0,
            key_camelot="8A",
            energy=5,
            played_at=FIXED_NOW,
            title="6 - Song Name",
        ),
    ]
    db = tmp_path / "s.db"
    conn = sqlite3.connect(str(db), isolation_level=None)
    try:
        out = suggest_next(
            conn=conn,
            current_stable_id="cur",
            library=lib,
            context=_ctx(recent),
            top_n=10,
        )
        ids = {s.stable_id for s in out}
        assert "played-id" not in ids
        assert "copy-id" not in ids
    finally:
        conn.close()
