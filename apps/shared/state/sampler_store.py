"""Edge sampler persistence: the ``sampler.db`` sidecar (spec ``(private path)`` 3.2, 7.4).

A sidecar sqlite file next to ``state.db`` (``<state dir>/sampler.db``), the
``grid-quality.db`` sidecar pattern: per-slot names, descriptions and settings,
captures (a region plus its mix vector), the edge sets, the pinned tabs and
one-way user flags.

Why not a ``track_fields`` row in ``state.db`` like the cue store: a new field
name must be added to ``provenance.WRAPPED_FIELDS`` (shared, CloudSync-visible),
and sets have no track to hang a row on. Moving the per-track part into
``state.db`` later is one ``WRAPPED_FIELDS`` entry plus a copy of these rows.

Mini-PRD:
  ✔︎ ✅ 🎯 captures and slot meta round-trip per track
    [if] a capture is upserted [then] load_all returns it under its stable_id with its vector [else ⛔️]
    [if] a capture is deleted [then] its meta and pin go with it [else ⛔️]
    [if] meta is upserted twice [then] the second write wins [else ⛔️]
  ✔︎ ✅ 🎯 sets keep their name, description and slot order (None keeps the ranked default)
    [if] a set is written then read [then] name, description and slot_ids match [else ⛔️]
    [if] a set is written with slot_ids None [then] it reads back None [else ⛔️]
  ✔︎ ✅ 🎯 pins are idempotent and ordered by first pin
    [if] a slot is re-pinned [then] it keeps its first position [else ⛔️]
    [if] a slot is unpinned [then] it leaves the pin list [else ⛔️]
  ✔︎ ✅ 🎯 flags are one-way decisions read back by key
    [if] a flag is unset [then] read_flag is None [else ⛔️]
  ✔︎ ✅ 🎯 the file is a sidecar beside state.db, never state.db itself
    [if] the store is written [then] sampler.db exists beside state.db and state.db does not [else ⛔️]
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

SIDECAR_FILENAME: Final = "sampler.db"

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS sampler_capture (
        stable_id   TEXT NOT NULL,
        in_ms       INTEGER NOT NULL,
        out_ms      INTEGER NOT NULL,
        beats       INTEGER NOT NULL,
        vector_json TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        PRIMARY KEY (stable_id, in_ms, out_ms),
        CHECK (in_ms >= 0 AND in_ms < out_ms AND beats >= 1)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sampler_slot_meta (
        slot_id       TEXT PRIMARY KEY,
        stable_id     TEXT NOT NULL,
        name          TEXT,
        description   TEXT NOT NULL,
        settings_json TEXT,
        updated_at    TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sampler_set (
        id          TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        description TEXT NOT NULL,
        slot_ids    TEXT,
        updated_at  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sampler_pin (
        slot_id   TEXT PRIMARY KEY,
        pinned_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sampler_flag (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
)


@dataclass
class TrackSamplerData:
    """One track's captures and per-slot metadata (keyed by slot_id)."""

    captures: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, dict[str, Any]] = field(default_factory=dict)


def sidecar_path(state_db_path: Path) -> Path:
    """Path of ``sampler.db``, the sidecar beside the given ``state.db``."""
    return Path(state_db_path).parent / SIDECAR_FILENAME


@contextmanager
def _conn(state_db_path: Path) -> Iterator[sqlite3.Connection]:
    path = sidecar_path(state_db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        with conn:
            for sql in _SCHEMA:
                conn.execute(sql)
            yield conn
    finally:
        conn.close()


# ---------- slots ---------------------------------------------------------------


def load_all(state_db_path: Path) -> dict[str, TrackSamplerData]:
    """Every capture and slot meta row, grouped by the track's stable_id."""
    out: dict[str, TrackSamplerData] = {}
    with _conn(state_db_path) as conn:
        for sid, a, b, beats, vector, created in conn.execute(
            "SELECT stable_id, in_ms, out_ms, beats, vector_json, created_at FROM sampler_capture"
        ):
            out.setdefault(sid, TrackSamplerData()).captures.append(
                {"in_ms": a, "out_ms": b, "beats": beats, "vector": json.loads(vector), "created_at": created}
            )
        for slot_id, sid, name, desc, settings, updated in conn.execute(
            "SELECT slot_id, stable_id, name, description, settings_json, updated_at FROM sampler_slot_meta"
        ):
            out.setdefault(sid, TrackSamplerData()).meta[slot_id] = {
                "name": name,
                "description": desc,
                "settings": None if settings is None else json.loads(settings),
                "updated_at": updated,
            }
    return out


def upsert_capture(
    state_db_path: Path, stable_id: str, *, in_ms: int, out_ms: int, beats: int, vector: dict[str, Any], created_at: str
) -> None:
    """Save one captured region and its mix vector (same region again replaces it)."""
    with _conn(state_db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO sampler_capture VALUES (?,?,?,?,?,?)",
            (stable_id, in_ms, out_ms, beats, json.dumps(vector), created_at),
        )


def delete_capture(state_db_path: Path, stable_id: str, *, in_ms: int, out_ms: int, slot_id: str) -> None:
    """Delete one capture together with its slot meta and pin."""
    with _conn(state_db_path) as conn:
        conn.execute(
            "DELETE FROM sampler_capture WHERE stable_id = ? AND in_ms = ? AND out_ms = ?", (stable_id, in_ms, out_ms)
        )
        conn.execute("DELETE FROM sampler_slot_meta WHERE slot_id = ?", (slot_id,))
        conn.execute("DELETE FROM sampler_pin WHERE slot_id = ?", (slot_id,))


def upsert_meta(
    state_db_path: Path,
    slot_id: str,
    stable_id: str,
    *,
    name: str | None,
    description: str,
    settings: dict[str, Any] | None,
    updated_at: str,
) -> None:
    """Save one slot's name, description and settings (the last write wins)."""
    with _conn(state_db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO sampler_slot_meta VALUES (?,?,?,?,?,?)",
            (slot_id, stable_id, name, description, None if settings is None else json.dumps(settings), updated_at),
        )


# ---------- sets ----------------------------------------------------------------


def read_sets(state_db_path: Path) -> list[dict[str, Any]]:
    """Every stored set by id; ``slot_ids`` None means the ranked default order."""
    with _conn(state_db_path) as conn:
        rows = conn.execute("SELECT id, name, description, slot_ids FROM sampler_set ORDER BY id").fetchall()
    return [
        {"id": sid, "name": name, "description": desc, "slot_ids": None if ids is None else json.loads(ids)}
        for sid, name, desc, ids in rows
    ]


def write_set(
    state_db_path: Path,
    set_id: str,
    *,
    name: str,
    description: str,
    slot_ids: list[str | None] | None,
    updated_at: str,
) -> dict[str, Any]:
    """Save one set's name, description and slot order and return it as stored."""
    with _conn(state_db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO sampler_set VALUES (?,?,?,?,?)",
            (set_id, name, description, None if slot_ids is None else json.dumps(slot_ids), updated_at),
        )
    return {"id": set_id, "name": name, "description": description, "slot_ids": slot_ids}


# ---------- pins (a tab pinned out of the edge wave) --------------------------------


def read_pins(state_db_path: Path) -> list[str]:
    """Pinned slot ids, oldest pin first."""
    with _conn(state_db_path) as conn:
        rows = conn.execute("SELECT slot_id FROM sampler_pin ORDER BY pinned_at, slot_id").fetchall()
    return [str(r[0]) for r in rows]


def write_pin(state_db_path: Path, slot_id: str, *, pinned: bool, pinned_at: str) -> list[str]:
    """Pin or unpin one slot (re-pinning keeps its first pinned_at) and return all pins."""
    with _conn(state_db_path) as conn:
        if pinned:
            conn.execute("INSERT OR IGNORE INTO sampler_pin VALUES (?, ?)", (slot_id, pinned_at))
        elif not pinned:
            conn.execute("DELETE FROM sampler_pin WHERE slot_id = ?", (slot_id,))
    return read_pins(state_db_path)


# ---------- flags (one-way user decisions, e.g. the Starter set was deleted) ------------


def read_flag(state_db_path: Path, key: str) -> str | None:
    """The stored value of one flag, None when it was never set."""
    with _conn(state_db_path) as conn:
        row = conn.execute("SELECT value FROM sampler_flag WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row[0])


def write_flag(state_db_path: Path, key: str, value: str) -> None:
    """Set one flag (a later write replaces the value)."""
    with _conn(state_db_path) as conn:
        conn.execute("INSERT OR REPLACE INTO sampler_flag VALUES (?, ?)", (key, value))
