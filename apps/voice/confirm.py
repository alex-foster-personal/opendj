"""Verbal yes/no confirmation loop for destructive intents.

Phase 14 Plan 3. Flow:

  1. TTS the prompt ("did you say rate 5 stars? yes or no").
  2. Re-open VAD + STT on the warm whisper daemon; 3 s timeout.
  3. Parse the transcript with a tiny regex: "yes" (+synonyms) -> True;
     anything else -> False.
  4. Journal both prompt + answer to ``data/voice/confirmations.jsonl``
     with a UTC timestamp.

The STT + VAD layers are injected so tests can drive the flow without
a real mic or daemon.
"""
from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

YES_PATTERN = re.compile(
    r"^\s*(?:yes|yeah|yep|yup|confirm|affirmative|ok|okay|sure|do it)\b",
    flags=re.IGNORECASE,
)
NO_PATTERN = re.compile(
    r"^\s*(?:no|nope|nah|cancel|abort|stop|negative)\b",
    flags=re.IGNORECASE,
)


def parse_yes_no(text: str) -> bool | None:
    """Classify a confirmation transcript.

    Returns ``True`` for explicit yes, ``False`` for explicit no, and
    ``None`` for "didn't understand" (treated as cancel upstream).
    """
    if text is None:
        return None
    if YES_PATTERN.match(text):
        return True
    if NO_PATTERN.match(text):
        return False
    return None


def _now_iso() -> str:
    return datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def append_confirmation_log(
    path: Path,
    prompt: str,
    transcript: str,
    decision: bool,
    meta: dict[str, Any] | None = None,
) -> None:
    """Append a JSONL confirmation audit row."""
    entry = {
        "ts": _now_iso(),
        "prompt": prompt,
        "transcript": transcript,
        "decision": decision,
        **(meta or {}),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True))
        fh.write("\n")


@dataclass
class ConfirmResult:
    """Outcome of a single confirmation round."""

    accepted: bool
    transcript: str
    elapsed_ms: float
    timed_out: bool = False
    ambiguous: bool = False


@dataclass
class ConfirmEngine:
    """Reusable yes/no loop.

    Parameters
    ----------
    tts_engine
        Any object with ``speak(text)``.
    listen_fn
        Callable ``listen(timeout_s) -> str`` that blocks at most
        ``timeout_s`` seconds and returns a transcript (or ``""`` on
        timeout). Tests inject a deterministic fake; production passes
        a closure wrapping VAD + STT.
    log_path
        Path to the confirmations JSONL; defaults to
        ``data/voice/confirmations.jsonl``.
    timeout_s
        How long to wait for an answer. voice-feasibility.md 7.2 locks
        this at 3 s.
    """

    tts_engine: Any
    listen_fn: Callable[[float], str]
    log_path: Path = field(
        default_factory=lambda: Path(__file__).resolve().parents[2]
        / "data"
        / "voice"
        / "confirmations.jsonl"
    )
    timeout_s: float = 3.0

    def confirm(self, prompt: str) -> ConfirmResult:
        self.tts_engine.speak(prompt)
        t0 = time.perf_counter()
        transcript = self.listen_fn(self.timeout_s) or ""
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        timed_out = not transcript.strip()
        decision = parse_yes_no(transcript)
        ambiguous = (not timed_out) and decision is None
        accepted = bool(decision)

        append_confirmation_log(
            self.log_path,
            prompt=prompt,
            transcript=transcript,
            decision=accepted,
            meta={
                "timed_out": timed_out,
                "ambiguous": ambiguous,
                "elapsed_ms": round(elapsed_ms, 2),
            },
        )
        return ConfirmResult(
            accepted=accepted,
            transcript=transcript,
            elapsed_ms=elapsed_ms,
            timed_out=timed_out,
            ambiguous=ambiguous,
        )


def bind_confirm(engine: ConfirmEngine):
    """Return a ``confirm_fn(ctx, prompt) -> bool`` for actions.handle_*."""

    def _fn(_ctx, prompt: str) -> bool:
        result = engine.confirm(prompt)
        return result.accepted

    return _fn
