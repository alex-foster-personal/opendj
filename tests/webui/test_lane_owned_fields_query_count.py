"""Lane-owned fields cost no sqlite work per rekordbox-mapped track (LIBM-135).

[if] resolving lane-owned fields issues sqlite work per track [then] fail, [else stop].

[if] resolving lane-owned fields for N mapped tracks issues more statements
than for 4 [then] fail, [else stop].

Found on the Missing tracks page (Thu 1 Oct 2026): ``GET /reconcile/broken``
took 5 to 9 s on a 9,713-track library. Profiled on a copy of that library,
258,530 statements ran in one request, and 218,000 of them were the same two:
the per-lane default was re-read (a schema probe plus a SELECT) for every
track and every lane-owned field, three times over, and each track then paid
one canonical-pointer lookup per field. ``tests/webui/test_listing_query_count``
pins the same property for UNMAPPED tracks, which never reach this code.

The instrument is sqlite's own trace callback on the real connection. Both
sizes sit inside one 500-id bind batch, so this pins "no work per track".

The overshoot control is the answer itself: batching must not change what any
track says, so every batched row is compared with the same function asked for
that one track alone, and the fixture is checked to really contain the
``available-not-selected`` rows the per-track path existed to produce.

Regression one-liners:
  - if effective_fields for 20 mapped tracks issues more statements than for 4 then broken
  - if the batch pre-check for 20 mapped tracks issues more statements than for 4 then broken
  - if a batched row differs from the same track resolved alone then broken
  - if no fixture row reads available-not-selected then the control proves nothing
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from apps.analysis import selection
from apps.analysis.lanes import LaneResult
from apps.analysis.record import AnalysisRecord
from apps.analysis.store import upsert_record
from apps.shared.state import db as state_db
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter
from apps.webui.server import analysis_overlay

pytestmark = [pytest.mark.requirement("LIBM-135")]

SMALL, LARGE = 4, 20
BPM = 121.0
FINGERPRINT = "sha256:" + "cd" * 32


def _sid(i: int) -> str:
    return f"{i:040x}"


def _beatgrid_record(stable_id: str) -> AnalysisRecord:
    beats = [
        {"t": round(index * 0.5, 5), "n": (index % 4) + 1, "bpm": BPM}
        for index in range(8)
    ]
    return AnalysisRecord(
        stable_id=stable_id,
        backend="own_beatgrid.backfill",
        backend_version="1.0.0",
        analyzed_at=datetime.now(UTC),
        duration_s=32.0,
        sample_rate=44100,
        bpm=BPM,
        bpm_confidence=0.9,
        key_camelot="",
        key_openkey="",
        key_confidence=0.0,
        energy=0,
        producer="backfill",
        producer_version="1.0.0",
        uses_model=False,
        model_sha256=None,
        decode_fingerprint=FINGERPRINT,
        lanes={
            "beatgrid": LaneResult(
                status="ok",
                payload={
                    "beats": beats,
                    "bpm": BPM,
                    "bpm_confidence": 0.9,
                    "octave_reason": "in_band",
                    "first_downbeat_s": 0.0,
                    "tempo_changes": [],
                    "static_grid_untrusted": False,
                },
            )
        },
    )


@pytest.fixture(autouse=True)
def _reset_toggles() -> Iterator[None]:
    selection.reset_toggles()
    yield
    selection.reset_toggles()


@pytest.fixture
def state_path(tmp_path: Path) -> Path:
    """LARGE tracks; every second one has an own beatgrid record."""
    path = tmp_path / "state.db"
    conn = state_db.open_rw(path)
    writer = StateWriter(conn, bus=FakeEventBus(), actor="test")
    try:
        for i in range(LARGE):
            writer.upsert_track(
                stable_id=_sid(i),
                stable_id_tier="inferred",
                title=f"Track {i}",
                artists=[],
                album=None,
                isrc=None,
                duration_ms=60_000,
                file_path=str(tmp_path / f"t{i}.wav"),
            )
    finally:
        writer.close()
        conn.close()
    for i in range(0, LARGE, 2):
        upsert_record(_beatgrid_record(_sid(i)), db_path=path)
    return path


def _traced(path: Path) -> tuple[sqlite3.Connection, list[str]]:
    conn = state_db.open_ro(path)
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    return conn, statements


def _effective(path: Path, ids: list[str]) -> tuple[dict, int]:
    conn, statements = _traced(path)
    try:
        resolved = selection.Selection.resolve(conn)
        statements.clear()
        out = selection.effective_fields(
            conn, ids, resolved, rb_mapped={sid: True for sid in ids}
        )
        return out, len(statements)
    finally:
        conn.close()


def test_effective_fields_statement_count_is_constant_in_track_count(
    state_path: Path,
) -> None:
    _small_out, small = _effective(state_path, [_sid(i) for i in range(SMALL)])
    _large_out, large = _effective(state_path, [_sid(i) for i in range(LARGE)])
    assert small > 0, "the trace saw nothing: the instrument is not attached"
    assert large == small, (
        f"{LARGE} mapped tracks issued {large} statements, {SMALL} issued {small}"
    )


def test_batch_precheck_statement_count_is_constant_in_track_count(
    state_path: Path,
) -> None:
    counts: list[int] = []
    for size in (SMALL, LARGE):
        ids = [_sid(i) for i in range(size)]
        conn, statements = _traced(state_path)
        try:
            analysis_overlay._batch_needs_lane_owned_fields(
                conn, ids, {sid: True for sid in ids}
            )
            counts.append(len(statements))
        finally:
            conn.close()
    assert counts[0] > 0, "the trace saw nothing: the instrument is not attached"
    assert counts[1] == counts[0], counts


def test_batched_rows_match_each_track_resolved_alone(state_path: Path) -> None:
    ids = [_sid(i) for i in range(LARGE)]
    batched, _count = _effective(state_path, ids)
    for sid in ids:
        alone, _ = _effective(state_path, [sid])
        assert batched[sid] == alone[sid], sid
    annotated = [
        sid for sid in ids
        if batched[sid].get("bpm") is not None
        and batched[sid]["bpm"].status == "available-not-selected"
    ]
    # Presence, not absence: the per-track path being replaced existed to
    # produce exactly these rows, so a fixture without them tests nothing.
    assert annotated == [_sid(i) for i in range(0, LARGE, 2)]


def test_a_mixed_batch_resolves_mapped_and_unmapped_tracks_separately(
    state_path: Path,
) -> None:
    """One source table per mapping state: an unmapped track in the same
    batch still reads own (STANDALONE-06), a mapped one the lane default."""
    ids = [_sid(i) for i in range(SMALL)]
    conn, _statements = _traced(state_path)
    try:
        resolved = selection.Selection.resolve(conn)
        out = selection.effective_fields(
            conn, ids, resolved,
            rb_mapped={_sid(0): True, _sid(1): False, _sid(2): True, _sid(3): False},
        )
    finally:
        conn.close()
    assert out[_sid(0)]["bpm"].status == "available-not-selected"
    # Unmapped, no own record: own is effective and there is no row for it.
    assert out[_sid(1)]["bpm"].status == "missing"
    # Unmapped WITH an own record would read it; _sid(2) is mapped, so rbx.
    assert out[_sid(2)]["bpm"].status == "available-not-selected"
    assert out[_sid(3)]["bpm"].status == "missing"
