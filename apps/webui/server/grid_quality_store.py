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

-Claude
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import astuple, dataclass, fields
from pathlib import Path
from typing import Any

from apps.adapters.rekordbox import config
from apps.analysis_beatgrid.grid_quality import (
    REASON_NOT_SCANNED,
    GridClass,
    GridQuality,
    grid_quality_message,
    unknown_quality,
)

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


def read_many(stable_ids: Sequence[str]) -> dict[str, StoredGridQuality]:
    """Stored verdicts for `stable_ids`; ids never scanned are absent."""
    ids = list(dict.fromkeys(stable_ids))
    path = store_path()
    if not ids or not path.is_file():
        return {}
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
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


def upsert_many(rows: Sequence[StoredGridQuality]) -> None:
    """Replace the stored verdict for each row's track, in one transaction."""
    if not rows:
        return
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        with conn:
            conn.execute(_SCHEMA)
            conn.executemany(
                f"INSERT OR REPLACE INTO grid_quality ({', '.join(_COLUMNS)}) "
                f"VALUES ({', '.join('?' * len(_COLUMNS))})",
                [astuple(row) for row in rows],
            )
    finally:
        conn.close()


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
    "read_many",
    "row_payload",
    "store_path",
    "upsert_many",
]
