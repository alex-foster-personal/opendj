"""Read-only access to the own-lane records the `/anlz` overlays serve.

One home for the four steps every own-lane overlay performs identically: open
the state DB read-only (or find that there is none), look a table up, resolve
the lane's effective source, and pull the canonical lane block for a track.
`own_beatgrid_overlay.py` carries private twins of the first two; they are left
alone here rather than refactored, so this change stays inside the key lane's
own files, and a later pass can point that module at these instead.

WHAT `None` MEANS, everywhere in this module: there is no own record, which is
a STATE with a correct answer (`status: missing`, or rekordbox as the effective
source) rather than a failure to paper over. What is NOT `None` is corruption
-- a canonical pointer naming a row that is not there, or a row that does not
carry the lane its backend named -- which raises, because serving `missing` for
a database that disagrees with itself would hide the disagreement.

-Claude
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

SOURCE_OWN = "own"
SOURCE_REKORDBOX = "rekordbox"


def state_conn_ro(state_db_path: Path | None = None) -> Any:
    """A read-only state connection, or None when there is no state DB at all.

    `state_db_path` defaults to the process-global `config.STATE_DB` so
    existing callers and tests (which monkeypatch that constant) are
    unaffected; a caller with access to `request.app.state.analysis_db_path`
    passes it through explicitly instead, so the overlay reads the same
    database the selection toggle was read from (Codex P2 BLOCKING, PR #1587).
    """
    from apps.adapters.rekordbox import config

    db_path = state_db_path if state_db_path is not None else config.STATE_DB
    if not db_path.exists():
        return None
    from apps.adapters.rekordbox.errors import _open_ro

    return _open_ro(db_path, "STATE_DB")


def table_present(conn: Any, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def effective_lane_source(
    conn: Any,
    lane: str,
    *,
    has_rb_mapping: bool | None = None,
) -> str:
    """`rbx` or `own` for one lane, with or without a state DB.

    With a connection this is `apps.analysis.selection.effective_source`
    verbatim. Without one there is no `analysis_source_default` table to read,
    which is the same state `get_default` answers `DEFAULT_SOURCE` for, so the
    in-memory toggle is the only input left. Spelled out rather than routed
    through a fabricated connection so the no-database case is visible.

    When ``has_rb_mapping`` is set, uses :func:`selection.effective_source_for_track`.
    """
    from apps.analysis import selection

    if conn is not None:
        if has_rb_mapping is not None:
            return selection.effective_source_for_track(
                conn, lane, has_rb_mapping=has_rb_mapping
            )
        return selection.effective_source(conn, lane)
    toggle = selection.get_toggle(lane)
    if toggle != "unset":
        return toggle
    if has_rb_mapping is False:
        return "own"
    return selection.DEFAULT_SOURCE


def canonical_lane_result(conn: Any, stable_id: str, lane: str) -> Any:
    """The canonical own lane block for this track, or None when there is none.

    `analysis_canonical` absent entirely is None rather than an error: it is
    created by the same idempotent `_ensure_analysis_tables` call as
    `analysis_source_default` (apps/analysis/store.py), so the two tables are
    always present or always absent together, and "no lane was ever promoted"
    is a state with a correct answer (the reasoning is
    `apps.analysis.selection.get_default`'s, applied to its sibling table).
    """
    if conn is None or not table_present(conn, "analysis_canonical"):
        return None
    from apps.analysis.canonical import canonical_pointer
    from apps.analysis.record import AnalysisRecord

    pointer = canonical_pointer(conn, stable_id, lane)
    if pointer is None:
        return None
    row = conn.execute(
        "SELECT record_json FROM analysis "
        "WHERE stable_id = ? AND backend = ? AND backend_version = ?",
        (stable_id, pointer[0], pointer[1]),
    ).fetchone()
    if row is None:
        raise RuntimeError(
            f"canonical {lane} pointer for {stable_id} names {pointer[0]}@"
            f"{pointer[1]} but no such analysis row exists"
        )
    result = AnalysisRecord.from_json(row[0]).lanes.get(lane)
    if result is None:
        # `_eligible_rows` only ever points a lane's canonical pointer at a row
        # whose backend was parsed as THAT lane, so a pointed-to record missing
        # its own named lane is the schema disagreeing with itself, not an
        # ordinary "not analyzed yet" state.
        raise RuntimeError(
            f"canonical {lane} record {pointer[0]}@{pointer[1]} for {stable_id} "
            f"carries no {lane!r} lane"
        )
    return result


__all__ = [
    "SOURCE_OWN",
    "SOURCE_REKORDBOX",
    "canonical_lane_result",
    "effective_lane_source",
    "state_conn_ro",
    "table_present",
]
