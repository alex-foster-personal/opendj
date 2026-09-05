"""Promotion tests: the documented way OUT of the staging table.

A staging table without a tested exit is a landfill. These tests pin the four
properties that make it safe: idempotency, loud conflict, the gate still
applying, and the staged row surviving as an audit trail.

[if] a row is promoted to known/unknown/deleted/claimed [then] it writes once/raises, [else stop].
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from apps.mik import load as loader
from apps.mik import match as matcher
from apps.mik import mikdb
from apps.mik import promote as promoter
from apps.shared.equivalence import EquivalenceGate

from .conftest import STAGED_FIELD_COUNT

pytestmark = pytest.mark.requirement("META-01")


def test_promotion_moves_scalars_and_the_series(
    state_conn: sqlite3.Connection, add_track, staged
) -> None:
    add_track("a" * 40, file_path="/Users/dev/orphan.mp3")
    result = promoter.promote(
        state_conn,
        staged["gate"],
        source_row_id=staged["source_row_id"],
        stable_id="a" * 40,
    )
    assert result.fields_written == STAGED_FIELD_COUNT - 1  # all but the series
    assert result.segment_rows == 2
    assert (
        state_conn.execute(
            "SELECT value_json FROM track_fields WHERE field_name='energy'"
        ).fetchone()[0]
        == "7"
    )
    rows = state_conn.execute(
        "SELECT seq, start_ms, length_ms, energy FROM track_energy_segments "
        "ORDER BY seq"
    ).fetchall()
    assert rows == [(0, 0, 60000, 5), (1, 60000, 30000, 8)]
    assert (
        state_conn.execute(
            "SELECT confidence FROM track_fields WHERE field_name='key'"
        ).fetchone()[0]
        == 0.91
    )


def test_promotion_is_idempotent(
    state_conn: sqlite3.Connection, add_track, staged
) -> None:
    add_track("a" * 40, file_path="/Users/dev/orphan.mp3")
    first = promoter.promote(
        state_conn, staged["gate"], source_row_id="1", stable_id="a" * 40
    )
    second = promoter.promote(
        state_conn, staged["gate"], source_row_id="1", stable_id="a" * 40
    )
    assert first.fields_written == STAGED_FIELD_COUNT - 1
    assert second.fields_written == 0
    assert second.segment_rows == 0
    assert second.already_promoted == STAGED_FIELD_COUNT
    written = state_conn.execute("SELECT COUNT(*) FROM track_fields").fetchone()[0]
    assert written == STAGED_FIELD_COUNT - 1
    assert (
        state_conn.execute(
            "SELECT COUNT(*) FROM track_energy_segments"
        ).fetchone()[0]
        == 2
    )
    # No spurious history: the second pass wrote nothing to overwrite.
    assert (
        state_conn.execute(
            "SELECT COUNT(*) FROM track_field_history"
        ).fetchone()[0]
        == 0
    )


def test_a_rescan_after_promotion_is_re_promoted_to_the_same_target(
    state_conn: sqlite3.Connection, add_track, make_mik_store, staged
) -> None:
    """P1 regression (PR #383 review): a manually promoted source row that
    stays unmatched (no track auto-discovers it) can still be rescanned by
    MIK. The rescan updates the staged value_json/confidence/modified_at in
    place while promoted_stable_id keeps pointing at the same target, so the
    early return in _promote_row must not treat that as still applied --
    every later promotion attempt returns there too, leaving the target
    permanently stale."""
    add_track("a" * 40, file_path="/Users/dev/target.mp3")
    first = promoter.promote(
        state_conn,
        staged["gate"],
        source_row_id="1",
        stable_id="a" * 40,
        now="2024-06-01T00:00:00+00:00",
    )
    assert first.fields_written == STAGED_FIELD_COUNT - 1
    assert (
        state_conn.execute(
            "SELECT value_json FROM track_fields WHERE field_name='energy'"
        ).fetchone()[0]
        == "7"
    )

    rescan_store = make_mik_store(
        [
            {
                "pk": 1,
                "path": "/Users/dev/orphan.mp3",
                "name": "7 - Orphan",
                "artist": "Nobody",
                "key": "9A",
                "energy": 9.0,
                "confidence": 0.91,
                "analysed_at": "2025-01-01T00:00:00+00:00",
                "segments": [(0.0, 60.0, 5), (60.0, 30.0, 8)],
            }
        ],
        name="Collection10-rescan.mikdb",
    )
    songs, _ = mikdb.read_songs(mikdb.open_ro(rescan_store))
    index = matcher.TrackIndex.from_conn(state_conn)
    report = matcher.match_songs(songs, index)
    plan = loader.build_plan(songs, report, staged["gate"], state_conn)
    loader.apply_plan(state_conn, plan)

    second = promoter.promote(
        state_conn,
        staged["gate"],
        source_row_id="1",
        stable_id="a" * 40,
        now="2025-02-01T00:00:00+00:00",
    )
    assert second.fields_written == STAGED_FIELD_COUNT - 1
    assert second.already_promoted == 0
    assert (
        state_conn.execute(
            "SELECT value_json FROM track_fields WHERE field_name='energy'"
        ).fetchone()[0]
        == "9"
    )


def test_repointing_to_a_different_track_raises(
    state_conn: sqlite3.Connection, add_track, staged
) -> None:
    add_track("a" * 40, file_path="/Users/dev/orphan.mp3")
    add_track("b" * 40, file_path="/Users/dev/other.mp3")
    promoter.promote(state_conn, staged["gate"], source_row_id="1", stable_id="a" * 40)
    with pytest.raises(promoter.PromotionConflict, match="already promoted"):
        promoter.promote(
            state_conn, staged["gate"], source_row_id="1", stable_id="b" * 40
        )


def test_promoting_to_an_unknown_track_raises(
    state_conn: sqlite3.Connection, staged
) -> None:
    with pytest.raises(promoter.PromotionError, match="not in tracks"):
        promoter.promote(
            state_conn, staged["gate"], source_row_id="1", stable_id="z" * 40
        )


def test_promoting_to_a_soft_deleted_track_raises(
    state_conn: sqlite3.Connection, add_track, staged
) -> None:
    """[if] the target stable_id's tracks row has deleted_at set [then]
    promote() raises PromotionError instead of resurrecting analysis onto a
    tombstoned track, [else stop].

    A soft delete only sets tracks.deleted_at; it never issues a physical
    DELETE, so the row a naive existence check finds is a tombstone, not a
    live track (soft-delete-read-guard, tests/cloudsync/
    test_soft_delete_read_guard.py).
    """
    add_track("a" * 40, file_path="/Users/dev/orphan.mp3")
    state_conn.execute(
        "UPDATE tracks SET deleted_at = ? WHERE stable_id = ?",
        (datetime.now(UTC).isoformat(), "a" * 40),
    )
    with pytest.raises(promoter.PromotionError, match="not in tracks"):
        promoter.promote(
            state_conn, staged["gate"], source_row_id="1", stable_id="a" * 40
        )


def test_promoting_an_unknown_source_row_raises(
    state_conn: sqlite3.Connection, add_track, staged
) -> None:
    add_track("a" * 40)
    with pytest.raises(promoter.PromotionError, match="no staged rows"):
        promoter.promote(
            state_conn, staged["gate"], source_row_id="404", stable_id="a" * 40
        )


def test_the_gate_still_applies_at_promotion_time(
    state_conn: sqlite3.Connection, add_track, staged, data_dir: Path, write_verdicts
) -> None:
    """A field staged under an override cannot sneak in later unverified."""
    add_track("a" * 40, file_path="/Users/dev/orphan.mp3")
    write_verdicts({"energy": "passed"})
    strict = EquivalenceGate.load(data_dir)
    result = promoter.promote(
        state_conn, strict, source_row_id="1", stable_id="a" * 40
    )
    assert result.fields_written == 1
    assert sorted(result.blocked_by_gate) == sorted(
        set(loader.GATED_FIELDS) - {"energy"}
    )
    assert {
        row[0]
        for row in state_conn.execute("SELECT field_name FROM track_fields")
    } == {"energy"}


def test_the_staged_row_survives_as_an_audit_trail(
    state_conn: sqlite3.Connection, add_track, staged
) -> None:
    add_track("a" * 40, file_path="/Users/dev/orphan.mp3")
    promoter.promote(
        state_conn,
        staged["gate"],
        source_row_id="1",
        stable_id="a" * 40,
        now="2026-07-28T10:00:00+00:00",
    )
    rows = state_conn.execute(
        "SELECT promoted_stable_id, promoted_at, unmatched_reason "
        "FROM unmatched_source_analysis"
    ).fetchall()
    assert len(rows) == STAGED_FIELD_COUNT
    for promoted, promoted_at, reason in rows:
        assert promoted == "a" * 40
        assert promoted_at == "2026-07-28T10:00:00+00:00"
        assert reason == "no_candidate"  # why it was staged is not rewritten


def test_discover_finds_a_row_once_the_track_exists(
    state_conn: sqlite3.Connection, add_track, staged
) -> None:
    assert promoter.discover(state_conn) == {}
    add_track("a" * 40, file_path="/Users/dev/orphan.mp3")
    assert promoter.discover(state_conn) == {"1": "a" * 40}


def test_discover_ignores_already_promoted_rows(
    state_conn: sqlite3.Connection, add_track, staged
) -> None:
    add_track("a" * 40, file_path="/Users/dev/orphan.mp3")
    promoter.promote_all(state_conn, staged["gate"], {"1": "a" * 40})
    assert promoter.discover(state_conn) == {}


def test_discover_leaves_an_ambiguous_row_staged(
    state_conn: sqlite3.Connection, add_track, staged
) -> None:
    add_track("a" * 40, file_path="/Users/dev/orphan.mp3")
    add_track("b" * 40, file_path="/Users/dev/orphan.mp3")
    assert promoter.discover(state_conn) == {}


def test_pending_counts_track_the_backlog(
    state_conn: sqlite3.Connection, add_track, staged
) -> None:
    assert promoter.pending_counts(state_conn) == {"no_candidate": 1}
    add_track("a" * 40, file_path="/Users/dev/orphan.mp3")
    promoter.promote_all(state_conn, staged["gate"], {"1": "a" * 40})
    assert promoter.pending_counts(state_conn) == {}
