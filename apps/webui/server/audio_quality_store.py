"""Stored audio-quality scan facts (QLTSCAN-03): one row per track.

A sidecar sqlite file next to state.db (`<state dir>/audio-quality.db`), the
same shape as `grid_quality_store`: DERIVED data, rebuilt from the audio file
it describes, so it is deliberately not in state.db (no migration, never
synced, safe to delete; deleting it just makes every letter hollow until the
next scan).

Why it exists: measuring a file means decoding all of it (about 2.5 s for a
6-minute track on nucbox). A listing never pays that; the background scan
pays it once per file version, and rows read the facts back in one bulk query.

A row is current while its `rule_version` (the scanner's thresholds) matches
and its file is the same one. The scan compares path, mtime and size; a
listing, which already has the size from its availability probe and must not
stat again, compares the size only.

MINI-PRD
--------
  ✔︎ ✅ 🎯 read_many(): bulk read, never creates the file.
    [if] the store does not exist [then] every id is absent and no file is made
  ✔︎ ✅ 🎯 upsert_many(): one transaction per batch, replaces by stable_id.
  ✔︎ ✅ 🎯 current_facts(): facts only when rule, path and size still match.
    [if] the scanner rule changed [then] None ⛔️ stale facts read as checked

-Claude
"""
from __future__ import annotations

import json
import os.path
import sqlite3
import unicodedata
from collections.abc import Iterator, Sequence
from dataclasses import astuple, dataclass, fields
from pathlib import Path
from typing import Any

from apps.shared import audio_grade
from apps.shared.audio_scan import rule_version
from apps.webui.server.sqlite_sidecar_store import upsert_many_sidecar

STORE_FILENAME = "audio-quality.db"
_SQL_CHUNK = 500

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audio_quality (
    stable_id     TEXT PRIMARY KEY,
    file_path     TEXT NOT NULL,
    file_version  TEXT NOT NULL,
    file_size     INTEGER NOT NULL,
    rule_version  TEXT NOT NULL,
    facts_json    TEXT,
    error         TEXT,
    computed_at   TEXT NOT NULL
)
"""


@dataclass(frozen=True)
class StoredScan:
    """One stored scan. Exactly one of `facts_json` / `error` is set."""

    stable_id: str
    file_path: str
    #: `<sha16 of path>:<mtime_ns>:<size>`; a different value means re-scan.
    file_version: str
    file_size: int
    rule_version: str
    facts_json: str | None
    error: str | None
    computed_at: str

    def facts(self) -> dict[str, Any] | None:
        return json.loads(self.facts_json) if self.facts_json else None


_COLUMNS = tuple(field.name for field in fields(StoredScan))
_SELECT = f"SELECT {', '.join(_COLUMNS)} FROM audio_quality"


def store_path(state_db: Path) -> Path:
    """The sidecar beside the app's own `state_db` (never a process global:
    an app serving another library must read and write that library's facts)."""
    return state_db.parent / STORE_FILENAME


def _chunked(ids: Sequence[str]) -> Iterator[Sequence[str]]:
    for start in range(0, len(ids), _SQL_CHUNK):
        yield ids[start : start + _SQL_CHUNK]


def read_many(stable_ids: Sequence[str], state_db: Path) -> dict[str, StoredScan]:
    """Stored scans for `stable_ids`; ids never scanned are absent."""
    ids = list(dict.fromkeys(stable_ids))
    path = store_path(state_db)
    if not ids or not path.is_file():
        return {}
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        found: dict[str, StoredScan] = {}
        for chunk in _chunked(ids):
            placeholders = ",".join("?" * len(chunk))
            for row in conn.execute(f"{_SELECT} WHERE stable_id IN ({placeholders})", tuple(chunk)):
                stored = StoredScan(*row)
                found[stored.stable_id] = stored
        return found
    finally:
        conn.close()


def upsert_many(rows: Sequence[StoredScan], state_db: Path) -> None:
    """Replace the stored scan for each row's track, in one transaction."""
    if not rows:
        return
    upsert_many_sidecar(
        store_path(state_db),
        _SCHEMA,
        "audio_quality",
        _COLUMNS,
        [astuple(row) for row in rows],
    )


def same_path(a: str, b: str) -> bool:
    """One file path, spelled either way (NFC vs NFD, redundant separators)."""
    return unicodedata.normalize("NFC", os.path.normpath(a)) == unicodedata.normalize("NFC", os.path.normpath(b))


def current_facts(
    stored: StoredScan | None,
    size_bytes: int | None,
    file_version: str | None = None,
    path: str | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """`(facts, scan_error)`, both None when not current.

    Current means the scanner's rule is unchanged and the file is the one that
    was scanned. A caller that stat-ed the file passes its full `file_version`
    (path, mtime, size). A listing row passes its resolved `path` and the
    size from the shared availability index: a track remapped to another file,
    or a file whose size changed, is never shown as checked, and a path the
    row spells differently from the scanner errs to unchecked. The index
    records no mtime, so a same-path same-size in-place replacement still reads
    as current on the row until a scan re-measures it (issue #7584)."""
    if stored is None or stored.rule_version != rule_version():
        return None, None
    if file_version is not None and stored.file_version != file_version:
        return None, None
    if file_version is None and size_bytes is None:
        return None, None  # no size to compare: a vanished file must not keep its grade
    if size_bytes is not None and stored.file_size != size_bytes:
        return None, None
    if path is not None and not same_path(stored.file_path, path):
        return None, None
    return stored.facts(), stored.error


def attach_grades(
    quality_by_sid: dict[str, dict[str, Any]],
    size_by_sid: dict[str, int | None],
    path_by_sid: dict[str, str | None],
    state_db: Path,
) -> None:
    """Add the QLT `grade` block to each row's quality dict, in place, from
    one bulk read. Never decodes or stats anything (QLTSCAN-02). A row with
    no resolved path is never checked."""
    stored = read_many(list(quality_by_sid), state_db)
    for sid, quality in quality_by_sid.items():
        row_path = path_by_sid.get(sid)
        facts, error = (
            current_facts(stored.get(sid), size_by_sid.get(sid), path=row_path)
            if row_path
            else (None, None)
        )
        quality["grade"] = audio_grade.grade(quality, facts, error).row_dict()


__all__ = [
    "STORE_FILENAME",
    "StoredScan",
    "attach_grades",
    "current_facts",
    "read_many",
    "same_path",
    "store_path",
    "upsert_many",
]
