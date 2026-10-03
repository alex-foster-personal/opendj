"""rekordbox ANLZ times moved onto our decode timeline (MP3 lead-in shift).

Split out of ``anlz.py``, which sits at its 600-line ceiling. The arithmetic
and the measurement behind it live in ``apps.shared.mp3_lead_in``; this is the
``/anlz`` payload's half of "Shift at import" (JIK, Fri 2 Oct 2026).

-Claude
"""
from __future__ import annotations

from typing import Any

from apps.shared.mp3_lead_in import rekordbox_lead_in_s


def anlz_lead_in_s(folder_path: str | None) -> float:
    """The lead-in to take off a served payload; 0 when there is no local file to read.

    No local file means the track cannot play here either, so there is no
    timeline of ours for its positions to be wrong against.
    """
    return rekordbox_lead_in_s(folder_path) or 0.0


def on_our_timeline(payload: dict[str, Any], lead_in_s: float) -> dict[str, Any]:
    """The rekordbox beatgrid and phrases with the file's MP3 lead-in taken off.

    rekordbox times start at the first sample of a raw decode; ours start
    after the lead-in our decoders trim (``apps.shared.mp3_lead_in``). Applied
    after the file cache, so the cache keeps rekordbox's own numbers and no
    cache entry needs invalidating. A beat the shift puts before the first
    sample we play is dropped: it lies in audio no engine of ours renders.
    """
    if not lead_in_s:
        return payload
    grid = payload["beatgrid"]
    beats = [
        {**b, "t": round(b["t"] - lead_in_s, 3)}
        for b in grid.get("beats", [])
        if b["t"] - lead_in_s >= 0
    ]
    phrases = [
        {
            **ph,
            "start_s": round(max(0.0, ph["start_s"] - lead_in_s), 3),
            "end_s": round(max(0.0, ph["end_s"] - lead_in_s), 3),
        }
        for ph in payload.get("phrases", [])
    ]
    return {
        **payload,
        "beatgrid": {**grid, "beats": beats, "beat_count": len(beats)},
        "phrases": phrases,
    }
