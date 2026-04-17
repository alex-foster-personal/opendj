"""Validators for PLAY-03 per-track overrides.

No silent coercion: each validator either accepts the value or raises
``ValueError`` with a message pointing at the offending field. Accept
lists are intentionally narrow -- the open-dj v0 schema ratifies these
ranges and anything outside them is a user bug, not a datum to normalise.
"""
from __future__ import annotations

from apps.shared.harmonic import _KEY_TO_CAMELOT

# A regenerable view of the accepted Camelot tokens, derived once at import
# time. Using ``frozenset`` keeps membership tests cheap.
_CAMELOT_TOKENS: frozenset[str] = frozenset(
    f"{n}{letter}" for n in range(1, 13) for letter in ("A", "B")
)

# Tempo acceptance: anything djay/Rekordbox realistically store.
_MIN_TEMPO: float = 20.0
_MAX_TEMPO: float = 300.0


def validate_target_key(value: str | None) -> None:
    """Accept Camelot (``1A``..``12B``), traditional key strings (``Am``,
    ``F#m``, ``C``, ...), or ``None``. Reject everything else.
    """
    if value is None:
        return
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"target_key must be None or a non-empty string, got {value!r}"
        )
    v = value.strip()
    if v in _CAMELOT_TOKENS:
        return
    if v in _KEY_TO_CAMELOT:
        return
    raise ValueError(
        f"target_key {value!r} is not a Camelot token (1A..12B) "
        "nor a recognised traditional key (Am, F#m, C, ...)."
    )


def validate_target_tempo(bpm: float | int | None) -> None:
    """Accept ``None`` or a positive float/int in ``[20.0, 300.0]``."""
    if bpm is None:
        return
    if isinstance(bpm, bool) or not isinstance(bpm, (int, float)):
        raise ValueError(
            f"target_tempo must be a number or None, got {bpm!r}"
        )
    if bpm <= 0:
        raise ValueError(
            f"target_tempo must be positive, got {bpm!r}"
        )
    if not (_MIN_TEMPO <= float(bpm) <= _MAX_TEMPO):
        raise ValueError(
            f"target_tempo {bpm!r} outside accepted range "
            f"[{_MIN_TEMPO}, {_MAX_TEMPO}]."
        )


def validate_key_sync(flag: bool | None) -> None:
    """Accept ``None``, ``True``, or ``False`` only (reject truthy ints)."""
    if flag is None:
        return
    if not isinstance(flag, bool):
        raise ValueError(
            f"key_sync must be bool or None, got {flag!r} "
            f"(type {type(flag).__name__})"
        )
