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

THE PHRASE LANE CAN PICK OWN (STRUCT-04). The PARITY-02 dropdown's Phrase
row sets the ``phrase`` selection lane. On ``rbx`` (the default) the rules
above hold. On ``own`` only own sections are served, even where PSSI exists,
and a track with no usable sidecar answers an empty list with
``phrases_source: none`` rather than falling back to rekordbox: a chosen
source is never silently substituted by another.

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


def _own_phrase(section: dict[str, Any]) -> dict[str, Any]:
    """One own section as a phrase; its DJ label wins ``label`` when it has one (STRUCT-03).

    A section the energy rules labeled (``dj_labels.py``) is served with
    ``label`` set to its DJ label, the model's pop label kept as
    ``model_label``, plus ``ambient``. An unlabeled section is served exactly
    as before, model label in ``label``.
    """
    phrase = {
        "start_s": section["start_s"],
        "end_s": section["end_s"],
        "kind": 0,
        "mood": 0,
        "label": section["label"],
        "source": "own",
    }
    if section.get("dj_label"):
        phrase.update(
            label=section["dj_label"],
            model_label=section["label"],
            ambient=bool(section.get("ambient")),
        )
    return phrase


def merge_own_phrases(
    payload: dict[str, Any], stable_id: str, data_dir: Path, *, source: str
) -> dict[str, Any]:
    if source not in ("rbx", "own"):
        raise ValueError(f"unknown phrase source {source!r}")
    if source == "rbx" and payload.get("phrases"):
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
    phrases = [_own_phrase(s) for s in doc["sections"]]
    return {**payload, "phrases": phrases, "phrases_source": "own", "own_phrases": status}


__all__ = ["merge_own_phrases"]
