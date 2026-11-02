"""Stored grid-vs-audio verdicts (GRIDFIT-02): one row per track.

A second table in the grid-quality sidecar (`<state dir>/grid-quality.db`),
for the same reasons that file exists: DERIVED data, rebuildable from the grid
and the audio it describes, never synced, safe to delete. Separate from the
`grid_quality` table on purpose (ADR-NEW-grid-audio-fit): a grid's shape and
its fit to the audio are different questions with different rule versions.

A row is current while `grid_version`, `audio_version` and `rule_version` all
still match. A track with no row has not been judged; that is `not_scanned`,
never `fit`.

Requirements (mini-PRD):
  ✔︎ ✅ 🎯 read_many(): bulk read, never creates the file.
  ✔︎ ✅ 🎯 upsert_many(): one transaction, a re-judged track replaces its row.
  ✔︎ ✅ 🎯 row_payload(): the API block, with the deck sentence for warnings.
    [if] wrong_bpm [then] names the grid and audio tempo ⛔️ a bare label
    [if] never judged [then] verdict not_scanned ⛔️ fit

-Claude
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import astuple, dataclass, fields
from pathlib import Path
from typing import Any

from apps.adapters.rekordbox import config
from apps.analysis_beatgrid.grid_audio_fit import DECK_WARNING_VERDICTS
from apps.webui.server.grid_quality_store import STORE_FILENAME
from apps.webui.server.sqlite_sidecar_store import upsert_many_sidecar

NOT_SCANNED = "not_scanned"
_SQL_CHUNK = 500

_SCHEMA = """
CREATE TABLE IF NOT EXISTS grid_audio_fit (
    stable_id             TEXT PRIMARY KEY,
    grid_version          TEXT NOT NULL,
    audio_version         TEXT NOT NULL,
    rule_version          TEXT NOT NULL,
    verdict               TEXT NOT NULL,
    reason                TEXT,
    grid_bpm              REAL,
    audio_bpm             REAL,
    fold_sharpness_audio  REAL,
    fold_sharpness_grid   REAL,
    kick_offset_ms        REAL,
    kick_spread_ms        REAL,
    kick_coverage         REAL,
    onbeat_ratio          REAL,
    computed_at           TEXT NOT NULL
)
"""


@dataclass(frozen=True)
class StoredGridAudioFit:
    """One stored verdict. Field order is the table's column order."""

    stable_id: str
    grid_version: str
    audio_version: str
    rule_version: str
    #: A `Verdict`, or "unknown" when there was nothing to judge (see `reason`).
    verdict: str
    reason: str | None
    grid_bpm: float | None
    audio_bpm: float | None
    fold_sharpness_audio: float | None
    fold_sharpness_grid: float | None
    kick_offset_ms: float | None
    kick_spread_ms: float | None
    kick_coverage: float | None
    onbeat_ratio: float | None
    computed_at: str


_COLUMNS = tuple(field.name for field in fields(StoredGridAudioFit))
_SELECT = f"SELECT {', '.join(_COLUMNS)} FROM grid_audio_fit"


def store_path() -> Path:
    return config.STATE_DB.parent / STORE_FILENAME


def _chunked(ids: Sequence[str]) -> Iterator[Sequence[str]]:
    for start in range(0, len(ids), _SQL_CHUNK):
        yield ids[start : start + _SQL_CHUNK]


def _has_table(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='grid_audio_fit'").fetchone() is not None
    )


def read_many(stable_ids: Sequence[str]) -> dict[str, StoredGridAudioFit]:
    """Stored verdicts for `stable_ids`; ids never judged are absent."""
    ids = list(dict.fromkeys(stable_ids))
    path = store_path()
    if not ids or not path.is_file():
        return {}
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        if not _has_table(conn):
            return {}
        found: dict[str, StoredGridAudioFit] = {}
        for chunk in _chunked(ids):
            placeholders = ",".join("?" * len(chunk))
            for row in conn.execute(f"{_SELECT} WHERE stable_id IN ({placeholders})", tuple(chunk)):
                stored = StoredGridAudioFit(*row)
                found[stored.stable_id] = stored
        return found
    finally:
        conn.close()


def read_by_verdict(verdicts: Sequence[str]) -> list[StoredGridAudioFit]:
    """Every stored row whose verdict is one of `verdicts`."""
    path = store_path()
    if not verdicts or not path.is_file():
        return []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        if not _has_table(conn):
            return []
        placeholders = ",".join("?" * len(verdicts))
        return [
            StoredGridAudioFit(*row)
            for row in conn.execute(f"{_SELECT} WHERE verdict IN ({placeholders}) ORDER BY stable_id", tuple(verdicts))
        ]
    finally:
        conn.close()


def upsert_many(rows: Sequence[StoredGridAudioFit]) -> None:
    """Replace the stored verdict for each row's track, in one transaction."""
    if not rows:
        return
    upsert_many_sidecar(
        store_path(),
        _SCHEMA,
        "grid_audio_fit",
        _COLUMNS,
        [astuple(row) for row in rows],
    )


def message(stored: StoredGridAudioFit) -> str | None:
    """The sentence a deck or the library shows; None when there is nothing to say."""
    verdict = stored.verdict
    if verdict == "wrong_bpm" and stored.grid_bpm is not None and stored.audio_bpm is not None:
        return (
            f"Beatgrid tempo {stored.grid_bpm:.2f} BPM does not match the audio "
            f"(about {stored.audio_bpm:.2f} BPM): Beat Sync will drift on this track."
        )
    if verdict == "off_beat":
        return "Beatgrid sits on the off-beats, not the kicks: Beat Sync will lock half a beat out."
    if verdict == "shifted" and stored.kick_offset_ms is not None:
        return f"Beatgrid is offset from the kicks by about {abs(stored.kick_offset_ms):.0f} ms."
    return None


def row_payload(stored: StoredGridAudioFit | None) -> dict[str, Any]:
    """The `grid_audio_fit` block of the API. Pure formatting of the stored row."""
    if stored is None:
        return {"verdict": NOT_SCANNED, "reason": None, "deck_warning": False, "message": None}
    verdict: str = stored.verdict
    return {
        "verdict": verdict,
        "reason": stored.reason,
        "deck_warning": verdict in DECK_WARNING_VERDICTS,
        "message": message(stored),
        "grid_bpm": stored.grid_bpm,
        "audio_bpm": stored.audio_bpm,
        "kick_offset_ms": stored.kick_offset_ms,
        "kick_spread_ms": stored.kick_spread_ms,
        "kick_coverage": stored.kick_coverage,
        "rule_version": stored.rule_version,
        "computed_at": stored.computed_at,
    }


__all__ = [
    "NOT_SCANNED",
    "StoredGridAudioFit",
    "message",
    "read_by_verdict",
    "read_many",
    "row_payload",
    "store_path",
    "upsert_many",
]
