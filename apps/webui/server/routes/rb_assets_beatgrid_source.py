"""Per-track beatgrid source for ``/anlz``, split from rb_assets (quality file-size ratchet)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import HTTPException, Request

from apps.analysis import canonical, selection
from apps.analysis.record import AnalysisRecord

from . import analysis as analysis_routes
from . import analysis_source as analysis_source_routes

# The `/anlz` wire contract's `beatgrid_source` predates PARITY-02's lane
# selection module and uses "rekordbox"/"own"; `apps.analysis.selection`
# uses "rbx"/"own". Translate at the boundary rather than widen the wire
# vocabulary to match an internal module.
_BEATGRID_SOURCE_LABELS: dict[str, str] = {"rbx": "rekordbox", "own": "own"}


# `beatgrid_source_basis` on `/anlz`: WHY this track resolved to the source
# `beatgrid_source` names. "unmapped-default" is STANDALONE-06's per-track own
# (an unmapped track, the PARITY-02 toggle unset, the lane default still rbx),
# which legitimately differs from the lane-wide selection GET
# /api/v1/analysis/source reports. The client compares every payload against
# that lane-wide selection to reject stragglers from a source switch, so
# without this field it read every unmapped track as a straggler and refetched
# it forever (deck load never settled, trunk red Fri 25 Sep 2026).
BASIS_SELECTION = "selection"
BASIS_UNMAPPED_DEFAULT = "unmapped-default"


def beatgrid_source_for_track(request: Request, *, has_rb_mapping: bool) -> tuple[str, str]:
    """Per-track beatgrid source label and its basis for ``/anlz`` (STANDALONE-06).

    Reads through :mod:`apps.analysis.selection` once per request, source and
    basis from ONE toggle read and ONE default read so they cannot straddle a
    PUT; callers thread the returned pair through rescue branches and
    ``rb_assets._resolve_beatgrid_source`` without re-reading (ADR-0099).
    """
    conn = analysis_source_routes._open_ro(request)
    try:
        source, implicit = selection.effective_source_and_implicit_own_for_track(
            conn, "beatgrid", has_rb_mapping=has_rb_mapping
        )
    finally:
        conn.close()
    basis = BASIS_UNMAPPED_DEFAULT if implicit else BASIS_SELECTION
    return _BEATGRID_SOURCE_LABELS[source], basis


def phrase_source_for_request(request: Request) -> str:
    """The PARITY-02 ``phrase`` lane's effective source, ``rbx`` or ``own`` (STRUCT-04).

    Lane-wide, not per track: own sections come from a machine-local sidecar
    that exists or not regardless of rekordbox mapping, so STANDALONE-06's
    unmapped-track default does not apply here.
    """
    conn = analysis_source_routes._open_ro(request)
    try:
        return selection.effective_source(conn, "phrase")
    finally:
        conn.close()


def _canonical_beatgrid_record(db_path: Path, stable_id: str) -> AnalysisRecord | None:
    """The analysis row ``analysis_canonical`` names for this track's beatgrid.

    THE POINTER, NOT THE NEWEST ROW. ``_load_latest_record(.., None)``
    cannot answer this: its default query excludes every ``own_*`` backend
    on purpose (``analysis.py``), so it returns a legacy librosa row and the
    OWN grid would be paired with the canonical record's BPM -- because
    ``GET /tracks/{id}`` reads ``bpm`` from ``analysis_projection``, which
    :func:`apps.analysis.canonical.rebuild_projection` derives from THIS
    pointer. Two readers of one lane must resolve the same record or the
    read model reports a grid and a tempo that were never measured together
    (discussion_r3969942709 P1 BLOCKING).

    Returns None when the track has no canonical own beatgrid (no pointer,
    or the analysis tables have never been created). A pointer that names a
    row which is not there is corruption, not an absence, and raises.
    """
    conn = analysis_routes._open_analysis_ro(db_path)
    try:
        try:
            pointer = canonical.canonical_pointer(conn, stable_id, "beatgrid")
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return None
            raise
        if pointer is None:
            return None
        backend, backend_version = pointer
        row = conn.execute(
            "SELECT record_json FROM analysis WHERE stable_id = ? AND backend = ? AND backend_version = ?",
            (stable_id, backend, backend_version),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "ANALYSIS_CANONICAL_DANGLING",
                "message": (
                    f"canonical beatgrid pointer for {stable_id!r} names "
                    f"{backend!r}@{backend_version!r} but no such analysis row exists"
                ),
            },
        )
    return AnalysisRecord.from_json(row[0])
