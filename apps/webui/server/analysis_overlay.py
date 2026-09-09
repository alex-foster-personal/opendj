"""The own-analysis half of the track read model.

Split out of ``sqlite_backend.py``, which is already the largest module in
this package: these two functions are one concern (how a lane's SOURCE
changes what a track row says) and neither touches SQLite'"'"'s cursor
handling, connection pooling or the EAV pass around them.

Both are called from ``_row_to_track`` / ``_fetch_fields``. Neither writes.

-Claude
"""
from __future__ import annotations

import sqlite3

from apps.analysis import lanes as analysis_lanes
from apps.analysis import selection as analysis_selection
from apps.analysis.selection import OWN_ANALYSIS_SOURCE as _OWN_ANALYSIS_SOURCE
from apps.analysis.selection import EffectiveField


def lane_owned_fields(
    conn: sqlite3.Connection, stable_ids: list[str],
) -> dict[str, dict[str, EffectiveField]]:
    """The lane-owned half, skipped only when every lane is on rbx.

    The skip is gated on the SELECTION, never on whether
    ``analysis_projection`` exists. An earlier draft gated on the table and
    was wrong in a way a live run caught and the unit tests did not: the
    promoted default lives in a DIFFERENT table
    (``analysis_source_default``), so a lane could be on own while the
    projection table was still absent, and the EAV pass then served the
    rekordbox value under an `own` selection. That is the exact silent
    substitution this milestone removes.

    Under all-rbx, :func:`effective_fields` returns the same ``track_fields``
    rows the EAV pass above already produced for ``bpm`` and ``key`` and
    nothing else, so skipping it is an optimization on the library-listing
    hot path rather than a behavior change. It is asserted as such in
    tests/webui/test_effective_fields.py.
    """
    selection = analysis_selection.Selection.resolve(conn)
    if not selection.any_own:
        return {}
    return analysis_selection.effective_fields(conn, stable_ids, selection)


def selection_tag(fields: dict[str, EffectiveField]) -> str:
    """A stable marker for WHICH SOURCE each lane-owned field came from.

    The etag is derived from timestamps, and switching a lane from rbx to own
    changes the bpm, the key and the whole provenance block WITHOUT moving
    any of them: an own projection row can easily be older than the track's
    base `updated_at`, in which case the maximum does not budge and two
    genuinely different representations share one strong validator, so a
    stale `If-Match` still passes (Codex P2, PR #1549).

    Only the SOURCE goes in, not the value: a value change already moves a
    timestamp. Empty when no lane-owned field is on own, so an all-rbx
    library keeps byte-identical etags to before this existed.
    """
    own = sorted(
        f"{name}={view.source}"
        for name, view in fields.items()
        if name in analysis_selection.PROJECTION_FIELDS
        and view.source != "rekordbox"
        and (
            view.source.startswith(analysis_lanes.OWN_BACKEND_PREFIX)
            or view.source == _OWN_ANALYSIS_SOURCE
        )
    )
    return "|".join(own)


__all__ = ["lane_owned_fields", "selection_tag"]
