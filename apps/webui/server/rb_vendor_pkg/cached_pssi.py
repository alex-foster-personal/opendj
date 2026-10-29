"""rekordbox PSSI phrases recovered from the anlz cache for a track whose content row is gone.

A track relinked to a new path can keep its ``track_vendor_ids`` row while the
rekordbox content id it names no longer exists in ``master.plain.db``, so
``resolve_content`` reports ``VENDOR_MAPPING_NOT_FOUND`` and ``/anlz`` serves
the local-import stub with no phrases. The anlz cache entry written while the
track was still mapped (``<stable_id>.json``, ``payload.phrases``) still holds
rekordbox's own phrase analysis for it (PHRASE-RECOVER-01).

Guard: the cache has no audio fingerprint, so the entry is served only when
the cached rekordbox beatgrid's last beat lies within ``DURATION_TOLERANCE_S``
of the track row's ``duration_ms`` (rekordbox grids run to the end of the
file; the last PHRASE does not, it ended up to 5.8 s early on the maintainer's library).
A different file almost always disagrees here. A re-encode of the same length
would pass; that limit is stated, not hidden.

Times: the cache keeps rekordbox's numbers, so the MP3 lead-in of the file we
serve is taken off exactly as on the mapped branch (``lead_in_shift``). PSSI
is phrase SECONDS, not beat indices, so it stays valid whichever beatgrid the
payload serves.

-Claude
"""
from __future__ import annotations

import json
import logging
from typing import Any

from .anlz_cache import _cache_path

log = logging.getLogger(__name__)

DURATION_TOLERANCE_S = 1.5
RECOVERY_SOURCE = "anlz-cache"


def cached_pssi_phrases(
    stable_id: str, duration_ms: int | None
) -> tuple[list[dict[str, Any]] | None, dict[str, Any]]:
    """The cached rekordbox phrases on rekordbox's timeline, or None, plus the stated outcome."""
    path = _cache_path(stable_id)
    if not path.is_file():
        return None, {"status": "no_cache_entry"}
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("anlz cache unreadable for phrase recovery: %s (%s)", path, exc)
        return None, {"status": "cache_unreadable"}
    payload = cached.get("payload") or {}
    phrases = payload.get("phrases") or []
    if payload.get("stable_id") != stable_id or not phrases:
        return None, {"status": "no_cached_phrases"}
    if duration_ms is None:
        return None, {"status": "guard_unmeasurable", "reason": "track row has no duration_ms"}
    beats = (payload.get("beatgrid") or {}).get("beats") or []
    if not beats:
        return None, {"status": "guard_unmeasurable", "reason": "cache entry has no beatgrid"}
    delta_s = round(abs(float(beats[-1]["t"]) - duration_ms / 1000), 3)
    outcome = {
        "from": RECOVERY_SOURCE,
        "anlz_mtime": cached.get("anlz_mtime"),
        "cache_schema": cached.get("schema"),
        "guard": "last_beat_vs_duration",
        "duration_delta_s": delta_s,
    }
    if delta_s > DURATION_TOLERANCE_S:
        return None, {**outcome, "status": "guard_mismatch"}
    return [dict(ph) for ph in phrases], {**outcome, "status": "recovered"}


def _track_duration_ms(stable_id: str) -> int | None:
    from apps.adapters.rekordbox import config
    from apps.adapters.rekordbox.errors import _open_ro

    state = _open_ro(config.STATE_DB, "STATE_DB")
    try:
        row = state.execute(
            "SELECT duration_ms FROM tracks WHERE stable_id = ? AND deleted_at IS NULL",
            (stable_id,),
        ).fetchone()
    finally:
        state.close()
    return int(row[0]) if row and row[0] is not None else None


def _served_lead_in_s(stable_id: str, share: bool) -> float:
    """The MP3 lead-in of the file GET /audio serves; 0 when none resolves (it cannot play)."""
    from fastapi import HTTPException

    from apps.adapters.rekordbox.paths import resolve_playable_audio

    from .lead_in_shift import anlz_lead_in_s

    try:
        picked = resolve_playable_audio(stable_id, share=share)
    except HTTPException:
        return 0.0
    return anlz_lead_in_s(str(picked.path))


def apply_cached_pssi(payload: dict[str, Any], stable_id: str, *, share: bool) -> dict[str, Any]:
    """Fill an unmapped payload's empty phrases from the anlz cache when the guard passes.

    ``phrases_recovery`` always states the outcome, so a ``phrases_source:
    rekordbox`` that came from the cache is distinguishable from a live ANLZ read.
    """
    if payload.get("phrases"):
        return payload
    phrases, outcome = cached_pssi_phrases(stable_id, _track_duration_ms(stable_id))
    if phrases is None:
        return {**payload, "phrases_recovery": outcome}
    from .lead_in_shift import on_our_timeline

    shifted = on_our_timeline(
        {"beatgrid": {"beats": []}, "phrases": phrases}, _served_lead_in_s(stable_id, share)
    )["phrases"]
    return {**payload, "phrases": shifted, "phrases_recovery": outcome}
