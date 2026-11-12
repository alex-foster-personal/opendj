"""Enrich-on-open: what the library still lacks, and what to ask the user.

Opening the app on a library that is not fully enriched shows one card
(`(private path)`). The lanes split by consent:

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
    [if] stems have no source on this host [then] stems is not asked, the reason is shown, and the card shows
  ✔︎ failed separations are never silent (ENRICH-04)
    [if] stems failed with nothing pending [then] the line is "failed", not "done", and show is true
    [if] a source refusal filed the missing stems as terminal [then] the line is "no_source" with that count
    [if] nothing is pending, failed or refused [then] the line is "done" and does not raise the card
  ✔︎ a lane that cannot run is said (ENRICH-08)
    [if] the enrichment coordinator reports a capability warning [then] it is carried, and a blocking one sets show
  ✔︎ store cover suggestions wait on the user (LIBM-189)
    [if] covers are waiting on consent [then] their count is carried and show is true
  ✔︎ the card's numbers are the coverage API's numbers (ENRICH-02)
    [if] the coverage reading names absent rows by folder [then] the summary carries them unchanged
    [if] the drain reports usable values and drain states [then] ``analysis`` carries them unchanged
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping, Sequence
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


# -----------------------------------------------------------------------------
# decision store
# -----------------------------------------------------------------------------
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


# -----------------------------------------------------------------------------
# policy
# -----------------------------------------------------------------------------
def _analysis_left(analysis: Mapping[str, Any] | None) -> bool:
    if analysis is None:
        return False
    for lane in ANALYSIS_LANES:
        counts = analysis.get("lanes", {}).get(lane)
        if counts is None:
            continue
        # ENRICH-03: over songs when the drain reports them, so a dud file alone never raises the card.
        songs = counts.get("songs") or counts
        if songs.get("missing") or songs.get("failed") or counts.get("unavailable"):
            return True
    return False


def _lyrics_left(coverage: Mapping[str, Any] | None) -> bool:
    if coverage is None:
        return False
    return bool(coverage["pending"].get("lyrics") or coverage["failed"].get("lyrics"))


def stems_ask(coverage: Mapping[str, Any] | None, decisions: Mapping[str, str]) -> dict[str, Any]:
    """The stems line of the card: asked, declined, impossible, failed, or nothing to do.

    ``failed`` counts separations that failed ``MAX_ATTEMPTS`` times and left
    pending (ENRICH-04): over songs when the song view is there, else rows.

    ``missing`` (``no_source`` only) counts stems this computer cannot make:
    with a source refusal the coverage reading files every missing bundle not
    in R2 under ``terminal``, never ``pending``, so pending alone would read
    "done" on a library with no stems at all (ENRICH-04). Songs when the song
    view is there, else rows. Without a refusal, ``terminal`` holds only the
    ledger's own "nothing to separate" verdicts and is finished work.

    The line is ``done`` only when nothing is pending, failed or refused.

    With a song view (ENRICH-03) it also carries ``separated`` (songs with a
    real stem bundle, here or in R2) and ``not_yet`` (songs still pending)."""
    if coverage is None:
        return {"state": "unknown", "pending": None, "failed": None, "reason": "stems coverage could not be read"}
    pending = int(coverage["pending"].get("stems", 0))
    refusal = coverage.get("stems_source_refusal")
    songs = (coverage.get("songs") or {}).get("steps", {}).get("stems")
    extra = {} if songs is None else {"separated": songs["done"], "not_yet": songs["pending"]}
    failed = int(coverage["failed"].get("stems", 0) if songs is None else songs.get("failed", 0))
    terminal = int(coverage["terminal"].get("stems", 0) if songs is None else songs.get("terminal", 0))
    missing = terminal if refusal else 0
    if pending == 0 and failed == 0 and missing == 0:
        return {"state": "done", "pending": 0, "failed": 0, "reason": None, **extra}
    if refusal:
        return {
            "state": "no_source",
            "pending": pending,
            "failed": failed,
            "missing": missing,
            "reason": str(refusal),
            **extra,
        }
    if pending == 0:
        # Nothing left to ask about, only failures to report. A stored "never" still wins.
        if decisions.get("stems") == "never":
            return {"state": "user_declined", "pending": 0, "failed": failed, "reason": None, **extra}
        return {"state": "failed", "pending": 0, "failed": failed, "reason": None, **extra}
    if decisions.get("stems") == "never":
        return {"state": "user_declined", "pending": pending, "failed": failed, "reason": None, **extra}
    return {"state": "ask", "pending": pending, "failed": failed, "reason": None, **extra}


def _stems_left(stems: Mapping[str, Any], decisions: Mapping[str, str]) -> bool:
    """ENRICH-04: the stems line raises the card when it asks, cannot be made
    here, or failed; a stored "never" keeps every one of them quiet."""
    if decisions.get("stems") == "never":
        return False
    return stems["state"] in ("ask", "no_source", "failed") or bool(stems.get("failed"))


def build_summary(  # noqa: PLR0913 -- optional ingest lane count maps stay separate for API clarity
    *,
    analysis: Mapping[str, Any] | None,
    analysis_error: str | None,
    coverage: Mapping[str, Any] | None,
    coverage_error: str | None,
    decisions: Mapping[str, str],
    warnings: Sequence[Mapping[str, str]] = (),
    artwork_suggestions: int = 0,
    ingest_duplicates: int = 0,
    ingest_genre: Mapping[str, int] | None = None,
    ingest_identify: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    stems = stems_ask(coverage, decisions)
    # identify proposals need a person; genre fill does not feed show (SETPREP-10646).
    show = (
        any(w.get("severity") == "blocking" for w in warnings)
        or artwork_suggestions > 0
        or (ingest_identify or {}).get("proposals", 0) > 0
        or ingest_duplicates > 0
        or _analysis_left(analysis)
        or _lyrics_left(coverage)
        or _stems_left(stems, decisions)
        or analysis_error is not None
        or coverage_error is not None
    )
    return {
        "show": show,
        "analysis": analysis,
        "analysis_error": analysis_error,
        "coverage": None
        if coverage is None
        else {
            "on_disk": coverage["on_disk"],
            # ENRICH-02: rows whose file is not on this Mac, by bucket and folder,
            # straight from the coverage reading (never recounted here).
            "availability": dict(coverage.get("availability", {})),
            "absent_folders": list(coverage.get("absent_folders", [])),
            "songs": coverage.get("songs"),
            "done": {k: coverage["done"].get(k, 0) for k in ("stems", "lyrics")},
            "terminal": {k: coverage["terminal"].get(k, 0) for k in ("stems", "lyrics")},
            "failed": {k: coverage["failed"].get(k, 0) for k in ("stems", "lyrics")},
            "pending": {k: coverage["pending"].get(k, 0) for k in ("stems", "lyrics")},
        },
        "coverage_error": coverage_error,
        "stems": stems,
        "decisions": dict(decisions),
        # ENRICH-08: what cannot run here, and where to set it.
        "warnings": [dict(w) for w in warnings],
        # LIBM-189: store covers found, saved only after the user says yes.
        "artwork_suggestions": artwork_suggestions,
        "ingest_duplicates": max(0, int(ingest_duplicates)),
        # SETPREP-10646: genre fill counts from the latest ingest enrichment batch.
        "ingest_genre": None if ingest_genre is None else dict(ingest_genre),
        "ingest_identify": None if ingest_identify is None else dict(ingest_identify),
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
