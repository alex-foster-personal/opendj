"""Stored beatgrid-quality verdicts (GRIDFLAG-02): one row per track.

A sidecar sqlite file next to state.db (`<state dir>/grid-quality.db`). It is
DERIVED data: every row can be rebuilt from the grid it describes, so it is
deliberately not in state.db (no migration, never synced, safe to delete).

Why it exists: classifying a grid means parsing a rekordbox ANLZ file (about
10 ms each, measured over 9,659 files on Thu 1 Oct 2026). A track listing
must never pay that per row, so the scan (`grid_quality_scan`) pays it once
per grid version and listings read the verdict back with one bulk query.

A row is current while its `grid_version` (what the grid was) and
`rule_version` (what the rule was) both still match. A track with no row has
not been scanned: that is `unknown`, never `ok`.

`config.STATE_DB` is read at call time, never imported by value: it is a
rebindable override (see `apps/adapters/rekordbox/config.py`).

Requirements (mini-PRD):
  ✔︎ ✅ 🎯 read_many(): bulk read, no writes.
    [if] the store file does not exist [then] every id reads as absent and
      the file is still not created ⛔️ a read creating state
    [if] 1,000 ids are asked for [then] chunked IN queries, one connection
      ⛔️ a query per id
  ✔︎ ✅ 🎯 upsert_many(): one transaction per batch.
    [if] a row exists for the id [then] it is replaced ⛔️ a duplicate row
  ✔︎ ✅ 🎯 tempo_stability table (issue #7879): the scan's tempo-stability
    verdict per track, beside its grid verdict, so library rows read it with
    one bulk query and never fetch `/anlz`.
    [if] a store predates the table [then] reads find no tempo row (blank
      column) ⛔️ a 500, or "steady" for an unscanned track

-Claude
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, cast

from apps.adapters.rekordbox import config
from apps.analysis_beatgrid.grid_quality import (
    REASON_NOT_SCANNED,
    GridClass,
    GridQuality,
    grid_quality_message,
    unknown_quality,
)
from apps.analysis_beatgrid.tempo_stability import Verdict
from apps.analysis_beatgrid.tempo_stability_label import tempo_label
from apps.webui.server.sqlite_sidecar_store import bind_upsert_many

STORE_FILENAME = "grid-quality.db"
_SQL_CHUNK = 500

_SCHEMA = """
CREATE TABLE IF NOT EXISTS grid_quality (
    stable_id             TEXT PRIMARY KEY,
    grid_source           TEXT NOT NULL,
    grid_version          TEXT NOT NULL,
    rule_version          TEXT NOT NULL,
    grid_class            TEXT NOT NULL,
    reason                TEXT,
    interval_count        INTEGER NOT NULL,
    uneven_interval_count INTEGER NOT NULL,
    worst_deviation_ms    REAL NOT NULL,
    worst_at_sec          REAL NOT NULL,
    median_bpm            REAL,
    tempo_marker_count    INTEGER,
    steady_coverage       REAL,
    steady_on_line        REAL,
    steady_line_bpm       REAL,
    computed_at           TEXT NOT NULL
)
"""


_TEMPO_SCHEMA = """
CREATE TABLE IF NOT EXISTS tempo_stability (
    stable_id          TEXT PRIMARY KEY,
    grid_version       TEXT NOT NULL,
    rule_version       TEXT NOT NULL,
    verdict            TEXT,
    nominal_bpm        REAL,
    largest_change_pct REAL,
    change_count       INTEGER,
    segments_json      TEXT NOT NULL,
    computed_at        TEXT NOT NULL
)
"""


@dataclass(frozen=True)
class StoredTempoStability:
    """One stored tempo-stability verdict (issue #7879). `verdict` None means the
    grid could not be judged (no grid, unreadable, fewer than two beats): the
    column is blank, never `steady`. Field order is the table's column order."""

    stable_id: str
    grid_version: str
    rule_version: str
    verdict: str | None
    nominal_bpm: float | None
    largest_change_pct: float | None
    change_count: int | None
    #: JSON list of `TempoSegment` dicts; "[]" when steady or not judged.
    segments_json: str
    computed_at: str

    def segments(self) -> list[dict[str, Any]]:
        value = json.loads(self.segments_json)
        if not isinstance(value, list):
            raise TypeError(f"tempo_stability.segments_json for {self.stable_id} is not a list")
        return value


@dataclass(frozen=True)
class StoredGridQuality:
    """One stored verdict. Field order is the table's column order."""

    stable_id: str
    #: Which grid was judged: "rekordbox" (PQTZ) or "none" (no grid to judge).
    grid_source: str
    #: Identity of the grid that was judged; a different value means re-judge.
    grid_version: str
    rule_version: str
    grid_class: GridClass
    reason: str | None
    interval_count: int
    uneven_interval_count: int
    worst_deviation_ms: float
    worst_at_sec: float
    median_bpm: float | None
    tempo_marker_count: int | None
    steady_coverage: float | None
    steady_on_line: float | None
    steady_line_bpm: float | None
    computed_at: str

    def quality(self) -> GridQuality:
        return GridQuality(
            grid_class=self.grid_class,
            reason=self.reason,
            interval_count=self.interval_count,
            uneven_interval_count=self.uneven_interval_count,
            worst_deviation_ms=self.worst_deviation_ms,
            worst_at_sec=self.worst_at_sec,
            median_bpm=self.median_bpm,
            tempo_marker_count=self.tempo_marker_count,
            steady_coverage=self.steady_coverage,
            steady_on_line=self.steady_on_line,
            steady_line_bpm=self.steady_line_bpm,
        )


_COLUMNS = tuple(field.name for field in fields(StoredGridQuality))
_SELECT = f"SELECT {', '.join(_COLUMNS)} FROM grid_quality"


def store_path() -> Path:
    return config.STATE_DB.parent / STORE_FILENAME


def _chunked(ids: Sequence[str]) -> Iterator[Sequence[str]]:
    for start in range(0, len(ids), _SQL_CHUNK):
        yield ids[start : start + _SQL_CHUNK]


def _has_table(conn: sqlite3.Connection, table: str = "grid_quality") -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def read_many(stable_ids: Sequence[str]) -> dict[str, StoredGridQuality]:
    """Stored verdicts for `stable_ids`; ids never scanned are absent.

    The sidecar file is shared with `grid_audio_fit_store`, so on a fresh data dir the
    grid-vs-audio scan can create it before any grid verdict is written: the file exists
    with no `grid_quality` table yet, which is the same empty state as no file at all.
    Without this guard the library listing failed 500 (`no such table: grid_quality`) on
    every fresh CI runner (PR #7948, Fri 30 Oct 2026).
    """
    ids = list(dict.fromkeys(stable_ids))
    path = store_path()
    if not ids or not path.is_file():
        return {}
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        if not _has_table(conn):
            return {}
        found: dict[str, StoredGridQuality] = {}
        for chunk in _chunked(ids):
            placeholders = ",".join("?" * len(chunk))
            for row in conn.execute(
                f"{_SELECT} WHERE stable_id IN ({placeholders})", tuple(chunk)
            ):
                stored = StoredGridQuality(*row)
                found[stored.stable_id] = stored
        return found
    finally:
        conn.close()


upsert_many: Callable[[Sequence[StoredGridQuality]], None] = bind_upsert_many(
    store_path, _SCHEMA, "grid_quality", _COLUMNS
)

_TEMPO_COLUMNS = tuple(field.name for field in fields(StoredTempoStability))
_TEMPO_SELECT = f"SELECT {', '.join(_TEMPO_COLUMNS)} FROM tempo_stability"

upsert_tempo_many: Callable[[Sequence[StoredTempoStability]], None] = bind_upsert_many(
    store_path, _TEMPO_SCHEMA, "tempo_stability", _TEMPO_COLUMNS
)


def read_tempo_many(stable_ids: Sequence[str]) -> dict[str, StoredTempoStability]:
    """Stored tempo verdicts for `stable_ids`; ids never scanned are absent."""
    ids = list(dict.fromkeys(stable_ids))
    path = store_path()
    if not ids or not path.is_file():
        return {}
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        if not _has_table(conn, "tempo_stability"):
            return {}
        found: dict[str, StoredTempoStability] = {}
        for chunk in _chunked(ids):
            placeholders = ",".join("?" * len(chunk))
            for row in conn.execute(f"{_TEMPO_SELECT} WHERE stable_id IN ({placeholders})", tuple(chunk)):
                stored = StoredTempoStability(*row)
                found[stored.stable_id] = stored
        return found
    finally:
        conn.close()


def tempo_row_payload(stored: StoredTempoStability | None, grid_message: str) -> dict[str, Any] | None:
    """The BPM Stability column's block on a track row (issue #7879), or None
    when the track's tempo was never judged (the column is blank). Pure
    formatting of the stored verdict; segments ride row-assets, not here."""
    if stored is None or stored.verdict is None:
        return None
    if stored.largest_change_pct is None or stored.change_count is None:
        raise ValueError(f"tempo verdict for {stored.stable_id} lacks its numbers")
    verdict = cast(Verdict, stored.verdict)
    label = tempo_label(
        verdict,
        stored.largest_change_pct,
        stored.change_count,
        [segment["kind"] for segment in stored.segments()],
        grid_message,
    )
    return {
        "verdict": verdict,
        "largest_change_pct": stored.largest_change_pct,
        "change_count": stored.change_count,
        "text": label.text,
        "tone": label.tone,
        "title": label.title,
    }


def tempo_curve_payload(stored: StoredTempoStability | None) -> dict[str, Any] | None:
    """The row strip's tempo curve (row-assets `tempo`): None when not judged or
    steady, so a steady track paints nothing."""
    if stored is None or stored.verdict is None or stored.verdict == "steady":
        return None
    return {
        "verdict": stored.verdict,
        "nominal_bpm": stored.nominal_bpm,
        "largest_change_pct": stored.largest_change_pct,
        "segments": stored.segments(),
    }


def row_payload(stored: StoredGridQuality | None, *, dismissed: bool) -> dict[str, Any]:
    """The `grid_quality` block a track row carries. Pure formatting of the
    stored verdict: never parses a grid and never runs the rule.

    `message` is None for an `ok` grid (nothing to say on thousands of rows);
    every other class carries the same sentence the deck badge shows."""
    quality = stored.quality() if stored is not None else unknown_quality(REASON_NOT_SCANNED)
    return {
        "grid_class": quality.grid_class,
        "reason": quality.reason,
        "dismissed": dismissed,
        "message": None if quality.grid_class == "ok" else grid_quality_message(quality),
    }


__all__ = [
    "STORE_FILENAME",
    "StoredGridQuality",
    "StoredTempoStability",
    "read_many",
    "read_tempo_many",
    "row_payload",
    "store_path",
    "tempo_curve_payload",
    "tempo_row_payload",
    "upsert_many",
    "upsert_tempo_many",
]
