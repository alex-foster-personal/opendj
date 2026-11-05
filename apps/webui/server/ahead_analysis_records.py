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

from apps.analysis.canonical import dependency_stale_row
from apps.analysis.lanes import parse_own_backend
from apps.analysis.queue import LANE_DEPENDENCIES
from apps.analysis_key import key_guess
from apps.shared.state import analysis_stale


def done_ids(conn_factory: Callable[[], sqlite3.Connection], backend: str) -> set[str]:
    """Ids with a record at the lane's current version that is still current.

    A row the canonical pointer dropped because its dependency moved (a key
    computed on a beatgrid that has since been re-analyzed) is NOT done: the
    drain must re-run it (NATIVE-36). Only rows with no pointer for their
    lane are re-checked, so a library whose keys are current pays one query.
    """
    version = producer_version(backend)
    lane = _lane_of(backend)
    conn = conn_factory()
    try:
        rows = conn.execute(
            "SELECT DISTINCT stable_id FROM analysis WHERE backend = ? AND backend_version = ?",
            (backend, version),
        ).fetchall()
        done = {row[0] for row in rows}
        if lane in LANE_DEPENDENCIES and done:
            done -= _dependency_stale_ids(conn, lane, backend, version)
        if lane == "key" and done:
            # KEY-GUESS-01: a declined key saved before guesses existed runs
            # once more so it gains one; the new record carries it, so never twice.
            done -= {
                row[0] for row in conn.execute(
                    "SELECT stable_id FROM analysis WHERE backend = ? AND backend_version = ? AND "
                    + key_guess.declined_without_guess_sql(),
                    (backend, version),
                )
            }
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return set()
        raise
    finally:
        conn.close()
    return done


def _lane_of(backend: str) -> str:
    parsed = parse_own_backend(backend)
    if parsed is None:
        raise ValueError(f"ahead analysis backend {backend!r} is not an own_<lane>.<producer> name")
    return parsed.lane


def _dependency_stale_ids(
    conn: sqlite3.Connection, lane: str, backend: str, version: str
) -> set[str]:
    has_pointer_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='analysis_canonical'"
    ).fetchone()
    if has_pointer_table is None:
        return set()
    unpointed = conn.execute(
        "SELECT a.stable_id, a.record_json FROM analysis AS a "
        "LEFT JOIN analysis_canonical AS c ON c.stable_id = a.stable_id AND c.lane = ? "
        "WHERE a.backend = ? AND a.backend_version = ? AND c.stable_id IS NULL",
        (lane, backend, version),
    ).fetchall()
    return {
        str(sid) for sid, record_json in unpointed
        if dependency_stale_row(conn, str(sid), lane, record_json)
        or (backend, version) in analysis_stale.stale_rows(conn, str(sid), lane)
    }


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
    from apps.analysis_loudness import contract as loudness_contract
    from apps.analysis_waveform import version as waveform_version

    versions = {
        "own_loudness.backfill": loudness_contract.PRODUCER_VERSION,
        "own_waveform.backfill": waveform_version.PRODUCER_VERSION,
        "own_beatgrid.backfill": beatgrid_version.PRODUCER_VERSION,
        "own_key.backfill": key_version.PRODUCER_VERSION,
    }
    if backend not in versions:
        raise ValueError(f"ahead analysis has no producer version for {backend!r}")
    return versions[backend]


__all__ = ["declined_ids", "done_ids", "library_value_sources", "producer_version"]
