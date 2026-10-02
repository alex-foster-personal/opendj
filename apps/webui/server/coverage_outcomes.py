"""Per-track outcome ledger: terminal "nothing to do" states and drain backoff.

A coverage light can only reach green if "there is nothing here to make" is a
recordable answer, distinct from "nobody has tried yet". This ledger holds the
two outcomes the artifact caches cannot express on their own:

* ``no_source`` - the step has no input for this track (no stems source).
  Terminal: the track counts as finished for that step.
* ``failed``    - a drain job failed. Retried with exponential backoff, and
  terminal after :data:`MAX_ATTEMPTS`, so an unchanged failure is never
  retried in a loop. A terminally failed track keeps its light amber.

Every entry is keyed on a signature of the track's audio file, so replacing
the file re-arms the track: an outcome describes one generation of the audio,
never the stable_id forever.

Lyrics "no lyrics available" is NOT stored here. ``apps.lyrics.fetch_verdicts``
already records ``instrumental`` / ``no_source`` per track and coverage reads
that store directly; a second copy would be two rows for one truth.

Stored at ``<data_dir>/state/coverage-outcomes.json``, written atomically.

Requirements (mini-PRD):
  ✔︎ ✅ 🎯 failures back off and go terminal
    [if] a failure is retried before its backoff elapses [then ⛔️] refused
    [if] attempts reach MAX_ATTEMPTS on one signature [then] never retried
    [if] the audio signature changes [then] attempts restart from 1
  ✔︎ ✅ 🎯 the ledger is durable and fail-fast
    [if] the file is absent [then] the ledger is empty, not an error
    [if] the file is malformed or names an unknown step [then ⛔️] raise
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

SCHEMA: int = 1
FILENAME: str = "coverage-outcomes.json"
STEPS: tuple[str, ...] = ("vocals", "stems", "lyrics")
KINDS: tuple[str, ...] = ("no_source", "failed")
MAX_ATTEMPTS: int = 3
BACKOFF_BASE_S: float = 60.0

Key = tuple[str, str]


@dataclass(frozen=True)
class Outcome:
    step: str
    stable_id: str
    kind: str
    reason: str
    attempts: int
    signature: str
    recorded_at: float


#-----------------------------------------------------------------------------
# pure predicates
#-----------------------------------------------------------------------------
def store_path(data_dir: Path) -> Path:
    return data_dir / "state" / FILENAME


def audio_token(audio_path: Path) -> str:
    """Identity of one generation of an audio file: size and mtime."""
    stat = audio_path.stat()
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def is_no_source(outcome: Outcome | None, signature: str) -> bool:
    return outcome is not None and outcome.kind == "no_source" and outcome.signature == signature


def is_failed_terminal(outcome: Outcome | None, signature: str) -> bool:
    return (
        outcome is not None
        and outcome.kind == "failed"
        and outcome.signature == signature
        and outcome.attempts >= MAX_ATTEMPTS
    )


def next_attempt_at(outcome: Outcome) -> float:
    """When a failed track may be tried again: 1x, 2x, 4x the base."""
    return outcome.recorded_at + BACKOFF_BASE_S * (2 ** (outcome.attempts - 1))


def may_attempt(outcome: Outcome | None, signature: str, *, now: float) -> bool:
    """Whether a drain job for this track may run right now."""
    if outcome is None or outcome.signature != signature:
        return True
    if outcome.kind == "no_source":
        return False
    if outcome.attempts >= MAX_ATTEMPTS:
        return False
    return now >= next_attempt_at(outcome)


#-----------------------------------------------------------------------------
# the store
#-----------------------------------------------------------------------------
class OutcomeStore:
    """Read-modify-write over one JSON file, serialized by a process lock."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def load(self) -> dict[Key, Outcome]:
        if not self.path.is_file():
            return {}
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
            raise ValueError(f"{self.path} is not a schema {SCHEMA} coverage outcome ledger")
        return {(o.step, o.stable_id): o for o in map(_parse, payload["entries"])}

    def record_failure(
        self, step: str, stable_id: str, signature: str, reason: str, *, now: float
    ) -> Outcome:
        with self._lock:
            entries = self.load()
            prior = entries.get((_step(step), stable_id))
            same = prior is not None and prior.kind == "failed" and prior.signature == signature
            attempts = prior.attempts + 1 if same and prior is not None else 1
            outcome = Outcome(step, stable_id, "failed", reason, attempts, signature, now)
            entries[(step, stable_id)] = outcome
            self._write(entries)
            return outcome

    def record_no_source(
        self, step: str, stable_id: str, signature: str, reason: str, *, now: float
    ) -> Outcome:
        with self._lock:
            entries = self.load()
            outcome = Outcome(_step(step), stable_id, "no_source", reason, 0, signature, now)
            entries[(step, stable_id)] = outcome
            self._write(entries)
            return outcome

    def clear(self, step: str, stable_id: str) -> bool:
        with self._lock:
            entries = self.load()
            removed = entries.pop((_step(step), stable_id), None)
            if removed is not None:
                self._write(entries)
            return removed is not None

    def clear_failures(self) -> int:
        """Re-arm every failed track. ``no_source`` entries are left alone."""
        with self._lock:
            entries = self.load()
            kept = {key: o for key, o in entries.items() if o.kind != "failed"}
            if len(kept) != len(entries):
                self._write(kept)
            return len(entries) - len(kept)

    def _write(self, entries: dict[Key, Outcome]) -> None:
        payload = {
            "schema": SCHEMA,
            "entries": [asdict(entries[key]) for key in sorted(entries)],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=self.path.parent, delete=False
        ) as temporary:
            json.dump(payload, temporary, indent=1)
            temporary_path = Path(temporary.name)
        try:
            os.replace(temporary_path, self.path)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise


def _step(step: str) -> str:
    if step not in STEPS:
        raise ValueError(f"unknown step {step!r}; expected one of {STEPS}")
    return step


def _parse(value: Any) -> Outcome:
    if not isinstance(value, dict):
        raise TypeError("coverage outcome entry must be a JSON object")
    outcome = Outcome(
        step=_step(str(value["step"])),
        stable_id=str(value["stable_id"]),
        kind=str(value["kind"]),
        reason=str(value["reason"]),
        attempts=int(value["attempts"]),
        signature=str(value["signature"]),
        recorded_at=float(value["recorded_at"]),
    )
    if outcome.kind not in KINDS:
        raise ValueError(f"unknown outcome kind {outcome.kind!r}; expected one of {KINDS}")
    return outcome


__all__ = [
    "BACKOFF_BASE_S",
    "KINDS",
    "MAX_ATTEMPTS",
    "STEPS",
    "Outcome",
    "OutcomeStore",
    "audio_token",
    "is_failed_terminal",
    "is_no_source",
    "may_attempt",
    "next_attempt_at",
    "store_path",
]
