"""A key backfill run writes NOTHING into `track_fields` or its history.

Spec section 3 ("Consumer, key and loudness lanes"): "Source selection is
READ-TIME and never writes `track_fields`: no producer, no toggle and no
promotion ever calls `set_field` for an own value, so nothing from own analysis
enters `track_field_history` or the sync path". Section 8's regression line:
"if any own producer, toggle or promotion writes an own value into track_fields
(or field history) then broken".

Measured as a DELTA across a real backfill run rather than as an assertion
about an empty table: an empty-table assertion passes for a run that wrote
nothing because it analyzed nothing, which is the failure mode this test exists
to catch. The run here is the real producer over real audio.

  [if] a key backfill run writes into track_fields or history [then] broken, [else stop]

-Claude
"""
from __future__ import annotations

import math
import wave
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from apps.analysis import selection
from apps.analysis.backends.own_key import OwnKeyBackfillBackend
from apps.analysis.store import open_conn, upsert_record
from apps.shared.state import provenance

pytestmark = pytest.mark.requirement("NATIVE-04")

REKORDBOX_KEY = "5A"
STAMP = "2026-09-10T00:00:00Z"


@pytest.fixture(autouse=True)
def _launch_state_toggles() -> Any:
    selection.reset_toggles()
    yield
    selection.reset_toggles()


@pytest.fixture
def state_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "state" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    open_conn(db_path).close()
    return db_path


def _write_a_minor_wav(path: Path, *, seconds: float = 24.0, sr: int = 44100) -> None:
    """A minor's tonic triad: root 9, minor third 0, fifth 4."""
    timeline = np.arange(int(seconds * sr), dtype=np.float64) / sr
    samples = np.zeros_like(timeline)
    for pitch_class in (9, 0, 4):
        frequency = 440.0 * 2.0 ** ((pitch_class - 9) / 12.0)
        samples += np.sin(2.0 * math.pi * frequency * timeline)
    samples = samples / (np.max(np.abs(samples)) + 1e-9) * 0.5
    pcm = (samples * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sr)
        handle.writeframes(pcm.tobytes())


def _seed_track(conn: Any, stable_id: str) -> None:
    """The `tracks` row `track_fields.stable_id` REFERENCES.

    `_seed_rekordbox_key` goes through the real provenance writer, and that
    table carries a real foreign key, so a field cannot be seeded for a track
    the state layer does not know about.
    """
    conn.execute(
        "INSERT OR IGNORE INTO tracks (stable_id, stable_id_tier, created_at, "
        "updated_at) VALUES (?, 'inferred', ?, ?)",
        (stable_id, STAMP, STAMP),
    )
    conn.commit()


def _seed_rekordbox_key(conn: Any, stable_id: str) -> None:
    _seed_track(conn, stable_id)
    provenance.write_field(
        conn,
        stable_id=stable_id,
        field_name="key",
        value=REKORDBOX_KEY,
        source="rekordbox",
        modified_at=STAMP,
    )


def _count(conn: Any, table: str) -> int:
    return int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])


def _own_key_rows(conn: Any) -> list[tuple[str, str]]:
    """Every `track_fields` row (field, source) the OWN path could have written."""
    return list(
        conn.execute(
            "SELECT field_name, source FROM track_fields WHERE field_name = 'key'"
        ).fetchall()
    )


#-----------------------------------------------------------------------------
# the delta
#-----------------------------------------------------------------------------

@pytest.mark.requires_canonical_decode
def test_a_key_backfill_run_writes_no_track_fields_and_no_history(
    tmp_path: Path, state_db: Path
) -> None:
    stable_id = "t-delta"
    audio = tmp_path / "a-minor.wav"
    _write_a_minor_wav(audio)

    conn = open_conn(state_db)
    try:
        _seed_rekordbox_key(conn, stable_id)
        fields_before = _count(conn, "track_fields")
        history_before = _count(conn, "track_field_history")
        key_rows_before = _own_key_rows(conn)

        # The run under test: the real producer over real audio, through the
        # real write path, with the lane then promoted to own so the read side
        # has something to serve.
        record = OwnKeyBackfillBackend.analyze(audio, stable_id, db_path=state_db)
        upsert_record(record, conn=conn)
        selection.set_default(conn, "key", "own")
        selection.set_toggle("key", "own")
        fields = selection.effective_fields(conn, [stable_id], selection.Selection.resolve(conn))

        assert fields[stable_id]["key"].value is not None, "the run measured no key"
        # DELTAS, not absolutes: the seeded rekordbox row is still there and is
        # the same row.
        assert _count(conn, "track_fields") == fields_before
        assert _count(conn, "track_field_history") == history_before
        assert _own_key_rows(conn) == key_rows_before
        assert [source for _field, source in key_rows_before] == ["rekordbox"]
    finally:
        conn.close()


def test_a_promotion_writes_no_track_fields_either(tmp_path: Path, state_db: Path) -> None:
    """A promotion is a persisted default, not a library write."""
    stable_id = "t-promo"
    conn = open_conn(state_db)
    try:
        _seed_rekordbox_key(conn, stable_id)
        before = _count(conn, "track_fields")
        history_before = _count(conn, "track_field_history")
        selection.set_default(conn, "key", "own")
        assert selection.effective_source(conn, "key") == "own"
        assert _count(conn, "track_fields") == before
        assert _count(conn, "track_field_history") == history_before
    finally:
        conn.close()


#-----------------------------------------------------------------------------
# the two readers agree
#-----------------------------------------------------------------------------

@pytest.mark.requires_canonical_decode
def test_the_track_list_and_the_smartlist_column_agree_on_the_effective_key(
    tmp_path: Path, state_db: Path
) -> None:
    stable_id = "t-readers"
    audio = tmp_path / "a-minor.wav"
    _write_a_minor_wav(audio)

    conn = open_conn(state_db)
    try:
        _seed_rekordbox_key(conn, stable_id)
        selection.set_default(conn, "key", "own")
        record = OwnKeyBackfillBackend.analyze(audio, stable_id, db_path=state_db)
        upsert_record(record, conn=conn)

        # Reader 1: the row fetch every track-list caller uses.
        fetched = selection.effective_fields(
            conn, [stable_id], selection.Selection.resolve(conn)
        )[stable_id]["key"].value

        # Reader 2: the SQL the smartlist evaluator builds, over the same
        # tracks table. Not a re-implementation: the same function.
        column = selection.field_column_sql(
            "key", selection.Selection.resolve(conn)
        )
        queried = conn.execute(
            f"SELECT {column} FROM tracks WHERE stable_id = ?", (stable_id,)
        ).fetchone()[0]

        assert fetched is not None
        assert queried == fetched
    finally:
        conn.close()


def test_a_key_change_count_filter_reads_the_projection_not_the_cli(
    state_db: Path,
) -> None:
    conn = open_conn(state_db)
    try:
        selection.set_default(conn, "key", "own")
        selection.Selection.resolve(conn)  # creates no tables; projection may be absent
        sql = selection.field_column_sql("key_change_count", selection.Selection.resolve(conn))
        assert "analysis_projection" in sql
        assert "track_fields" not in sql
    finally:
        conn.close()


def test_under_rbx_the_same_filter_reads_nothing_for_a_field_rekordbox_lacks(
    state_db: Path,
) -> None:
    """A filter on a field rekordbox cannot serve matches NOTHING, rather than
    the wrong column: literal NULL, not a silent fall-back to another table."""
    conn = open_conn(state_db)
    try:
        sql = selection.field_column_sql("key_change_count", selection.Selection.all_rbx())
        assert sql == "NULL"
    finally:
        conn.close()
