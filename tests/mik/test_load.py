"""Loader tests: the two-case separation, the gate, precedence, idempotency.

The two cases must NOT be collapsed:

* matched track  -> ``track_fields`` + ``track_energy_segments``, even when its
  audio is absent. Availability is a dimension, not a second copy.
* unmatched row  -> ``unmatched_source_analysis`` only. Never ``track_fields``,
  because there is no stable_id to hang it on and inventing one fabricates
  identity.

[if] a MIK song matches a track or none [then] load() writes fields/stages unmatched, [else stop].
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.mik import availability as avail
from apps.mik import load as loader
from apps.mik import match as matcher
from apps.mik import mikdb
from apps.shared.equivalence import EquivalenceGate

pytestmark = pytest.mark.requirement("META-01")

ALL_PASSED = {field: "passed" for field in loader.GATED_FIELDS}


def _load(
    state_conn: sqlite3.Connection,
    store: Path,
    data_dir: Path,
    *,
    allow_unverified: bool = False,
    overwrite_lower_precedence: bool = False,
    allow_fuzzy: bool = True,
):
    songs, _stats = mikdb.read_songs(mikdb.open_ro(store))
    index = matcher.TrackIndex.from_conn(state_conn)
    report = matcher.match_songs(songs, index, allow_fuzzy=allow_fuzzy)
    gate = EquivalenceGate.load(data_dir, allow_unverified=allow_unverified)
    plan = loader.build_plan(
        songs,
        report,
        gate,
        state_conn,
        overwrite_lower_precedence=overwrite_lower_precedence,
    )
    return plan, gate


# ------------------------------------------------------- the gate blocks


def test_no_verdict_file_writes_nothing_at_all(
    state_conn: sqlite3.Connection, add_track, make_mik_store, data_dir: Path
) -> None:
    add_track("a" * 40, file_path="/Users/dev/x.mp3")
    store = make_mik_store(
        [{"path": "/Users/dev/x.mp3", "confidence": 0.9, "segments": [(0.0, 60.0, 5)]}]
    )
    plan, gate = _load(state_conn, store, data_dir)
    assert gate.file_present is False
    assert plan.field_writes == []
    assert plan.segment_writes == []
    assert plan.staged == []
    assert set(plan.blocked_by_gate) == set(loader.GATED_FIELDS)
    result = loader.apply_plan(state_conn, plan)
    assert (result.fields_written, result.segment_rows, result.staged_written) == (
        0,
        0,
        0,
    )
    assert state_conn.execute("SELECT COUNT(*) FROM track_fields").fetchone()[0] == 0


def test_a_failed_field_is_blocked_while_a_passed_one_lands(
    state_conn: sqlite3.Connection,
    add_track,
    make_mik_store,
    data_dir: Path,
    write_verdicts,
) -> None:
    add_track("a" * 40, file_path="/Users/dev/x.mp3")
    write_verdicts({"energy": "passed", "loudness": "failed"})
    store = make_mik_store([{"path": "/Users/dev/x.mp3", "confidence": 0.9}])
    plan, _gate = _load(state_conn, store, data_dir)
    written = {write.field_name for write in plan.field_writes}
    assert written == {"energy"}
    assert plan.blocked_by_gate["loudness"] == 1
    assert plan.blocked_by_gate["bpm"] == 1  # no entry at all: untested


def test_override_lets_an_untested_field_through(
    state_conn: sqlite3.Connection, add_track, make_mik_store, data_dir: Path
) -> None:
    add_track("a" * 40, file_path="/Users/dev/x.mp3")
    store = make_mik_store([{"path": "/Users/dev/x.mp3", "confidence": 0.9}])
    plan, _gate = _load(state_conn, store, data_dir, allow_unverified=True)
    assert {write.field_name for write in plan.field_writes} >= {"energy", "loudness"}


# ------------------------------------------- case (a): matched, audio absent


def test_absent_audio_keeps_its_analysis_in_track_fields(
    state_conn: sqlite3.Connection,
    add_track,
    make_mik_store,
    data_dir: Path,
    write_verdicts,
) -> None:
    """the maintainer's ask: grab the analysis for files we do not have, marked missing."""
    write_verdicts(ALL_PASSED)
    add_track("a" * 40, file_path="/Users/dev/gone.mp3")
    avail.refresh(state_conn)
    assert (
        state_conn.execute(
            "SELECT state FROM track_availability WHERE stable_id = ?", ("a" * 40,)
        ).fetchone()[0]
        == "absent"
    )
    store = make_mik_store(
        [
            {
                "path": "/Users/dev/gone.mp3",
                "confidence": 0.95,
                "energy": 7.0,
                "segments": [(0.0, 60.0, 5), (60.0, 30.0, 8)],
            }
        ]
    )
    plan, _gate = _load(state_conn, store, data_dir)
    loader.apply_plan(state_conn, plan)
    # Analysis lives in ONE place, keyed by stable_id.
    assert (
        state_conn.execute(
            "SELECT value_json FROM track_fields WHERE stable_id = ? AND "
            "field_name = 'energy'",
            ("a" * 40,),
        ).fetchone()[0]
        == "7"
    )
    assert (
        state_conn.execute(
            "SELECT COUNT(*) FROM track_energy_segments WHERE stable_id = ?",
            ("a" * 40,),
        ).fetchone()[0]
        == 2
    )
    # And it is impossible to confuse with playable data: the safe views exclude it.
    assert state_conn.execute("SELECT COUNT(*) FROM tracks_available").fetchone()[0] == 0
    assert (
        state_conn.execute("SELECT COUNT(*) FROM track_fields_available").fetchone()[0]
        == 0
    )
    assert (
        state_conn.execute("SELECT COUNT(*) FROM tracks_unavailable").fetchone()[0] == 1
    )
    # Nothing was duplicated into the staging table.
    assert (
        state_conn.execute(
            "SELECT COUNT(*) FROM unmatched_source_analysis"
        ).fetchone()[0]
        == 0
    )


# --------------------------------------- case (b): no track row to hang it on


def test_unmatched_analysis_never_reaches_track_fields(
    state_conn: sqlite3.Connection, make_mik_store, data_dir: Path, write_verdicts
) -> None:
    write_verdicts(ALL_PASSED)
    store = make_mik_store(
        [
            {
                "path": "/Users/dev/orphan.mp3",
                "name": "7 - Orphan",
                "artist": "Nobody",
                "confidence": 0.9,
                "segments": [(0.0, 45.0, 4)],
            }
        ]
    )
    plan, _gate = _load(state_conn, store, data_dir)
    assert plan.field_writes == []
    assert plan.segment_writes == []
    assert {row.field_name for row in plan.staged} == set(loader.GATED_FIELDS)
    loader.apply_plan(state_conn, plan)
    assert state_conn.execute("SELECT COUNT(*) FROM track_fields").fetchone()[0] == 0
    assert (
        state_conn.execute(
            "SELECT COUNT(*) FROM track_energy_segments"
        ).fetchone()[0]
        == 0
    )
    rows = state_conn.execute(
        "SELECT field_name, unmatched_reason, title, artist, source_path, "
        "promoted_stable_id FROM unmatched_source_analysis ORDER BY field_name"
    ).fetchall()
    assert len(rows) == len(loader.GATED_FIELDS)
    for _field, reason, title, artist, path, promoted in rows:
        assert reason == "no_candidate"
        assert title == "Orphan"  # energy prefix stripped at import
        assert artist == "Nobody"
        assert path == "/Users/dev/orphan.mp3"
        assert promoted is None


def test_staged_series_keeps_milliseconds(
    state_conn: sqlite3.Connection, make_mik_store, data_dir: Path, write_verdicts
) -> None:
    write_verdicts({"energy_segments": "passed"})
    store = make_mik_store(
        [{"path": "/none/x.mp3", "confidence": 0.9, "segments": [(60.4, 14.9, 8)]}]
    )
    plan, _gate = _load(state_conn, store, data_dir)
    loader.apply_plan(state_conn, plan)
    value = state_conn.execute(
        "SELECT value_json FROM unmatched_source_analysis WHERE field_name = ?",
        ("energy_segments",),
    ).fetchone()[0]
    assert '"start_ms":60400' in value
    assert '"length_ms":14900' in value


def test_staged_identity_refreshes_on_a_rescan(
    state_conn: sqlite3.Connection, make_mik_store, data_dir: Path, write_verdicts
) -> None:
    """P2 regression (PR #383 review): the staged upsert's WHERE guard only
    compared ``value_json``/``unmatched_reason``, so a rescan that changed
    MIK's own title/artist/confidence/etc for an already-staged row -- with
    the analysis value itself unchanged -- looked like a no-op and the stale
    identity metadata survived forever."""
    write_verdicts({"energy_segments": "passed"})
    same_segments = [(0.0, 45.0, 4)]
    store1 = make_mik_store(
        [
            {
                "path": "/none/orphan.mp3",
                "name": "7 - Orphan",
                "artist": "Nobody",
                "confidence": 0.9,
                "segments": same_segments,
            }
        ]
    )
    plan1, _gate = _load(state_conn, store1, data_dir)
    first = loader.apply_plan(state_conn, plan1)
    assert first.staged_written == 1

    store2 = make_mik_store(
        [
            {
                "path": "/none/orphan.mp3",
                "name": "7 - Orphan",
                "artist": "Somebody Else",
                "confidence": 0.9,
                "segments": same_segments,
            }
        ],
        name="Collection10-rescan.mikdb",
    )
    plan2, _gate2 = _load(state_conn, store2, data_dir)
    second = loader.apply_plan(state_conn, plan2)
    assert second.staged_written == 1
    assert second.staged_unchanged == 0

    artist = state_conn.execute(
        "SELECT artist FROM unmatched_source_analysis WHERE field_name = ?",
        ("energy_segments",),
    ).fetchone()[0]
    assert artist == "Somebody Else"


def test_promoted_row_is_not_reclaimed_by_a_later_match(
    state_conn: sqlite3.Connection,
    add_track,
    make_mik_store,
    data_dir: Path,
    write_verdicts,
) -> None:
    """P1 regression (PR #383 review): a source row already promoted to track
    A must not have its analysis written onto track B just because a later
    library change lets the normal matcher pair the same row with B. That
    would attach one MIK analysis to two identities and bypass
    ``PromotionConflict`` entirely."""
    write_verdicts(ALL_PASSED)
    add_track("a" * 40, file_path="/Users/dev/promoted.mp3")
    add_track("b" * 40, file_path="/Users/dev/rematched.mp3")
    state_conn.execute(
        "INSERT INTO unmatched_source_analysis(source, source_row_id, "
        "field_name, value_json, unmatched_reason, modified_at, imported_at, "
        "promoted_stable_id, promoted_at) VALUES ('mik', '1', 'energy', '6.0', "
        "'no_candidate', '2024-01-01T00:00:00+00:00', "
        "'2024-01-01T00:00:00+00:00', ?, '2024-01-01T00:00:00+00:00')",
        ("a" * 40,),
    )
    state_conn.commit()
    store = make_mik_store(
        [{"path": "/Users/dev/rematched.mp3", "pk": 1, "confidence": 0.9}]
    )
    plan, _gate = _load(state_conn, store, data_dir)
    written = {write.field_name for write in plan.field_writes if write.stable_id == "b" * 40}
    assert "energy" not in written
    assert plan.blocked_by_promotion_conflict["energy"] == 1
    loader.apply_plan(state_conn, plan)
    row = state_conn.execute(
        "SELECT COUNT(*) FROM track_fields WHERE stable_id = ? AND field_name = 'energy'",
        ("b" * 40,),
    ).fetchone()[0]
    assert row == 0


def test_promotion_binding_on_one_field_protects_every_field_of_the_row(
    state_conn: sqlite3.Connection,
    add_track,
    make_mik_store,
    data_dir: Path,
    write_verdicts,
) -> None:
    """P1 regression (PR #383 review, fresh evidence): promotion only binds
    the fields that cleared the gate at promotion time (say, energy), while
    a sibling field (key) was blocked and so carries no binding of its own.
    A later library change that rematches the same source row to a
    DIFFERENT track must still be rejected for key too -- the row's target
    is the promoted track, not whichever field happens to have a recorded
    binding."""
    write_verdicts(ALL_PASSED)
    add_track("a" * 40, file_path="/Users/dev/promoted.mp3")
    add_track("b" * 40, file_path="/Users/dev/rematched.mp3")
    state_conn.execute(
        "INSERT INTO unmatched_source_analysis(source, source_row_id, "
        "field_name, value_json, unmatched_reason, modified_at, imported_at, "
        "promoted_stable_id, promoted_at) VALUES ('mik', '1', 'energy', '6.0', "
        "'no_candidate', '2024-01-01T00:00:00+00:00', "
        "'2024-01-01T00:00:00+00:00', ?, '2024-01-01T00:00:00+00:00')",
        ("a" * 40,),
    )
    state_conn.commit()
    store = make_mik_store(
        [
            {
                "path": "/Users/dev/rematched.mp3",
                "pk": 1,
                "confidence": 0.95,
            }
        ]
    )
    plan, _gate = _load(state_conn, store, data_dir)
    written = {write.field_name for write in plan.field_writes if write.stable_id == "b" * 40}
    assert "key" not in written
    assert plan.blocked_by_promotion_conflict["key"] == 1
    loader.apply_plan(state_conn, plan)
    row = state_conn.execute(
        "SELECT COUNT(*) FROM track_fields WHERE stable_id = ? AND field_name = 'key'",
        ("b" * 40,),
    ).fetchone()[0]
    assert row == 0
