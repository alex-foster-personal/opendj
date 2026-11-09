"""Deck mix weights from channel faders and crossfader (LIGHT-04 in the spec).

Mimics Engine Lighting blend / upfader-only behaviour and rekordbox single-target
(master-only) routing. Inputs match ``MixerChannelState`` in
``apps/webui/frontend/src/lib/rb/mixer-types.ts``: ``deck_id``, ``fader`` 0..1,
``assign`` A / B / THRU. Crossfader 0 = full A, 1 = full B. Side gain is linear
(A: ``1 - x``, B: ``x``, THRU: ``1``), not the audio path's equal-power curve.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Any, Final

__all__ = [
    "ALLOWED_MODES",
    "CrossfaderAssign",
    "DeckWeightMode",
    "crossfader_gain",
    "deck_weights",
]


class DeckWeightMode(StrEnum):
    BLEND = "blend"
    UPFADER_ONLY = "upfader_only"
    MASTER_ONLY = "master_only"


class CrossfaderAssign(StrEnum):
    A = "A"
    B = "B"
    THRU = "THRU"


ALLOWED_MODES: Final[tuple[str, ...]] = tuple(m.value for m in DeckWeightMode)

_ASSIGN_VALUES: Final[str] = "A, B, THRU"


def _parse_mode(mode: DeckWeightMode | str) -> DeckWeightMode:
    if isinstance(mode, DeckWeightMode):
        return mode
    try:
        return DeckWeightMode(mode)
    except ValueError as exc:
        allowed = ", ".join(ALLOWED_MODES)
        raise ValueError(
            f"unknown deck weight mode {mode!r}; allowed modes: {allowed}"
        ) from exc


def _validate_crossfader(crossfader: float) -> float:
    if isinstance(crossfader, bool) or not isinstance(crossfader, (int, float)):
        raise TypeError(f"crossfader must be a number in [0.0, 1.0], got {crossfader!r}")
    xf = float(crossfader)
    if not math.isfinite(xf):
        raise ValueError(f"crossfader must be a finite number in [0.0, 1.0], got {crossfader!r}")
    if xf < 0.0 or xf > 1.0:
        raise ValueError(f"crossfader must be in [0.0, 1.0], got {xf}")
    return xf


def _require_key(channel: Mapping[str, Any], key: str) -> Any:
    if key not in channel:
        raise ValueError(f"missing required channel key {key!r}")
    return channel[key]


def _parse_fader(deck_id: int, raw: Any) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise TypeError(f"deck {deck_id}: fader must be a number in [0, 1], got {raw!r}")
    fader = float(raw)
    if not math.isfinite(fader):
        raise ValueError(f"deck {deck_id}: fader must be a finite number in [0, 1], got {raw!r}")
    if fader < 0.0 or fader > 1.0:
        raise ValueError(f"deck {deck_id}: fader must be in [0, 1], got {fader}")
    return fader


def _parse_deck_id(raw: Any) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise TypeError(f"deck_id must be an int, got {raw!r}")
    return raw


def _parse_assign(deck_id: int, raw: Any) -> CrossfaderAssign:
    if not isinstance(raw, str):
        raise TypeError(
            f"deck {deck_id}: assign must be one of {_ASSIGN_VALUES}, got {raw!r}"
        )
    key = raw.upper()
    try:
        return CrossfaderAssign(key)
    except ValueError as exc:
        raise ValueError(
            f"deck {deck_id}: assign must be one of {_ASSIGN_VALUES}, got {raw!r}"
        ) from exc


def crossfader_gain(assign: CrossfaderAssign | str, crossfader: float) -> float:
    """Linear side gain for one crossfader assignment at position ``crossfader``."""
    if isinstance(assign, str):
        assign = CrossfaderAssign(assign.upper())
    xf = _validate_crossfader(crossfader)
    if assign is CrossfaderAssign.A:
        return 1.0 - xf
    if assign is CrossfaderAssign.B:
        return xf
    return 1.0


def deck_weights(
    channels: Iterable[Mapping[str, Any]],
    crossfader: float,
    master_deck: int | None,
    mode: DeckWeightMode | str,
) -> dict[int, float]:
    """Return one mix weight per ``deck_id`` for the given mode."""
    parsed_mode = _parse_mode(mode)
    xf = _validate_crossfader(crossfader)

    parsed: list[tuple[int, float, CrossfaderAssign]] = []
    seen: set[int] = set()
    for channel in channels:
        deck_id = _parse_deck_id(_require_key(channel, "deck_id"))
        if deck_id in seen:
            raise ValueError(f"duplicate deck_id {deck_id}")
        seen.add(deck_id)
        fader = _parse_fader(deck_id, _require_key(channel, "fader"))
        assign = _parse_assign(deck_id, _require_key(channel, "assign"))
        parsed.append((deck_id, fader, assign))

    if parsed_mode is DeckWeightMode.BLEND:
        return {
            deck_id: fader * crossfader_gain(assign, xf)
            for deck_id, fader, assign in parsed
        }

    if parsed_mode is DeckWeightMode.UPFADER_ONLY:
        return {deck_id: fader for deck_id, fader, _assign in parsed}

    # master_only
    if master_deck is None:
        deck_list = ", ".join(str(d) for d, _, _ in parsed) or "(none)"
        raise ValueError(f"master_deck is required in master_only mode; decks present: {deck_list}")
    deck_ids = {deck_id for deck_id, _, _ in parsed}
    if master_deck not in deck_ids:
        deck_list = ", ".join(str(d) for d in sorted(deck_ids)) or "(none)"
        raise ValueError(
            f"master_deck {master_deck} is not among channel decks: {deck_list}"
        )
    return {deck_id: (1.0 if deck_id == master_deck else 0.0) for deck_id, _, _ in parsed}
