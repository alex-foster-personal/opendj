"""suggest_next end-to-end tests (AI-01); find-similar modes (INTEL-12031b, INTEL-12031)."""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest

from apps.dj_copilot.session_context import PlayedTrack, SessionContext
from apps.dj_copilot.suggester import find_similar_tracks, suggest_next
from apps.genre_infer import findings, service, taxonomy
from apps.shared.harmonic import TrackFeature
from tests.dj_copilot.conftest import FIXED_NOW
from tests.genre_infer.pipeline_support import library

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


def _genre_conn(state) -> sqlite3.Connection:
    conn = sqlite3.connect(str(state))
    findings.ensure_schema(conn)
    return conn


@pytest.mark.requirement("INTEL-12031b")
def test_find_similar_generic_returns_genre_cosine_neighbours(tmp_path) -> None:
    """[if] Generic mode runs on a measured track [then] candidates match genre similar, [else stop]."""
    _data, state = library(tmp_path)
    conn = _genre_conn(state)
    base = {g: 0.0 for g in taxonomy.NAMES}
    findings.put_vector(conn, "t-fred", "jev", base | {"House": 0.8, "Deep House": 0.2})
    findings.put_vector(conn, "t-fred2", "jev", base | {"House": 0.7, "Deep House": 0.3})
    ranked = find_similar_tracks(conn, stable_id="t-fred", mode="generic", limit=20)
    expected = service.similar(conn, "t-fred", 20)
    assert ranked["mode"] == "generic"
    assert ranked["candidates"] == expected["similar"]


@pytest.mark.requirement("INTEL-12031b")
def test_find_similar_generic_errors_when_unmeasured(tmp_path) -> None:
    """[if] the query track has no genre vector [then] Generic raises unmeasured, [else stop]."""
    _data, state = library(tmp_path)
    conn = _genre_conn(state)
    with pytest.raises(KeyError, match="no jev genre vector"):
        find_similar_tracks(conn, stable_id="t-id", mode="generic", limit=20)


def _specific_lib() -> list[TrackFeature]:
    return [
        TrackFeature("cur", "A", 120.0, "8A", 5),
        TrackFeature("tight", "B", 121.0, "8A", 5),
        TrackFeature("distant", "C", 150.0, "12B", 2),
    ]


@pytest.mark.requirement("INTEL-12031")
def test_find_similar_specific_ranks_by_harmonic_score() -> None:
    """[if] Specific runs with bpm and key [then] rationale has bpm/key/energy scores, [else stop]."""
    conn = sqlite3.connect(":memory:")
    try:
        doc = find_similar_tracks(conn, stable_id="cur", mode="specific", limit=10, library=_specific_lib())
    finally:
        conn.close()
    assert doc["provider"] == "harmonic"
    assert doc["candidates"][0]["stable_id"] == "tight"
    nums = doc["candidates"][0]["rationale_numbers"]
    assert {"camelot", "bpm", "energy"} <= set(nums)


@pytest.mark.requirement("INTEL-12031")
def test_find_similar_specific_missing_bpm_is_insufficient() -> None:
    """[if] the current track is missing bpm [then] Specific raises insufficient_data, [else stop]."""
    lib = [TrackFeature("cur", "A", None, "8A", 5), TrackFeature("x", "B", 120.0, "8A", 5)]
    conn = sqlite3.connect(":memory:")
    try:
        from apps.dj_copilot.play_it import InsufficientDataError

        with pytest.raises(InsufficientDataError) as exc:
            find_similar_tracks(conn, stable_id="cur", mode="specific", limit=10, library=lib)
        assert exc.value.missing["bpm"] == ["cur"]
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


@pytest.mark.requirement("INTEL-12031")
def test_find_similar_specific_tight_beats_distant() -> None:
    """[if] tight and distant neighbours exist [then] tight ranks above distant, [else stop]."""
    conn = sqlite3.connect(":memory:")
    try:
        doc = find_similar_tracks(conn, stable_id="cur", mode="specific", limit=10, library=_specific_lib())
    finally:
        conn.close()
    ids = [row["stable_id"] for row in doc["candidates"]]
    assert ids.index("tight") < ids.index("distant")


@pytest.mark.requirement("INTEL-12031")
def test_find_similar_specific_empty_when_solo_track() -> None:
    """[if] Specific has no library neighbours [then] candidates is empty, [else stop]."""
    lib = [TrackFeature("solo", "A", 120.0, "8A", 5)]
    conn = sqlite3.connect(":memory:")
    try:
        doc = find_similar_tracks(conn, stable_id="solo", mode="specific", limit=10, library=lib)
    finally:
        conn.close()
    assert doc["candidates"] == []


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


def _seed_pairings_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE pairings (
            from_stable_id TEXT NOT NULL,
            to_stable_id   TEXT NOT NULL,
            direction      TEXT NOT NULL,
            source         TEXT NOT NULL,
            notes          TEXT,
            confidence     REAL,
            created_at     TEXT NOT NULL,
            modified_at    TEXT NOT NULL
        )
        """
    )


def test_keyless_candidate_unscored_and_below_adjacent(tmp_path) -> None:
    db = tmp_path / "s.db"
    conn = sqlite3.connect(str(db), isolation_level=None)
    try:
        library = [*_lib_small(), TrackFeature("t-nokey", "NoKey", 121.0, None, 5)]
        out = suggest_next(
            conn=conn,
            current_stable_id="cur",
            library=library,
            top_n=10,
        )
        ids = [s.stable_id for s in out]
        assert ids.index("t-adj") < ids.index("t-nokey")
        nokey = next(s for s in out if s.stable_id == "t-nokey")
        assert nokey.unscored == ("key",)
        assert "camelot_step_0" not in nokey.rationale_tags
        assert all(s.unscored == () for s in out if s.stable_id != "t-nokey")
    finally:
        conn.close()


def test_unscored_survives_pairing_bump(tmp_path) -> None:
    db = tmp_path / "s.db"
    conn = sqlite3.connect(str(db), isolation_level=None)
    try:
        _seed_pairings_table(conn)
        conn.execute(
            "INSERT INTO pairings"
            "(from_stable_id, to_stable_id, direction, source, "
            " created_at, modified_at) "
            "VALUES (?, ?, 'into', 'manual', '2026-01-01', '2026-01-01')",
            ("cur", "t-nokey"),
        )
        library = [*_lib_small(), TrackFeature("t-nokey", "NoKey", 121.0, None, 5)]
        out = suggest_next(
            conn=conn,
            current_stable_id="cur",
            library=library,
            top_n=10,
        )
        nokey = next(s for s in out if s.stable_id == "t-nokey")
        assert nokey.unscored == ("key",)
        assert "pair_manual" in nokey.rationale_tags
    finally:
        conn.close()


def test_unscored_survives_pressure_prior(tmp_path) -> None:
    db = tmp_path / "s.db"
    conn = sqlite3.connect(str(db), isolation_level=None)
    try:
        library = [*_lib_small(), TrackFeature("t-nokey", "NoKey", 121.0, None, 5)]
        ctx = SessionContext(
            recent=[PlayedTrack(f"r-{i}", f"R{i}", 120.0, "8A", 9, played_at=FIXED_NOW) for i in range(4)],
            source="manual",
            captured_at=FIXED_NOW,
        )
        out = suggest_next(
            conn=conn,
            current_stable_id="cur",
            library=library,
            context=ctx,
            top_n=10,
        )
        nokey = next(s for s in out if s.stable_id == "t-nokey")
        assert nokey.unscored == ("key",)
    finally:
        conn.close()
