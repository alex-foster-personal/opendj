"""Grammar-only intent parser.

Deterministic regex parser over the 8 core intents (CONTEXT D3 +
RESEARCH section 5). No LLM in the hot path. Unmatched returns
``None`` so the daemon can TTS "didn't get that" and log the
transcript for post-set review.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


# ----- Number-word map (voice-feasibility.md 4.2 synonym table) ----------

NUMBER_WORDS: dict[str, int] = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}

_NUMBER_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in NUMBER_WORDS) + r")\b",
    flags=re.IGNORECASE,
)


def _normalise(text: str) -> str:
    t = text.strip().lower()
    # Collapse whitespace; sometimes STT puts double spaces around hesitations.
    t = re.sub(r"\s+", " ", t)

    def _repl(m: re.Match[str]) -> str:
        return str(NUMBER_WORDS[m.group(1).lower()])

    return _NUMBER_PATTERN.sub(_repl, t)


# ----- Intent definitions ----------------------------------------------------

# A rule is (intent-kind, compiled-regex, confidence, slot_names).
# Confidence = 1.0 for exact phrasings, 0.8 for loose synonyms.
# The regex is applied to the *normalised* transcript.


@dataclass(frozen=True)
class Intent:
    """Parsed intent."""

    kind: str
    slots: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    raw_transcript: str = ""


@dataclass(frozen=True)
class Rule:
    kind: str
    pattern: re.Pattern[str]
    confidence: float = 1.0


PATTERNS: list[Rule] = [
    # MUTE_VOICE / UNMUTE_VOICE before SEARCH (they start with commoner words).
    Rule("UNMUTE_VOICE", re.compile(r"^\s*un[- ]?mute\s+voice(?:\s+commands?)?\s*$")),
    Rule("MUTE_VOICE", re.compile(r"^\s*mute\s+voice(?:\s+commands?)?\s*$")),
    Rule(
        "READ_BPM",
        re.compile(
            r"^\s*(?:what(?:'?s|\s+is)?\s*)?(?:the\s+)?(?:this\s+)?"
            r"(?:bpm|tempo)\s*(?:of\s+this\s+(?:track|song))?\s*\??\s*$"
        ),
    ),
    Rule(
        "READ_KEY",
        re.compile(
            r"^\s*(?:what(?:'?s|\s+is)?\s*)?(?:the\s+)?(?:this\s+)?"
            r"key\s*(?:of\s+this\s+(?:track|song))?\s*\??\s*$"
        ),
    ),
    Rule(
        "ADVANCE_QUEUE",
        re.compile(
            r"^\s*(?:play\s+the\s+next(?:\s+(?:track|one))?|next\s+(?:track|one|up))\s*$"
        ),
    ),
    Rule(
        "SAVE_CUE",
        re.compile(
            r"^\s*(?:save|bookmark)\s+(?:that|the|this)?\s*(?:last\s+)?"
            r"transition(?:\s+as)?\s*(?:cue(?:\s+points?)?)?\s*$"
        ),
    ),
    Rule(
        "RATE_TRACK",
        re.compile(
            r"^\s*rate\s+(?:this|it|that)?\s*(?P<stars>\d+)\s+stars?\s*$"
        ),
    ),
    # SEARCH is last because it greedily swallows anything starting with "find".
    Rule(
        "SEARCH",
        re.compile(r"^\s*(?:find|search(?:\s+for)?)\s+(?P<query>.+?)\s*$"),
    ),
]


def parse(transcript: str) -> Intent | None:
    """Return the first matching intent, or ``None`` if nothing matches."""
    if transcript is None:
        return None
    normalised = _normalise(transcript)
    if not normalised:
        return None
    for rule in PATTERNS:
        m = rule.pattern.match(normalised)
        if m is None:
            continue
        slots: dict[str, Any] = {k: v for k, v in m.groupdict().items() if v is not None}
        # Cast numeric slots.
        if rule.kind == "RATE_TRACK" and "stars" in slots:
            try:
                slots["stars"] = int(slots["stars"])
            except (TypeError, ValueError):
                return None
        if rule.kind == "SEARCH" and "query" in slots:
            slots["query"] = slots["query"].strip()
        return Intent(
            kind=rule.kind,
            slots=slots,
            confidence=rule.confidence,
            raw_transcript=transcript,
        )
    return None
