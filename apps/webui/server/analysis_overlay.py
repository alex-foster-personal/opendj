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
from apps.analysis.byte_siblings import (
    LANE_DONOR_STATUSES,
    choose_lane_donor,
    sibling_path_key,
)
from apps.analysis.canonical import FIELDS_BY_LANE
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
    # The effective source depends on the lane and on whether a track is
    # mapped, never on which track: resolve each mapping state present in
    # the batch once, not once per track (LIBM-135).
    sources = analysis_selection.lane_sources_by_mapping(conn)
    return any(
        source == "own"
        for mapped in {bool(rb_mapped.get(sid, False)) for sid in stable_ids}
        for source in sources[mapped].values()
    )


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
    fields = analysis_selection.effective_fields(
        conn, stable_ids, selection, rb_mapped=rb_mapped
    )
    return _borrow_path_sibling_lanes(conn, fields)



def _projection_has_servable_lane(conn: sqlite3.Connection) -> bool:
    """True when some own lane row could be copied onto a same-path sibling.

    A missing own lane is synthesized when analysis_projection has no row.
    Listing tracks must not follow that with a second
    "FROM tracks WHERE deleted_at IS NULL" scan: the keyset chunk budget
    is the one LIMIT query. Failed rows are servable, because NATIVE-30
    copies a failure as well as an ok lane. A stored missing row is not.
    """
    present = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='analysis_projection'"
    ).fetchone()
    if present is None:
        return False
    return (
        conn.execute(
            "SELECT 1 FROM analysis_projection WHERE status != 'missing' LIMIT 1"
        ).fetchone()
        is not None
    )


def _borrow_path_sibling_lanes(
    conn: sqlite3.Connection,
    fields: dict[str, dict[str, EffectiveField]],
) -> dict[str, dict[str, EffectiveField]]:
    """Serve a same-path sibling's own projection when the audio hash matches.

    Tier-3 ids include mtime, so one file can have two rows (NATIVE-24).
    The imported row shows one sibling's own lane, including a failed lane,
    only when both rows carry the same ``audio_hash``. Several donors resolve
    to one per lane: the first ``ok`` in ``stable_id`` order, else the first
    failure. That is the same choice the ``/anlz`` readers make. A Rekordbox
    ``track_fields`` value is not an own lane and is not copied. Rows are
    not merged.
    """
    needy = [
        sid for sid, owned in fields.items()
        if any(_lane_is_absent(owned, names) for names in FIELDS_BY_LANE.values())
    ]
    if not needy or not _projection_has_servable_lane(conn):
        return fields
    ids = list(fields)
    placeholders = ",".join("?" * len(ids))
    identity: dict[str, tuple[str, str]] = {}
    for sid, path, digest in conn.execute(
        "SELECT stable_id, file_path, audio_hash FROM tracks "
        f"WHERE stable_id IN ({placeholders})",
        ids,
    ):
        if path and digest:
            identity[str(sid)] = (sibling_path_key(str(path)), str(digest))
    needy_keys = {identity[sid] for sid in needy if sid in identity}
    if not needy_keys:
        return fields
    hash_list = list({digest for _path, digest in needy_keys})
    hash_placeholders = ",".join("?" * len(hash_list))
    groups: dict[tuple[str, str], list[str]] = {key: [] for key in needy_keys}
    for sid, path, digest in conn.execute(
        "SELECT stable_id, file_path, audio_hash FROM tracks "
        "WHERE deleted_at IS NULL AND audio_hash IS NOT NULL AND "
        f"audio_hash IN ({hash_placeholders})",
        hash_list,
    ):
        if not path or not digest:
            continue
        key = (sibling_path_key(str(path)), str(digest))
        if key in groups:
            groups[key].append(str(sid))
    donor_ids = sorted({sid for group in groups.values() for sid in group})
    donated = analysis_selection.own_projection_fields(conn, donor_ids)
    for sid in needy:
        key = identity.get(sid)
        if key is None:
            continue
        donors_by_lane: dict[str, list[tuple[str, str]]] = {}
        for other in sorted(groups.get(key, [])):
            if other == sid:
                continue
            views = donated.get(other, {})
            for lane, names in FIELDS_BY_LANE.items():
                status = _donor_lane_status(views, names)
                if status is None:
                    continue
                donors_by_lane.setdefault(lane, []).append((other, status))
        for lane, candidates in donors_by_lane.items():
            names = FIELDS_BY_LANE[lane]
            if not _lane_is_absent(fields[sid], names):
                continue
            chosen = choose_lane_donor(candidates)
            if chosen is None:
                continue
            for name in names:
                view = donated.get(chosen, {}).get(name)
                if view is None or view.status == "missing":
                    continue
                current = fields[sid].get(name)
                if current is None or current.status == "missing":
                    fields[sid][name] = view
    return fields


def _lane_is_absent(
    owned: dict[str, EffectiveField], names: tuple[str, ...]
) -> bool:
    """True when every known field of this lane is missing.

    One servable field means the lane is the target's own. Filling the
    missing fields from a sibling would mix two records, while the
    ``/anlz`` reader keeps the target's own lane whole (NATIVE-30).
    """
    present = [owned[name] for name in names if name in owned]
    if not present:
        return True
    return all(view.status == "missing" for view in present)


def _donor_lane_status(
    views: dict[str, EffectiveField], names: tuple[str, ...]
) -> str | None:
    """One status for every field of a lane. ``ok`` beats ``failed``.

    Every field's status is checked first. Returning ``ok`` at the first ok
    field let a field with an unknown status ride along and be served.
    """
    statuses = [views[name].status for name in names if name in views]
    for name in names:
        if name in views and views[name].status not in LANE_DONOR_STATUSES:
            raise ValueError(
                f"unknown lane status {views[name].status!r} on projected field {name!r}"
            )
    if not statuses or all(status == "missing" for status in statuses):
        return None
    return "ok" if "ok" in statuses else "failed"


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
