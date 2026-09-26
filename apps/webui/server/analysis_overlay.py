"""The own-analysis half of the track read model.

Split out of ``sqlite_backend.py``, which is already the largest module in
this package: these two functions are one concern (how a lane's SOURCE
changes what a track row says) and neither touches SQLite's cursor
handling, connection pooling or the EAV pass around them.

Both are called from ``_row_to_track`` / ``_fetch_fields``. Neither writes.

-Claude
"""
from __future__ import annotations

import sqlite3

from apps.analysis import lanes as analysis_lanes
from apps.analysis import selection as analysis_selection
from apps.analysis.lanes import LANES
from apps.analysis.selection import OWN_ANALYSIS_SOURCE as _OWN_ANALYSIS_SOURCE
from apps.analysis.selection import EffectiveField


def _rb_mapped_for_batch(stable_ids: list[str]) -> dict[str, bool]:
    from apps.webui.server.rb_vendor_pkg.track_rows import bulk_rb_meta

    metas = bulk_rb_meta(stable_ids)
    return {sid: sid in metas for sid in stable_ids}


def _batch_needs_lane_owned_fields(
    conn: sqlite3.Connection,
    stable_ids: list[str],
    rb_mapped: dict[str, bool],
) -> bool:
    """True when any track/lane pair reads from ``analysis_projection``."""
    selection = analysis_selection.Selection.resolve(conn)
    if selection.any_own:
        return True
    for sid in stable_ids:
        mapped = rb_mapped.get(sid, False)
        for lane in LANES:
            if (
                analysis_selection.effective_source_for_track(
                    conn, lane, has_rb_mapping=mapped
                )
                == "own"
            ):
                return True
    return False


def lane_owned_fields(
    conn: sqlite3.Connection, stable_ids: list[str],
) -> dict[str, dict[str, EffectiveField]]:
    """Lane-owned scalars from :func:`effective_fields`, with a safe skip.

    Skips the projection query only when every track in the batch is
    rekordbox-mapped and every lane's per-track effective source is rbx
    (including PARITY-02 toggles). Unmapped standalone libraries always
    read ``analysis_projection`` even when the global default is still rbx
    (STANDALONE-02/06).
    """
    if not stable_ids:
        return {}
    rb_mapped = _rb_mapped_for_batch(stable_ids)
    if not _batch_needs_lane_owned_fields(conn, stable_ids, rb_mapped):
        return {}
    selection = analysis_selection.Selection.resolve(conn)
    return analysis_selection.effective_fields(
        conn, stable_ids, selection, rb_mapped=rb_mapped
    )


def selection_tag(fields: dict[str, EffectiveField]) -> str:
    """A stable marker for WHICH SOURCE each lane-owned field came from.

    The etag is derived from timestamps, and switching a lane from rbx to own
    changes the bpm, the key and the whole provenance block WITHOUT moving
    any of them: an own projection row can easily be older than the track's
    base `updated_at`, in which case the maximum does not budge and two
    genuinely different representations share one strong validator, so a
    stale `If-Match` still passes (Codex P2, PR #1549).

    Source AND the producer's timestamp go in, not just the source. "A value
    change already moves a timestamp" is only true when that timestamp is the
    one the etag actually selects, and `_effective_updated_at` takes the
    MAXIMUM: a `tracks.updated_at` from a host with a fast clock sits above
    every projection stamp, so a producer-version bump could rewrite an own
    BPM while the selected maximum never budged (Codex P2, PR #1549). The
    projection's own `modified_at` is included so a rewrite of the value
    moves the validator even when it does not move the maximum.

    Empty when no lane-owned field is on own, so an all-rbx library keeps
    byte-identical etags to before this existed.
    """
    own = sorted(
        f"{name}={view.source}@{view.modified_at}"
        for name, view in fields.items()
        if name in analysis_selection.PROJECTION_FIELDS
        and view.source != "rekordbox"
        and (
            view.source.startswith(analysis_lanes.OWN_BACKEND_PREFIX)
            or view.source == _OWN_ANALYSIS_SOURCE
        )
        and (view.modified_at or view.status == "missing")
    )
    return "|".join(own)


__all__ = ["lane_owned_fields", "selection_tag"]
