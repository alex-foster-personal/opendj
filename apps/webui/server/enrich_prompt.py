"""Enrich-on-open: what the library still lacks, and what to ask the user.

Opening the app on a library that is not fully enriched shows one card
(`specs/state-inventories/library-enrichment.md`). The lanes split by consent:

* Automatic, never asked: file tags, Preview strips, loudness, waveform,
  BPM/beatgrid and key (the ahead-of-time drain) and lyrics (the coverage
  drain). The card reports their progress, failures and any lane this host
  cannot run.
* Opt-in, asked once per library: stems, which need the remote farm or heavy
  local compute. The answer "never" is stored per library in the app's own
  data dir (``state/enrich-decisions.json``), never in library files, and
  every answer has an HTTP and a CLI face.

This module is the POLICY: pure functions over the two coverage readings and
the stored decisions, so the card's every state is unit-testable.

Requirements (mini-PRD):
  ✔︎ the card shows only when something is left
    [if] every automatic lane is done (or declined) and nothing is asked [then] show is false
    [if] any automatic lane is missing, failed or unavailable [then] show is true
  ✔︎ stems are asked once, and a "never" sticks
    [if] stems are pending and no decision is stored [then] stems is asked
    [if] the stored decision is "never" [then] stems is not asked, and the card says so
    [if] stems have no source on this host [then] stems is not asked, and the reason is shown
"""
from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

FILENAME: str = "enrich-decisions.json"
SCHEMA: int = 1
#: Lanes the card asks about. Every other lane runs without asking.
OPT_IN_LANES: tuple[str, ...] = ("stems",)
Decision = Literal["ask", "never"]
DECISIONS: tuple[str, ...] = ("ask", "never")
#: Automatic analysis lanes in the order the card lists them.
ANALYSIS_LANES: tuple[str, ...] = ("tags", "strip", "beatgrid", "key", "loudness", "waveform")


#-----------------------------------------------------------------------------
# decision store
#-----------------------------------------------------------------------------
def store_path(data_dir: Path) -> Path:
    return data_dir / "state" / FILENAME


def load_decisions(data_dir: Path) -> dict[str, Decision]:
    """Stored answers; an absent file is "nothing decided", a bad one raises."""
    path = store_path(data_dir)
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != SCHEMA:
        raise ValueError(f"{path} is not a schema {SCHEMA} enrich decision file")
    decisions = payload.get("decisions", {})
    for lane, answer in decisions.items():
        if lane not in OPT_IN_LANES or answer not in DECISIONS:
            raise ValueError(f"{path} holds an unknown decision {lane!r}={answer!r}")
    return dict(decisions)


def save_decision(data_dir: Path, lane: str, answer: str) -> dict[str, Decision]:
    if lane not in OPT_IN_LANES:
        raise ValueError(f"{lane!r} is not an opt-in lane; opt-in lanes are {OPT_IN_LANES}")
    if answer not in DECISIONS:
        raise ValueError(f"{answer!r} is not a decision; decisions are {DECISIONS}")
    decisions: dict[str, Any] = {**load_decisions(data_dir), lane: answer}
    path = store_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
        json.dump({"schema": SCHEMA, "decisions": decisions}, tmp, indent=1)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)
    return decisions


#-----------------------------------------------------------------------------
# policy
#-----------------------------------------------------------------------------
def _analysis_left(analysis: Mapping[str, Any] | None) -> bool:
    if analysis is None:
        return False
    for lane in ANALYSIS_LANES:
        counts = analysis.get("lanes", {}).get(lane)
        if counts is None:
            continue
        if counts.get("missing") or counts.get("failed") or counts.get("unavailable"):
            return True
    return False


def _lyrics_left(coverage: Mapping[str, Any] | None) -> bool:
    if coverage is None:
        return False
    return bool(coverage["pending"].get("lyrics") or coverage["failed"].get("lyrics"))


def stems_ask(
    coverage: Mapping[str, Any] | None, decisions: Mapping[str, str]
) -> dict[str, Any]:
    """The stems line of the card: asked, declined, impossible, or nothing to do."""
    if coverage is None:
        return {"state": "unknown", "pending": None, "reason": "stems coverage could not be read"}
    pending = int(coverage["pending"].get("stems", 0))
    refusal = coverage.get("stems_source_refusal")
    if pending == 0:
        return {"state": "done", "pending": 0, "reason": None}
    if refusal:
        return {"state": "no_source", "pending": pending, "reason": str(refusal)}
    if decisions.get("stems") == "never":
        return {"state": "user_declined", "pending": pending, "reason": None}
    return {"state": "ask", "pending": pending, "reason": None}


def build_summary(
    *,
    analysis: Mapping[str, Any] | None,
    analysis_error: str | None,
    coverage: Mapping[str, Any] | None,
    coverage_error: str | None,
    decisions: Mapping[str, str],
) -> dict[str, Any]:
    stems = stems_ask(coverage, decisions)
    show = (
        _analysis_left(analysis)
        or _lyrics_left(coverage)
        or stems["state"] == "ask"
        or analysis_error is not None
        or coverage_error is not None
    )
    return {
        "show": show,
        "analysis": analysis,
        "analysis_error": analysis_error,
        "coverage": None if coverage is None else {
            "on_disk": coverage["on_disk"],
            "done": {k: coverage["done"].get(k, 0) for k in ("stems", "lyrics")},
            "terminal": {k: coverage["terminal"].get(k, 0) for k in ("stems", "lyrics")},
            "failed": {k: coverage["failed"].get(k, 0) for k in ("stems", "lyrics")},
            "pending": {k: coverage["pending"].get(k, 0) for k in ("stems", "lyrics")},
        },
        "coverage_error": coverage_error,
        "stems": stems,
        "decisions": dict(decisions),
    }


__all__ = [
    "ANALYSIS_LANES",
    "DECISIONS",
    "OPT_IN_LANES",
    "build_summary",
    "load_decisions",
    "save_decision",
    "stems_ask",
    "store_path",
]
