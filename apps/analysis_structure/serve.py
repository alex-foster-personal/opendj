"""Serve-time merge of own sections into the ``/anlz`` ``phrases`` field.

REKORDBOX PSSI ALWAYS WINS. When the vendor analysis carries phrases they are
served untouched, exactly as before (NATIVE-01's "real rekordbox PSSI phrases
keep being served"). Own sections fill ONLY the empty case, which today is
every track rekordbox never analyzed and the 29 percent of analyzed tracks
with no PSSI (PARITY-01's 178 of 250).

THE SOURCE IS ALWAYS NAMED. ``phrases_source`` is ``rekordbox``, ``own`` or
``none``, so a consumer never has to guess whether a chevron came from the
vendor or from our model, and an empty list keeps meaning "nothing measured"
(``none``) rather than "measured nothing". An own phrase carries ``kind: 0``
and ``mood: 0``, which are not PSSI ids, plus the model's ``label`` and
``source: "own"``; the waveform only paints phrase starts, so the kind is
never interpreted as a PSSI kind.

A FAILED SIDECAR IS REPORTED, NOT HIDDEN. ``own_phrases`` carries the
sidecar's status and reason whenever one exists, so "could not analyze this
track" reaches the client instead of reading as "never analyzed".

Applied after the anlz file cache, like ``merge_demucs_vocals``, so a sidecar
landing later is reflected without an anlz-cache schema bump.

-Claude
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .pipeline import load_sidecar


def merge_own_phrases(payload: dict[str, Any], stable_id: str, data_dir: Path) -> dict[str, Any]:
    if payload.get("phrases"):
        return {**payload, "phrases_source": "rekordbox"}
    doc = load_sidecar(data_dir, stable_id)
    if doc is None:
        return {**payload, "phrases": [], "phrases_source": "none"}
    status = {
        "status": doc["status"],
        "reason": doc.get("reason"),
        "producer": doc.get("producer"),
        "producer_version": doc.get("producer_version"),
    }
    if doc["status"] != "ok":
        return {**payload, "phrases": [], "phrases_source": "none", "own_phrases": status}
    phrases = [
        {
            "start_s": s["start_s"],
            "end_s": s["end_s"],
            "kind": 0,
            "mood": 0,
            "label": s["label"],
            "source": "own",
        }
        for s in doc["sections"]
    ]
    return {**payload, "phrases": phrases, "phrases_source": "own", "own_phrases": status}


__all__ = ["merge_own_phrases"]
