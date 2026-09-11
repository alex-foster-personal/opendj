"""Full BPM-projection agreement sweep for own beatgrid records.

[if] beatgrid is on own [then] effective_fields bpm equals the canonical projection, [else stop].

Deliverable 4 of nav1-beatgrid-record (issue #1591): several BPM and tempo-change
combinations under both the PARITY-02 toggle and the persisted own default,
asserting `effective_fields` reads the same value `apps.analysis.canonical` wrote
into `analysis_projection`. The single-record producer-path check stays in
`tests/analysis_beatgrid/test_bpm_projection_agreement.py`.

-Claude
"""
from __future__ import annotations

import dataclasses
import sqlite3
from typing import Any

import pytest

from apps.analysis import selection
from apps.analysis import store as store_mod
from apps.analysis.lanes import LaneResult
from tests.analysis_contract.conftest import beatgrid_payload, own_record

pytestmark = pytest.mark.requirement("NATIVE-02")

BACKEND = "own_beatgrid.backfill"

SWEEP_CASES: list[tuple[str, float, int]] = [
    ("mid_band_static", 128.0, 0),
    ("near_min_boundary", 70.0, 0),
    ("near_max_boundary", 180.0, 0),
    ("octave_low_80", 80.0, 0),
    ("octave_high_160", 160.0, 0),
    ("octave_scaled_64", 64.0, 0),
    ("octave_scaled_128", 128.0, 0),
    ("tempo_changes_mid", 128.0, 2),
    ("tempo_changes_boundary", 70.0, 1),
    ("tempo_changes_octave", 160.0, 1),
]

SOURCE_MODES = ("toggle", "promote")


def _write_own_record(
    db: sqlite3.Connection, stable_id: str, bpm: float, tempo_changes: int
) -> dict[str, Any]:
    payload = beatgrid_payload(bpm=bpm, tempo_changes=tempo_changes)
    record = dataclasses.replace(
        own_record(
            stable_id=stable_id,
            producer="backfill",
            version="1.0.0",
            result=LaneResult(status="ok", payload=payload),
        ),
        bpm=bpm,
    )
    store_mod.upsert_record(record, conn=db)
    return payload


def _select_own(db: sqlite3.Connection, source_mode: str) -> selection.Selection:
    if source_mode == "toggle":
        selection.set_toggle("beatgrid", "own")
    elif source_mode == "promote":
        selection.set_default(db, "beatgrid", "own")
    else:
        raise AssertionError(f"unknown source_mode: {source_mode}")
    return selection.Selection.resolve(db)


def _projection_bpm_row(db: sqlite3.Connection, stable_id: str) -> tuple[Any, ...]:
    row = db.execute(
        "SELECT value, status, backend FROM analysis_projection "
        "WHERE stable_id = ? AND field = 'bpm'",
        (stable_id,),
    ).fetchone()
    assert row is not None, f"no bpm projection for {stable_id}"
    return row


@pytest.mark.parametrize("source_mode", SOURCE_MODES)
@pytest.mark.parametrize("case_id,bpm,tempo_changes", SWEEP_CASES)
def test_effective_fields_bpm_matches_canonical_projection(
    db: sqlite3.Connection,
    case_id: str,
    bpm: float,
    tempo_changes: int,
    source_mode: str,
) -> None:
    stable_id = f"sid-{case_id}-{source_mode}"
    payload = _write_own_record(db, stable_id, bpm, tempo_changes)

    row = _projection_bpm_row(db, stable_id)
    selected = _select_own(db, source_mode)
    got = selection.effective_fields(db, [stable_id], selected)[stable_id]["bpm"]

    assert got.value == row[0] == bpm
    assert got.status == "ok"
    assert got.source == BACKEND
    assert row[2] == BACKEND

    if tempo_changes > 0:
        expected_count = len(payload["tempo_changes"])
        count_row = db.execute(
            "SELECT value, status FROM analysis_projection "
            "WHERE stable_id = ? AND field = 'tempo_change_count'",
            (stable_id,),
        ).fetchone()
        assert count_row is not None
        assert count_row[0] == expected_count
        got_count = selection.effective_fields(db, [stable_id], selected)[stable_id][
            "tempo_change_count"
        ]
        assert got_count.value == count_row[0] == expected_count


def test_rbx_default_does_not_read_the_own_projection_until_toggled(db: sqlite3.Connection) -> None:
    stable_id = "sid-rbx-control"
    _write_own_record(db, stable_id, 128.0, 0)

    db.execute(
        "INSERT INTO tracks (stable_id, stable_id_tier, title, created_at, updated_at) "
        "VALUES (?, 'inferred', 'Fixture', '2026-09-09T00:00:00Z', '2026-09-09T00:00:00Z')",
        (stable_id,),
    )
    db.execute(
        "INSERT INTO track_fields (stable_id, field_name, value_json, source, "
        "confidence, modified_at) VALUES (?, 'bpm', '99.0', 'rekordbox', 1.0, 'x')",
        (stable_id,),
    )
    db.commit()

    selected = selection.Selection.resolve(db)
    got = selection.effective_fields(db, [stable_id], selected)[stable_id]["bpm"]
    assert got.value == 99.0
    assert got.source != BACKEND

    selection.set_toggle("beatgrid", "own")
    selected_own = selection.Selection.resolve(db)
    got_own = selection.effective_fields(db, [stable_id], selected_own)[stable_id]["bpm"]
    row = _projection_bpm_row(db, stable_id)
    assert got_own.value == row[0] == 128.0
    assert got_own.source == BACKEND


def test_no_own_value_ever_enters_track_fields(db: sqlite3.Connection) -> None:
    stable_id = "sid-no-track-fields"
    _write_own_record(db, stable_id, 128.0, 0)

    written_rows = db.execute(
        "SELECT COUNT(*) FROM track_fields WHERE stable_id = ?", (stable_id,)
    ).fetchone()[0]
    history = db.execute(
        "SELECT COUNT(*) FROM track_field_history WHERE stable_id = ?", (stable_id,)
    ).fetchone()[0]
    assert written_rows == 0
    assert history == 0


def test_a_failed_lane_projects_a_named_failure_through_effective_fields(
    db: sqlite3.Connection,
) -> None:
    stable_id = "sid-failed"
    record = own_record(
        stable_id=stable_id,
        producer="backfill",
        result=LaneResult(status="failed", reason="no_trackable_pulse", payload={}),
    )
    store_mod.upsert_record(record, conn=db)

    row = db.execute(
        "SELECT value, status, reason FROM analysis_projection "
        "WHERE stable_id = ? AND field = 'bpm'",
        (stable_id,),
    ).fetchone()
    assert row == (None, "failed", "no_trackable_pulse")

    selection.set_toggle("beatgrid", "own")
    got = selection.effective_fields(
        db, [stable_id], selection.Selection.resolve(db)
    )[stable_id]["bpm"]
    assert got.status == "failed"
    assert got.reason == "no_trackable_pulse"
    assert got.value is None
