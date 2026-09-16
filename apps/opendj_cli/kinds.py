"""Value kinds: how one CLI token becomes one bus command field.

Every parser here refuses out of domain with a message naming the argument and
its domain, so a bad value is a local refusal rather than a round trip to a
page that can only reject it. The domains are the ones the wire validator
enforces (``apps/webui/frontend/src/lib/rb/performance-ipc.svelte.ts``):
``unit`` is the bus's own 0..1 control value, where 0.5 is flat for EQ, TRIM
and FILTER.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

DECK_VALUES: tuple[int, ...] = (1, 2, 3, 4)

_BOOLS: dict[str, bool] = {
    "true": True, "1": True, "yes": True, "on": True,
    "false": False, "0": False, "no": False, "off": False,
}


def deck_value(key: str, raw: str) -> int:
    try:
        parsed = int(raw)
    except ValueError:
        raise ValueError(f"{key} must be a deck number 1..4, got {raw!r}") from None
    if parsed not in DECK_VALUES:
        raise ValueError(f"{key} must be a deck number 1..4, got {raw!r}")
    return parsed


def int_value(key: str, raw: str) -> int:
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{key} must be an integer, got {raw!r}") from None


def positive_int_value(key: str, raw: str) -> int:
    parsed = int_value(key, raw)
    if parsed <= 0:
        raise ValueError(f"{key} must be a positive integer, got {raw!r}")
    return parsed


def number_value(key: str, raw: str) -> float:
    try:
        parsed = float(raw)
    except ValueError:
        raise ValueError(f"{key} must be a number, got {raw!r}") from None
    if not math.isfinite(parsed):
        raise ValueError(f"{key} must be a finite number, got {raw!r}")
    return parsed


def unit_value(key: str, raw: str) -> float:
    parsed = number_value(key, raw)
    if parsed < 0.0 or parsed > 1.0:
        raise ValueError(
            f"{key} must be within 0..1 (bus control units; 0.5 is flat for EQ, "
            f"TRIM and FILTER), got {raw!r}"
        )
    return parsed


HEAD_DELAY_MAX_MS = 500  # keep in sync with apps/webui/frontend/src/lib/player/constants.ts


def head_delay_ms_value(key: str, raw: str) -> float:
    parsed = number_value(key, raw)
    if parsed < 0.0 or parsed > HEAD_DELAY_MAX_MS:
        raise ValueError(
            f"{key} must be within 0..{HEAD_DELAY_MAX_MS} (head delay milliseconds), "
            f"got {raw!r}"
        )
    return parsed


MASTER_DELAY_MAX_MS = 1500  # keep in sync with apps/webui/frontend/src/lib/player/constants.ts


def master_delay_ms_value(key: str, raw: str) -> float:
    parsed = number_value(key, raw)
    if parsed < 0.0 or parsed > MASTER_DELAY_MAX_MS:
        raise ValueError(
            f"{key} must be within 0..{MASTER_DELAY_MAX_MS} (room delay milliseconds), "
            f"got {raw!r}"
        )
    return parsed


def text_value(key: str, raw: str) -> str:
    if raw == "":
        raise ValueError(f"{key} must not be empty")
    return raw


def bool_value(key: str, raw: str) -> bool:
    try:
        return _BOOLS[raw.strip().lower()]
    except KeyError:
        raise ValueError(f"{key} must be true or false, got {raw!r}") from None


def enum_value(choices: Sequence[str]) -> Callable[[str, str], Any]:
    def parse(key: str, raw: str) -> Any:
        if raw in choices:
            return raw
        raise ValueError(
            f"{key} must be one of {'|'.join(str(choice) for choice in choices)}, "
            f"got {raw!r}"
        )

    return parse


def int_enum_value(choices: Sequence[int]) -> Callable[[str, str], Any]:
    def parse(key: str, raw: str) -> Any:
        parsed = int_value(key, raw)
        if parsed not in choices:
            raise ValueError(
                f"{key} must be one of {'|'.join(str(choice) for choice in choices)}, "
                f"got {raw!r}"
            )
        return parsed

    return parse


def loop_value(_key: str, raw: str) -> dict[str, float]:
    """``<in_ms> <out_ms>``: the two numbers the bus nests under ``loop``."""
    in_ms, out_ms = raw.split()
    return {"in_ms": number_value("in_ms", in_ms), "out_ms": number_value("out_ms", out_ms)}


def rescue_decks_value(key: str, raw: str) -> list[dict[str, Any]]:
    """``<deck>:<position_ms>[,<deck>:<position_ms>...]`` for rescue_resume."""
    pairs: list[dict[str, Any]] = []
    for chunk in raw.split(","):
        entry = chunk.strip()
        if entry == "":
            continue
        if ":" not in entry:
            raise ValueError(f"{key} entry must be deck:position_ms, got {entry!r}")
        deck_raw, pos_raw = entry.split(":", 1)
        deck = deck_value("deck", deck_raw.strip())
        position_ms = number_value("position_ms", pos_raw.strip())
        if position_ms < 0:
            raise ValueError(f"position_ms must be >= 0, got {position_ms!r}")
        pairs.append({"deck": deck, "position_ms": int(position_ms)})
    if not pairs:
        raise ValueError(f"{key} needs at least one deck:position_ms pair")
    return pairs


@dataclass(frozen=True)
class Arg:
    """One command field, parsed from one positional token (two for ``loop``)."""

    key: str
    kind: str
    parse: Callable[[str, str], Any]
    signature: str
    arity: int = 1
    optional: bool = False


def arg(
    key: str,
    kind: str,
    parse: Callable[[str, str], Any],
    hint: str = "",
    *,
    arity: int = 1,
    optional: bool = False,
) -> Arg:
    label = key if hint == "" else f"{key}:{hint}"
    return Arg(
        key=key,
        kind=kind,
        parse=parse,
        signature=f"<{label}>",
        arity=arity,
        optional=optional,
    )
