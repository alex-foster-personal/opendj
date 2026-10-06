"""What the analysis store already holds, per lane, for the ahead-of-time drain.

Pure reads of ``analysis`` at each lane's CURRENT producer version: the ids a
lane has produced (``done_ids``) and, of those, the ones whose lane block
declined as low-confidence (``declined_ids``). Split from
:mod:`apps.webui.server.ahead_analysis` so the drain module holds policy and
the loop, not SQL.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable


def done_ids(conn_factory: Callable[[], sqlite3.Connection], backend: str) -> set[str]:
    version = producer_version(backend)
    conn = conn_factory()
    try:
        rows = conn.execute(
            "SELECT DISTINCT stable_id FROM analysis WHERE backend = ? AND backend_version = ?",
            (backend, version),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return set()
        raise
    finally:
        conn.close()
    return {row[0] for row in rows}


def declined_ids(
    conn_factory: Callable[[], sqlite3.Connection], lane: str, backend: str
) -> dict[str, str]:
    version = producer_version(backend)
    conn = conn_factory()
    try:
        rows = conn.execute(
            "SELECT stable_id, json_extract(record_json, '$.lanes.' || ? || '.reason') FROM analysis "
            "WHERE backend = ? AND backend_version = ? "
            "AND json_extract(record_json, '$.lanes.' || ? || '.status') = 'failed'",
            (lane, backend, version, lane),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return {}
        raise
    finally:
        conn.close()
    return {row[0]: str(row[1]) for row in rows}


def library_value_sources(conn_factory: Callable[[], sqlite3.Connection], field: str) -> dict[str, str]:
    """stable_id -> source for every live ``track_fields`` row holding a REAL
    value of ``field``: a BPM above zero, a non-empty key. rekordbox writes
    ``0.0`` for a track it never analysed, which is no BPM at all."""
    conn = conn_factory()
    try:
        rows = conn.execute(
            "SELECT stable_id, source, value_json FROM track_fields "
            "WHERE field_name = ? AND deleted_at IS NULL",
            (field,),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return {}
        raise
    finally:
        conn.close()
    return {str(sid): str(source) for sid, source, raw in rows if _is_real_value(raw)}


def _is_real_value(raw: object) -> bool:
    try:
        value = json.loads(str(raw))
    except ValueError:
        return False
    if isinstance(value, bool):
        return False
    if isinstance(value, int | float):
        return value > 0
    return isinstance(value, str) and value.strip() != ""


def producer_version(backend: str) -> str:
    """The CURRENT producer version, from each lane's light version module
    (importing the backend itself would pull model code into the engine)."""
    from apps.analysis_beatgrid import version as beatgrid_version
    from apps.analysis_key import version as key_version
    from apps.analysis_loudness import backfill as loudness_backfill
    from apps.analysis_waveform import version as waveform_version

    versions = {
        "own_loudness.backfill": loudness_backfill.PRODUCER_VERSION,
        "own_waveform.backfill": waveform_version.PRODUCER_VERSION,
        "own_beatgrid.backfill": beatgrid_version.PRODUCER_VERSION,
        "own_key.backfill": key_version.PRODUCER_VERSION,
    }
    if backend not in versions:
        raise ValueError(f"ahead analysis has no producer version for {backend!r}")
    return versions[backend]


__all__ = ["declined_ids", "done_ids", "library_value_sources", "producer_version"]
