"""Precedence and invariant tests split out of :mod:`tests.mik.test_load`
(the file-size review gate). Shares the ``_load`` helper and ``ALL_PASSED``
fixture constant with that module rather than redefining them.

[if] a MIK song matches a track or none [then] load() writes fields/stages unmatched, [else stop].
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from apps.mik import load as loader

from .test_load import ALL_PASSED, _load

pytestmark = pytest.mark.requirement("META-01")

# ------------------------------------------------------------- precedence


def test_mik_bpm_never_clobbers_rekordbox(
    state_conn: sqlite3.Connection,
    add_track,
    make_mik_store,
    data_dir: Path,
    write_verdicts,
) -> None:
    """track_fields has no source in its PK, so a write REPLACES. MIK loses BPM."""
    write_verdicts(ALL_PASSED)
    add_track("a" * 40, file_path="/Users/user/x.mp3")
    state_conn.execute(
        "INSERT INTO track_fields(stable_id, field_name, value_json, source, "
        "modified_at) VALUES (?, 'bpm', '120.0', 'rekordbox', "
        "'2026-01-01T00:00:00+00:00')",
        ("a" * 40,),
    )
    store = make_mik_store(
        [{"path": "/Users/user/x.mp3", "tempo": 90.0, "confidence": 0.99}]
    )
    plan, _gate = _load(
        state_conn, store, data_dir, overwrite_lower_precedence=True
    )
    assert plan.blocked_by_precedence["bpm"] == 1
    loader.apply_plan(state_conn, plan)
    assert (
        state_conn.execute(
            "SELECT value_json, source FROM track_fields WHERE field_name='bpm'"
        ).fetchone()
        == ("120.0", "rekordbox")
    )


def test_low_confidence_key_falls_through_to_rekordbox(
    state_conn: sqlite3.Connection,
    add_track,
    make_mik_store,
    data_dir: Path,
    write_verdicts,
) -> None:
    write_verdicts(ALL_PASSED)
    add_track("a" * 40, file_path="/Users/user/x.mp3")
    store = make_mik_store(
        [{"path": "/Users/user/x.mp3", "key": "3A", "confidence": 0.29}]
    )
    plan, _gate = _load(state_conn, store, data_dir)
    assert plan.blocked_by_key_floor == 1
    assert {write.field_name for write in plan.field_writes} == {
        "energy",
        "bpm",
        "loudness",
        "clipped_peak_count",
    }


def test_high_confidence_key_only_overwrites_when_asked(
    state_conn: sqlite3.Connection,
    add_track,
    make_mik_store,
    data_dir: Path,
    write_verdicts,
) -> None:
    write_verdicts(ALL_PASSED)
    add_track("a" * 40, file_path="/Users/user/x.mp3")
    state_conn.execute(
        "INSERT INTO track_fields(stable_id, field_name, value_json, source, "
        "modified_at) VALUES (?, 'key', '\"8A\"', 'rekordbox', "
        "'2026-01-01T00:00:00+00:00')",
        ("a" * 40,),
    )
    store = make_mik_store(
        [{"path": "/Users/user/x.mp3", "key": "9A", "confidence": 0.99}]
    )
    default_plan, _ = _load(state_conn, store, data_dir)
    assert default_plan.blocked_by_precedence["key"] == 1
    opted_in, _ = _load(
        state_conn, store, data_dir, overwrite_lower_precedence=True
    )
    assert [write.value for write in opted_in.field_writes if
            write.field_name == "key"] == ["9A"]


def test_mid_band_key_is_flagged_for_review_not_silently_written(
    state_conn: sqlite3.Connection,
    add_track,
    make_mik_store,
    data_dir: Path,
    write_verdicts,
) -> None:
    """0.70-0.90 with rekordbox present: flag it, never guess (ISMIR 2015)."""
    write_verdicts(ALL_PASSED)
    add_track("a" * 40, file_path="/Users/user/x.mp3")
    state_conn.execute(
        "INSERT INTO track_fields(stable_id, field_name, value_json, source, "
        "modified_at) VALUES (?, 'key', '\"8A\"', 'rekordbox', "
        "'2026-01-01T00:00:00+00:00')",
        ("a" * 40,),
    )
    store = make_mik_store(
        [{"path": "/Users/user/x.mp3", "key": "9A", "confidence": 0.80}]
    )
    plan, _gate = _load(
        state_conn, store, data_dir, overwrite_lower_precedence=True
    )
    assert plan.key_review_band == ["a" * 40]
    assert "key" not in {write.field_name for write in plan.field_writes}
    assert plan.blocked_by_key_floor == 0


def test_mid_band_key_still_fills_an_empty_field(
    state_conn: sqlite3.Connection,
    add_track,
    make_mik_store,
    data_dir: Path,
    write_verdicts,
) -> None:
    """Nothing to disagree with, so a 0.80-confidence key beats no key."""
    write_verdicts(ALL_PASSED)
    add_track("a" * 40, file_path="/Users/user/x.mp3")
    store = make_mik_store(
        [{"path": "/Users/user/x.mp3", "key": "9A", "confidence": 0.80}]
    )
    plan, _gate = _load(state_conn, store, data_dir)
    assert plan.key_review_band == []
    assert [w.value for w in plan.field_writes if w.field_name == "key"] == ["9A"]


def test_key_confidence_is_mik_own_per_track_value(
    state_conn: sqlite3.Connection,
    add_track,
    make_mik_store,
    data_dir: Path,
    write_verdicts,
) -> None:
    """Not the flat 0.95: the whole point of the audit's correction 1."""
    write_verdicts(ALL_PASSED)
    add_track("a" * 40, file_path="/Users/user/x.mp3")
    store = make_mik_store(
        [{"path": "/Users/user/x.mp3", "key": "9A", "confidence": 0.83}]
    )
    plan, _gate = _load(state_conn, store, data_dir)
    loader.apply_plan(state_conn, plan)
    assert (
        state_conn.execute(
            "SELECT confidence FROM track_fields WHERE field_name='key'"
        ).fetchone()[0]
        == 0.83
    )
    assert (
        state_conn.execute(
            "SELECT confidence FROM track_fields WHERE field_name='energy'"
        ).fetchone()[0]
        == loader.MIK_CONFIDENCE
    )


# ------------------------------------------------------------ invariants


def test_modified_at_comes_from_the_analysis_date_not_the_clock(
    state_conn: sqlite3.Connection,
    add_track,
    make_mik_store,
    data_dir: Path,
    write_verdicts,
) -> None:
    write_verdicts(ALL_PASSED)
    add_track("a" * 40, file_path="/Users/user/x.mp3")
    store = make_mik_store(
        [
            {
                "path": "/Users/user/x.mp3",
                "confidence": 0.9,
                "analysed_at": "2023-12-20T02:20:10+00:00",
            }
        ]
    )
    plan, _gate = _load(state_conn, store, data_dir)
    loader.apply_plan(state_conn, plan)
    stamps = {
        row[0]
        for row in state_conn.execute("SELECT modified_at FROM track_fields")
    }
    assert stamps == {"2023-12-20T02:20:10+00:00"}


def test_second_live_run_writes_nothing(
    state_conn: sqlite3.Connection,
    add_track,
    make_mik_store,
    data_dir: Path,
    write_verdicts,
) -> None:
    write_verdicts(ALL_PASSED)
    add_track("a" * 40, file_path="/Users/user/x.mp3")
    store = make_mik_store(
        [
            {"path": "/Users/user/x.mp3", "confidence": 0.9,
             "segments": [(0.0, 60.0, 5)]},
            {"path": "/none/y.mp3", "confidence": 0.9, "segments": [(0.0, 30.0, 3)]},
        ]
    )
    plan, _gate = _load(state_conn, store, data_dir)
    first = loader.apply_plan(state_conn, plan)
    assert first.fields_written > 0 and first.staged_written > 0
    plan2, _gate2 = _load(state_conn, store, data_dir)
    second = loader.apply_plan(state_conn, plan2)
    assert second.fields_written == 0
    assert second.staged_written == 0
    assert second.segment_rows == 0
    assert second.segment_rows_unchanged == first.segment_rows


def test_apply_is_atomic(
    state_conn: sqlite3.Connection,
    add_track,
    make_mik_store,
    data_dir: Path,
    write_verdicts,
) -> None:
    write_verdicts(ALL_PASSED)
    add_track("a" * 40, file_path="/Users/user/x.mp3")
    store = make_mik_store([{"path": "/Users/user/x.mp3", "confidence": 0.9}])
    plan, _gate = _load(state_conn, store, data_dir)
    # A staged row referencing a bogus reason trips the CHECK constraint.
    plan.staged.append(
        loader.StagedWrite(
            source_row_id="999",
            field_name="energy",
            value=5,
            confidence=0.95,
            unmatched_reason="made-up",
            title=None,
            artist=None,
            album=None,
            duration_ms=None,
            source_path=None,
            modified_at="2024-01-01T00:00:00+00:00",
        )
    )
    with pytest.raises(sqlite3.IntegrityError):
        loader.apply_plan(state_conn, plan)
    assert state_conn.execute("SELECT COUNT(*) FROM track_fields").fetchone()[0] == 0


def test_verification_basis_is_persisted_with_the_data(
    state_conn: sqlite3.Connection,
    add_track,
    make_mik_store,
    data_dir: Path,
) -> None:
    """A one-sided check must be distinguishable from cross-validation in the DB.

    Not just in a log line: six months from now nobody should be able to read
    energy_segments and assume it was cross-validated.
    """
    add_track("a" * 40, file_path="/Users/user/x.mp3")
    (data_dir / "state" / "equivalence-verdicts.json").write_text(
        json.dumps(
            {
                "fields": {
                    "energy": {
                        "status": "passed",
                        "basis": "cross_source",
                        "suite_status": "passed",
                        "normaliser": "mik_energy_1_10",
                        "checked_at": "2026-07-28T01:00:00+00:00",
                    },
                    "energy_segments": {
                        "status": "passed",
                        "basis": "single_source",
                        "suite_status": "passed_single_source",
                        "normaliser": "mik_seconds_to_ms",
                        "checked_at": "2026-07-28T01:00:00+00:00",
                        "verified_by": "apps.equivalence",
                    },
                    "bpm": {"status": "failed", "normaliser": None},
                }
            }
        ),
        encoding="utf-8",
    )
    store = make_mik_store(
        [
            {"path": "/Users/user/x.mp3", "confidence": 0.95,
             "segments": [(0.0, 60.0, 5)]}
        ]
    )
    plan, _gate = _load(state_conn, store, data_dir)
    result = loader.apply_plan(state_conn, plan)
    assert result.verification_rows == len(loader.GATED_FIELDS)
    rows = {
        field_name: (status, basis, verified_by, overridden)
        for field_name, status, basis, verified_by, overridden in state_conn.execute(
            "SELECT field_name, status, basis, verified_by, overridden "
            "FROM analysis_field_verification WHERE source = 'mik'"
        )
    }
    assert rows["energy"] == ("passed", "cross_source", None, 0)
    assert rows["energy_segments"] == (
        "passed",
        "single_source",
        "apps.equivalence",
        0,
    )
    assert rows["bpm"] == ("failed", "unverified", None, 0)
    assert rows["loudness"] == ("untested", "unverified", None, 0)
    # And the data it gated actually landed.
    assert (
        state_conn.execute(
            "SELECT COUNT(*) FROM track_energy_segments"
        ).fetchone()[0]
        == 1
    )


def test_clamped_start_is_persisted_per_row(
    state_conn: sqlite3.Connection,
    add_track,
    make_mik_store,
    data_dir: Path,
    write_verdicts,
) -> None:
    """A genuine 0 start and a clamped one must be distinguishable in the DB."""
    write_verdicts(ALL_PASSED)
    add_track("a" * 40, file_path="/Users/user/x.mp3")
    store = make_mik_store(
        [
            {
                "path": "/Users/user/x.mp3",
                "confidence": 0.95,
                "segments": [(-0.0195, 60.0, 5), (60.0, 30.0, 8)],
            }
        ]
    )
    plan, _gate = _load(state_conn, store, data_dir)
    loader.apply_plan(state_conn, plan)
    rows = state_conn.execute(
        "SELECT seq, start_ms, start_clamped FROM track_energy_segments "
        "ORDER BY seq"
    ).fetchall()
    assert rows == [(0, 0, 1), (1, 60000, 0)]


def test_staged_series_round_trips_the_clamp_flag(
    state_conn: sqlite3.Connection,
    add_track,
    make_mik_store,
    data_dir: Path,
    write_verdicts,
) -> None:
    """Staging must not lose the flag, or promotion would invent a clean 0."""
    write_verdicts(ALL_PASSED)
    store = make_mik_store(
        [{"path": "/none/y.mp3", "confidence": 0.9, "segments": [(-0.0195, 60.0, 5)]}]
    )
    plan, _gate = _load(state_conn, store, data_dir)
    loader.apply_plan(state_conn, plan)
    value = state_conn.execute(
        "SELECT value_json FROM unmatched_source_analysis WHERE field_name = ?",
        ("energy_segments",),
    ).fetchone()[0]
    assert '"start_clamped":true' in value


def test_override_is_recorded_as_overridden(
    state_conn: sqlite3.Connection, add_track, make_mik_store, data_dir: Path
) -> None:
    add_track("a" * 40, file_path="/Users/user/x.mp3")
    store = make_mik_store([{"path": "/Users/user/x.mp3", "confidence": 0.95}])
    plan, _gate = _load(state_conn, store, data_dir, allow_unverified=True)
    loader.apply_plan(state_conn, plan)
    overridden = state_conn.execute(
        "SELECT COUNT(*) FROM analysis_field_verification WHERE overridden = 1"
    ).fetchone()[0]
    assert overridden == len(loader.GATED_FIELDS)


def test_every_source_value_is_accounted_for(
    state_conn: sqlite3.Connection,
    add_track,
    make_mik_store,
    data_dir: Path,
    write_verdicts,
) -> None:
    """Bucket invariant: written + staged + blocked + absent == what MIK offered."""
    write_verdicts({"energy": "passed"})
    add_track("a" * 40, file_path="/Users/user/x.mp3")
    store = make_mik_store(
        [
            {"path": "/Users/user/x.mp3", "confidence": 0.9},
            {"path": "/none/y.mp3", "confidence": 0.9},
            {"path": "/none/z.mp3", "energy": None, "confidence": 0.9},
        ]
    )
    plan, _gate = _load(state_conn, store, data_dir)
    energy_written = sum(
        1 for write in plan.field_writes if write.field_name == "energy"
    )
    energy_staged = sum(1 for row in plan.staged if row.field_name == "energy")
    energy_blocked = plan.blocked_by_gate.get("energy", 0)
    energy_absent = plan.no_value.get("energy", 0)
    assert energy_written + energy_staged + energy_blocked + energy_absent == 3
    assert (energy_written, energy_staged, energy_absent) == (1, 1, 1)
