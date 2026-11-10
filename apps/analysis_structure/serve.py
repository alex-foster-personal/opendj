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

OWN-FAST FILLS WHAT ALL-IN-ONE HAS NOT REACHED (STRUCT-06, behind
``MDT_OWN_FAST_SECTIONS=1``). Wherever the rules above would serve own
sections but no usable All-In-One sidecar exists, the own-fast sections
(``fast_segment.py``, cut on the payload's own grid) are served instead, with
``phrases_source: own-fast`` and ``source: "own-fast"`` on each phrase. They
are still OWN sections, so the ``own`` selection takes them; rekordbox is
never substituted. An All-In-One sidecar replaces them on the track's next
``/anlz``. The All-In-One CLI writes from its own process and publishes no
event, so a deck already showing own-fast sections keeps them until it
reloads the track (logged in ``(private path)``).
A track still being computed answers ``own_phrases.status: pending``.

-Claude
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import fast_sidecar
from .contract import PHRASE_SOURCE_LANES
from .drop_events import mark_drops
from .pipeline import load_sidecar


def _own_phrase(section: dict[str, Any], source: str = "own") -> dict[str, Any]:
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
        "source": source,
    }
    if section.get("dj_label"):
        phrase.update(
            label=section["dj_label"],
            model_label=section["label"],
            ambient=bool(section.get("ambient")),
        )
        if section.get("continuation"):
            phrase["continuation"] = True
    return phrase


def merge_own_phrases(
    payload: dict[str, Any], stable_id: str, data_dir: Path, *, source: str, share: bool = False
) -> dict[str, Any]:
    if source not in PHRASE_SOURCE_LANES:
        raise ValueError(f"unknown phrase source {source!r}")
    if source == "rbx" and payload.get("phrases"):
        return {**payload, "phrases_source": "rekordbox"}
    doc = load_sidecar(data_dir, stable_id)
    if (doc is None or doc["status"] != "ok") and fast_sidecar.enabled():
        if share:
            # Own-fast decodes, caches and keys the LOCAL audio copy only. A
            # share listener may hear another copy (share policy), so cutting
            # here would decode the owner's file, overwrite the share waveform
            # cache and serve sections of audio the listener does not hear.
            # Share requests never start own-fast work (discussion_r4264419246).
            unavailable = {"status": "failed", "reason": "share_audience: own-fast cuts the local copy only"}
            return {
                **payload,
                "phrases": [],
                "phrases_source": "none",
                "own_phrases": {**unavailable, "producer": fast_sidecar.PRODUCER},
            }
        return _merge_fast(payload, stable_id, data_dir)
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
    sections, moments = _sections_and_moments(doc)
    phrases = [_own_phrase(s) for s in sections]
    return {
        **payload,
        "phrases": phrases,
        "phrases_source": "own",
        "own_phrases": status,
        "phrase_moments": moments,
    }


def _sections_and_moments(doc: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The sidecar's sections and drop moments (STRUCT-07), moments always derived on load.

    ``drop_events.mark_drops`` runs on copies of the stored sections every
    time, so served moments and ``continuation`` flags can never disagree with
    the spans (a stored ``moments`` list is not trusted), and a sidecar written
    before STRUCT-07 gets the same ticks a fresh one would, without a re-run.
    """
    sections = [dict(s) for s in doc["sections"]]
    return sections, mark_drops(sections)


def _merge_fast(payload: dict[str, Any], stable_id: str, data_dir: Path) -> dict[str, Any]:
    fast = fast_sidecar.fast_sections(payload, stable_id, data_dir)
    status = {
        "status": fast["status"],
        "reason": fast.get("reason"),
        "producer": fast.get("producer"),
        "producer_version": fast.get("producer_version"),
    }
    if fast["status"] != "ok":
        return {**payload, "phrases": [], "phrases_source": "none", "own_phrases": status}
    sections, moments = _sections_and_moments(fast)
    return {
        **payload,
        "phrases": [_own_phrase(s, "own-fast") for s in sections],
        "phrases_source": "own-fast",
        "own_phrases": status,
        "phrase_moments": moments,
    }


def niche_match_citation(*, start_s: float, end_s: float, label: str, source: str) -> dict[str, Any]:
    """One labelled span cited on a niche search hit (parent #345)."""
    return {
        "start_s": round(float(start_s), 3),
        "end_s": round(float(end_s), 3),
        "label": label,
        "source": source,
    }


__all__ = ["merge_own_phrases", "niche_match_citation"]
