"""Stems that can never exist: record "nothing to separate" as a finished state.

The Stems light can only reach green if "this file has no stems to make" is a
recordable answer. Before this module ``no_source`` for stems was written only
when a drain job raised it, and stems has no drain job, so a track that is
itself a vocal stem, or whose file cannot be decoded, stayed pending forever.

Three rules decide it, checked cheapest first:

=================  ========================================================
rule               evidence
=================  ========================================================
stem file          the file sits in a directory named ``vocals`` or
                   ``instrumental``, or its file name or its library title
                   ends in ``-vocals`` / ``_vocals`` / `` - vocals``. It is
                   already one part of a separation
unreadable source  ffprobe exits 0 and reports no duration, or refuses the
                   file as invalid data. Nothing can decode it
too short          ffprobe reports under :data:`MIN_SEPARABLE_S` seconds: a
                   sampler one-shot, not a track to mix
=================  ========================================================

``-instrumental`` as a NAME suffix is deliberately not a rule: "Artist - Song
- Instrumental" is an ordinary release that separates into drums and bass.
Mark those by hand through the route or CLI when they really are stems.

Containment: a file that cannot be opened at all (an unmounted volume, a
pending macOS privacy prompt) and an ffprobe that is missing or fails for any
other reason are UNKNOWN, never terminal. A wrong terminal mark hides a track
from the farm, so only the two exact ffprobe signatures above produce one.

A mark is stored in the outcome ledger (``coverage_outcomes``) keyed on the
audio file's signature, so replacing the file re-arms the track. A user can
clear a mark; the cleared id is kept in ``coverage-stems-keep-pending.json``
so the automatic check does not put it straight back.

Requirements (mini-PRD):
  ✔︎ ✅ 🎯 HEALTH-09 the three rules produce a terminal mark
    [if] a file is named ``... - vocals.mp3`` [then] marked, ffprobe not run
    [if] ffprobe reports no duration [then] marked unreadable
    [if] ffprobe reports 2 s [then] marked too short; [if] 12 s [then] not
  ✔︎ ✅ 🎯 HEALTH-09 unknown is never terminal
    [if] the file cannot be opened [then] not marked
    [if] ffprobe is not installed [then] not marked, and the reason is named
  ✔︎ ✅ 🎯 HEALTH-09 a cleared mark stays cleared
    [if] a user clears an automatic mark [then] the check skips that track
    [if] a user marks it again [then] the skip is removed
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from apps.webui.server import coverage_outcomes as outcomes_mod

#: Shorter than this and there is nothing to mix with stems. The shortest
#: track in the preview library is 107 s; sampler one-shots run 1 to 5 s.
MIN_SEPARABLE_S: float = 10.0
STEM_DIRECTORY_NAMES: frozenset[str] = frozenset({"vocals", "instrumental"})
STEM_NAME_SUFFIX_RE: re.Pattern[str] = re.compile(r"(?:\s-\s|[-_])vocals$", re.IGNORECASE)
KEEP_PENDING_FILENAME: str = "coverage-stems-keep-pending.json"
KEEP_PENDING_SCHEMA: int = 1
FFPROBE_TIMEOUT_S: float = 20.0
FFPROBE_INVALID_DATA: str = "Invalid data found when processing input"
AUTO_PREFIX: str = "auto"
MANUAL_PREFIX: str = "manual"
STEP: str = "stems"

ProbeKind = Literal["duration", "no_duration", "unknown"]
Target = tuple[str, str]


@dataclass(frozen=True)
class Probe:
    kind: ProbeKind
    duration_s: float | None = None
    detail: str | None = None


#-----------------------------------------------------------------------------
# the rules
#-----------------------------------------------------------------------------
def stem_file_reason(audio_path: Path, title: str | None) -> str | None:
    """Why this file is itself a stem, or None. Names only, no file access."""
    if audio_path.parent.name.lower() in STEM_DIRECTORY_NAMES:
        return f"the file is itself a stem (directory {audio_path.parent.name!r})"
    for label, name in (("file name", audio_path.stem), ("title", title or "")):
        if STEM_NAME_SUFFIX_RE.search(name.strip()):
            return f"the file is itself a vocal stem ({label} ends in 'vocals')"
    return None


def ffprobe_refusal() -> str | None:
    """Why durations cannot be probed on this install, or None when they can."""
    if shutil.which("ffprobe") is None:
        return "ffprobe is not installed, so unreadable and too-short sources are not detected"
    return None


def probe(audio_path: Path) -> Probe:
    """One ffprobe read of the container duration. Never raises for a bad file."""
    try:
        with audio_path.open("rb") as handle:
            handle.read(1)
    except OSError as error:
        return Probe("unknown", detail=f"cannot open the file: {error}")
    executable = shutil.which("ffprobe")
    if executable is None:
        return Probe("unknown", detail="ffprobe is not installed")
    try:
        completed = subprocess.run(
            [executable, "-v", "error", "-show_entries", "format=duration", "-of", "json",
             str(audio_path)],
            capture_output=True, text=True, timeout=FFPROBE_TIMEOUT_S, check=False,
        )
    except subprocess.TimeoutExpired:
        return Probe("unknown", detail=f"ffprobe timed out after {FFPROBE_TIMEOUT_S:.0f} s")
    if completed.returncode != 0:
        if FFPROBE_INVALID_DATA in completed.stderr:
            return Probe("no_duration", detail="ffprobe: invalid data")
        tail = completed.stderr.strip().splitlines()[-1:] or ["no message"]
        return Probe("unknown", detail=f"ffprobe exited {completed.returncode}: {tail[0]}")
    raw = json.loads(completed.stdout).get("format", {}).get("duration")
    try:
        duration = float(raw)
    except (TypeError, ValueError):
        return Probe("no_duration", detail=f"ffprobe reported duration {raw!r}")
    if duration <= 0:
        return Probe("no_duration", detail=f"ffprobe reported duration {duration}")
    return Probe("duration", duration_s=duration)


def classify(
    audio_path: Path, title: str | None, *, probe_fn: Callable[[Path], Probe] = probe
) -> str | None:
    """The reason this track can never have stems, or None (pending or unknown)."""
    reason = stem_file_reason(audio_path, title)
    if reason is not None:
        return reason
    result = probe_fn(audio_path)
    if result.kind == "no_duration":
        return f"the source is unreadable ({result.detail})"
    too_short = (
        result.kind == "duration"
        and result.duration_s is not None
        and result.duration_s < MIN_SEPARABLE_S
    )
    if too_short:
        return (
            f"the source is {result.duration_s:.1f} s, under the "
            f"{MIN_SEPARABLE_S:.0f} s minimum worth separating"
        )
    return None


#-----------------------------------------------------------------------------
# user overrides
#-----------------------------------------------------------------------------
class KeepPending:
    """Ids a user cleared: the automatic check leaves them pending."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> set[str]:
        if not self.path.is_file():
            return set()
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema") != KEEP_PENDING_SCHEMA:
            raise ValueError(f"{self.path} is not a schema {KEEP_PENDING_SCHEMA} keep-pending file")
        return {str(stable_id) for stable_id in payload["stable_ids"]}

    def _write(self, stable_ids: set[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(
            json.dumps({"schema": KEEP_PENDING_SCHEMA, "stable_ids": sorted(stable_ids)}) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    def add(self, stable_id: str) -> None:
        self._write(self.load() | {stable_id})

    def discard(self, stable_id: str) -> None:
        current = self.load()
        if stable_id in current:
            self._write(current - {stable_id})


def keep_pending_path(data_dir: Path) -> Path:
    return data_dir / "state" / KEEP_PENDING_FILENAME


#-----------------------------------------------------------------------------
# marks
#-----------------------------------------------------------------------------
def mark_no_source(
    outcomes: outcomes_mod.OutcomeStore,
    keep: KeepPending,
    stable_id: str,
    audio_path: Path,
    reason: str,
    *,
    now: float,
) -> outcomes_mod.Outcome:
    """Record a user's "this track has no stems to make" for this audio file."""
    if not reason.strip():
        raise ValueError("a no_source mark needs a reason")
    keep.discard(stable_id)
    return outcomes.record_no_source(
        STEP, stable_id, outcomes_mod.audio_token(audio_path),
        f"{MANUAL_PREFIX}: {reason.strip()}", now=now,
    )


def clear_no_source(
    outcomes: outcomes_mod.OutcomeStore, keep: KeepPending, stable_id: str
) -> bool:
    """Remove a mark and keep the automatic check off the track. Returns
    whether a mark existed. A ``failed`` entry is not a mark and is left."""
    keep.add(stable_id)
    prior = outcomes.load().get((STEP, stable_id))
    if prior is None or prior.kind != "no_source":
        return False
    return outcomes.clear(STEP, stable_id)


def list_no_source(outcomes: outcomes_mod.OutcomeStore) -> list[outcomes_mod.Outcome]:
    return [
        outcome
        for (step, _stable_id), outcome in sorted(outcomes.load().items())
        if step == STEP and outcome.kind == "no_source"
    ]


#-----------------------------------------------------------------------------
# the drain-driven check
#-----------------------------------------------------------------------------
@dataclass(frozen=True)
class CheckReport:
    checked: int
    marked: tuple[str, ...]


class StemsCheck:
    """Classify pending-stems tracks once per audio generation per process.

    The memo is in memory on purpose: a verdict of "not terminal" is cheap to
    re-derive after a restart and cannot go stale on disk.
    """

    def __init__(
        self,
        *,
        outcomes: outcomes_mod.OutcomeStore,
        keep: KeepPending,
        title_fn: Callable[[str], str | None],
        probe_fn: Callable[[Path], Probe] = probe,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._outcomes = outcomes
        self._keep = keep
        self._title_fn = title_fn
        self._probe_fn = probe_fn
        self._clock = clock
        self._seen: set[tuple[str, str]] = set()
        self.marked_total: int = 0

    def run(self, pending: Sequence[Target], *, limit: int) -> CheckReport:
        """Check up to ``limit`` not-yet-seen tracks; mark the terminal ones."""
        kept = self._keep.load()
        checked = 0
        marked: list[str] = []
        for stable_id, audio_path in pending:
            if checked >= limit:
                break
            if stable_id in kept:
                continue
            path = Path(audio_path)
            key = (stable_id, outcomes_mod.audio_token(path))
            if key in self._seen:
                continue
            self._seen.add(key)
            checked += 1
            reason = classify(path, self._title_fn(stable_id), probe_fn=self._probe_fn)
            if reason is not None:
                self._outcomes.record_no_source(
                    STEP, stable_id, key[1], f"{AUTO_PREFIX}: {reason}", now=self._clock()
                )
                marked.append(stable_id)
        self.marked_total += len(marked)
        return CheckReport(checked=checked, marked=tuple(marked))


__all__ = [
    "AUTO_PREFIX",
    "KEEP_PENDING_FILENAME",
    "MANUAL_PREFIX",
    "MIN_SEPARABLE_S",
    "STEM_DIRECTORY_NAMES",
    "STEM_NAME_SUFFIX_RE",
    "CheckReport",
    "KeepPending",
    "Probe",
    "StemsCheck",
    "classify",
    "clear_no_source",
    "ffprobe_refusal",
    "keep_pending_path",
    "list_no_source",
    "mark_no_source",
    "probe",
    "stem_file_reason",
]
