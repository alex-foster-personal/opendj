"""Value kinds: how one CLI token becomes one bus command field.

Every parser here refuses out of domain with a message naming the argument and
its domain, so a bad value is a local refusal rather than a round trip to a
page that can only reject it. The domains are the ones the wire validator
enforces (``apps/webui/frontend/src/lib/rb/performance-ipc.svelte.ts``):
``unit`` is the bus's own 0..1 control value, where 0.5 is flat for EQ, TRIM
and FILTER.
"""

from __future__ import annotations

import json
import math
import re
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


def deck_or_master_value(key: str, raw: str) -> int | str:
    """KEYSYNC-01: a deck number 1..4, or ``master``."""
    return "master" if raw == "master" else deck_value(key, raw)


def deck_or_all_value(key: str, raw: str) -> int | str:
    """COLORFX-13: a deck number 1..4, or ``all`` (every channel)."""
    return "all" if raw == "all" else deck_value(key, raw)


def int_value(key: str, raw: str) -> int:
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{key} must be an integer, got {raw!r}") from None


def related_bpm_percent_value(key: str, raw: str) -> int:
    """LIBUX-10046: Related Tracks BPM window percent; keep in sync with related-criteria-prefs.ts."""
    parsed = number_value(key, raw)
    if not parsed.is_integer() or parsed < 1 or parsed > 20:
        raise ValueError(f"{key} must be an integer within 1..20 (percent), got {raw!r}")
    return int(parsed)


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


def fraction_enum_value(choices: Sequence[float]) -> Callable[[str, str], Any]:
    """A number written as a decimal or ``a/b`` that lands within 1e-9 of one of ``choices`` (returns the choice)."""

    def parse(key: str, raw: str) -> Any:
        numerator, slash, denominator = raw.partition("/")
        parsed = number_value(key, numerator) / number_value(key, denominator) if slash else number_value(key, raw)
        for choice in choices:
            if abs(parsed - choice) <= 1e-9:
                return choice
        raise ValueError(f"{key} must be one of {'|'.join(f'{choice:g}' for choice in choices)}, got {raw!r}")

    return parse


def beat_jump_beats_value(key: str, raw: str) -> int | float:
    """A non-zero whole beat count, or exactly +-0.5 (DECKUX-10470 half-beat jump)."""
    parsed = number_value(key, raw)
    if abs(parsed) == 0.5:
        return parsed
    if parsed == 0 or not parsed.is_integer():
        raise ValueError(f"{key} must be a non-zero whole number or +-0.5, got {raw!r}")
    return int(parsed)


ROLL_BEATS_VALUES: tuple[float, ...] = (1 / 32, 1 / 16, 1 / 8, 1 / 4, 1 / 2, 1, 2, 4, 8, 16, 32)


def roll_beats_value(key: str, raw: str) -> int | float:
    """One of ROLL_BEATS_VALUES; accepts a decimal or a/b fraction (FXROLL-01)."""
    if "/" in raw:
        parts = raw.split("/", 1)
        if len(parts) != 2:
            raise ValueError(f"{key} must be one of 1/32|1/16|1/8|1/4|1/2|1|2|4|8|16|32, got {raw!r}")
        try:
            numer = float(parts[0].strip())
            denom = float(parts[1].strip())
        except ValueError:
            raise ValueError(f"{key} must be one of 1/32|1/16|1/8|1/4|1/2|1|2|4|8|16|32, got {raw!r}") from None
        if not math.isfinite(numer) or not math.isfinite(denom) or denom == 0:
            raise ValueError(f"{key} must be one of 1/32|1/16|1/8|1/4|1/2|1|2|4|8|16|32, got {raw!r}")
        parsed = numer / denom
    else:
        parsed = number_value(key, raw)
    if parsed not in ROLL_BEATS_VALUES:
        raise ValueError(f"{key} must be one of 1/32|1/16|1/8|1/4|1/2|1|2|4|8|16|32, got {raw!r}")
    return int(parsed) if parsed.is_integer() else parsed


def beat_jump_size_value(choices: Sequence[float]) -> Callable[[str, str], Any]:
    """One of ``choices``, sent as an int when whole so the wire reads 4, not 4.0."""
    sized = number_enum_value(choices)

    def parse(key: str, raw: str) -> Any:
        parsed = sized(key, raw)
        return int(parsed) if parsed.is_integer() else parsed

    return parse


def positive_number_value(key: str, raw: str) -> float:
    parsed = number_value(key, raw)
    if parsed <= 0:
        raise ValueError(f"{key} must be a positive number, got {raw!r}")
    return parsed


def unit_value(key: str, raw: str) -> float:
    parsed = number_value(key, raw)
    if parsed < 0.0 or parsed > 1.0:
        raise ValueError(
            f"{key} must be within 0..1 (bus control units; 0.5 is flat for EQ, "
            f"TRIM and FILTER), got {raw!r}"
        )
    return parsed


# DIALCLOCK-8341: dial positions as clock times. Twin of
# apps/webui/frontend/src/lib/rb/dial-clock.ts; the dial sweeps 270 degrees,
# 7:30 (0) through 12 o'clock (0.5) to 4:30 (1), 30 degrees per hour.
_DIAL_HALF_SWEEP_HOURS = 4.5
_CLOCK_PATTERN = re.compile(
    r"^\s*(\d{1,2})(?::(\d{2}))?\s*(?:o'?\s*clock|am|pm|a\.m\.|p\.m\.)?\s*$", re.IGNORECASE
)
_CLOCK_MARKER = re.compile(r":|o'?\s*clock|am|pm|a\.m\.|p\.m\.", re.IGNORECASE)


def dial_clock_value(key: str, raw: str) -> float:
    """A clock time on the dial ("3", "3pm", "3 o'clock", "15:00", "4:30") as 0..1.

    AM/PM and the 24-hour clock only mark the token as a time: 3pm, 3am, 15:00
    and 3 o'clock are the same position. The gap at the bottom of the dial
    (after 4:30, before 7:30) is refused.
    """
    match = _CLOCK_PATTERN.match(raw)
    if match is None:
        raise ValueError(f"{key} must be a clock time like 3, 3pm, 3 o'clock, 3:00 or 15:00, got {raw!r}")
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if hour > 24 or minute > 59:
        raise ValueError(f"{key} is not a time of day, got {raw!r}")
    on_dial = ((hour % 12) + minute / 60) % 12
    signed = on_dial - 12 if on_dial > 6 else on_dial
    if abs(signed) > _DIAL_HALF_SWEEP_HOURS + 1e-9:
        raise ValueError(
            f"{key} {raw!r} is past the dial's stops: it turns from 7:30 (full left, 0) "
            f"through 12 o'clock (centre, 0.5) to 4:30 (full right, 1)"
        )
    return 0.5 + signed / (2 * _DIAL_HALF_SWEEP_HOURS)


def dial_value(key: str, raw: str) -> float:
    """A bipolar dial: a 0..1 number, or a clock time (DIALCLOCK-8341).

    A plain number in 0..1 is the dial value, so ``1`` stays full right (say
    ``1pm`` or ``1 o'clock`` for one o'clock). A bare whole number 2..24 has no
    dial-value reading and is the hour; anything with a colon, am/pm or
    o'clock is a clock time.
    """
    if _CLOCK_MARKER.search(raw) is not None:
        return dial_clock_value(key, raw)
    parsed = number_value(key, raw)
    if 0.0 <= parsed <= 1.0:
        return parsed
    if parsed.is_integer() and 2 <= parsed <= 24:
        return dial_clock_value(key, raw)
    raise ValueError(
        f"{key} must be within 0..1 (0.5 = centre) or a clock time like 3pm, "
        f"3 o'clock or 15:00 (12 o'clock = centre), got {raw!r}"
    )


HEAD_DELAY_MAX_MS = 500  # keep in sync with apps/webui/frontend/src/lib/player/constants.ts


def head_delay_ms_value(key: str, raw: str) -> float:
    parsed = number_value(key, raw)
    if parsed < 0.0 or parsed > HEAD_DELAY_MAX_MS:
        raise ValueError(
            f"{key} must be within 0..{HEAD_DELAY_MAX_MS} (head delay milliseconds), "
            f"got {raw!r}"
        )
    return parsed


LAYOUT_REGION_BOUNDS: dict[str, tuple[int, int]] = {
    "browser.tree": (220, 520),  # keep in sync with PLAYLIST_TREE_WIDTH_MIN/MAX in apps/webui/frontend/src/lib/rb/prefs.svelte.ts
}


def layout_px_value(key: str, raw: str) -> int:
    """DL-2: a region width in CSS px; out of range is refused with the range, never clamped.

    Single-region unpack: when a second region lands, unpacking ``values()`` fails loudly
    and forces a cross-field check against the chosen region.
    """
    (low, high), = LAYOUT_REGION_BOUNDS.values()
    parsed = number_value(key, raw)
    if not parsed.is_integer() or parsed < low or parsed > high:
        raise ValueError(
            f"{key} must be a whole number within {low}..{high} (browser.tree width in CSS px), got {raw!r}"
        )
    return int(parsed)


AUTO_GAIN_OFFSET_MAX_DB = 12.0  # apps/analysis_loudness/auto_gain.py OFFSET_MAX_DB


def auto_gain_offset_value(key: str, raw: str) -> float | None:
    """AUTOGAIN-04: the user's auto gain dB, or ``clear`` for the analysed value."""
    if raw == "clear":
        return None
    parsed = number_value(key, raw)
    if abs(parsed) > AUTO_GAIN_OFFSET_MAX_DB:
        raise ValueError(f"{key} must be within -12..12 dB or clear, got {raw!r}")
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


def json_object_value(key: str, raw: str) -> dict[str, Any]:
    """One JSON object token; the browser validates its fields (issue #10043 library_filter_set)."""
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(f"{key} must be a JSON object, got {raw!r}: {error.msg}") from None
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be a JSON object, got {raw!r}")
    return value


def bool_value(key: str, raw: str) -> bool:
    try:
        return _BOOLS[raw.strip().lower()]
    except KeyError:
        raise ValueError(f"{key} must be true or false, got {raw!r}") from None


def false_only_value(key: str, raw: str) -> bool:
    """A flag whose only explicit value is false: omitting it is the default."""
    if bool_value(key, raw) is not False:
        raise ValueError(f"{key} may only be false (omit it for the default), got {raw!r}")
    return False


def enum_value(choices: Sequence[str]) -> Callable[[str, str], Any]:
    def parse(key: str, raw: str) -> Any:
        if raw in choices:
            return raw
        raise ValueError(
            f"{key} must be one of {'|'.join(str(choice) for choice in choices)}, "
            f"got {raw!r}"
        )

    return parse


def number_enum_value(choices: Sequence[float]) -> Callable[[str, str], Any]:
    def parse(key: str, raw: str) -> Any:
        parsed = number_value(key, raw)
        if parsed not in choices:
            raise ValueError(
                f"{key} must be one of {'|'.join(f'{choice:g}' for choice in choices)}, "
                f"got {raw!r}"
            )
        return parsed

    return parse


def quantize_grid_value(choices: Sequence[int]) -> Callable[[str, str], Any]:
    """A whole-beat grid from ``choices``, or ``phase`` (DECKCTL-6235c: snap to phrase starts)."""
    beats = int_enum_value(choices)

    def parse(key: str, raw: str) -> Any:
        return "phase" if raw == "phase" else beats(key, raw)

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


# Mirrors GRID_ADJUST_MAX_OFFSET_MS and the whole-beat bound in
# apps/webui/frontend/src/lib/rb/grid-edit-command.ts (parseGridEditIntent).
GRID_EDIT_MAX_OFFSET_MS = 500.0
GRID_EDIT_MAX_STEP_BEATS = 3


def grid_bpm_override_parts(bpm_raw: str, anchor_raw: str) -> tuple[float, float]:
    """``bpm`` and ``anchor_t`` for PUT ``/api/v1/tracks/{id}/grid-edit`` (BEATGRID-13519)."""
    return positive_number_value("bpm", bpm_raw), number_value("anchor_t", anchor_raw)


def grid_edit_value(key: str, raw: str) -> dict[str, Any]:
    """``adjust|shift`` resets that part, ``undo`` steps back one stored edit
    (GRIDEDIT-10215); ``<offset_ms>[:<beats>]`` nudges the grid. Constant-tempo
    overrides use ``opendj track grid-edit <id> bpm <bpm> <anchor_t>`` (HTTP PUT)."""
    token = raw.strip()
    if token == "undo":
        return {"kind": "undo"}
    if token in ("adjust", "shift"):
        return {"kind": "reset", "part": token}
    offset_raw, _, beats_raw = token.partition(":")
    offset_ms = number_value(key, offset_raw)
    if abs(offset_ms) > GRID_EDIT_MAX_OFFSET_MS:
        raise ValueError(f"{key} offset_ms must be within +/-{GRID_EDIT_MAX_OFFSET_MS:g}; got {offset_raw}")
    beats = int_value(key, beats_raw) if beats_raw != "" else 0
    if abs(beats) > GRID_EDIT_MAX_STEP_BEATS:
        raise ValueError(f"{key} beats must be a whole number within +/-{GRID_EDIT_MAX_STEP_BEATS}; got {beats_raw}")
    return {"kind": "nudge", "offset_ms": offset_ms, "beats": beats}


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


RESET_PART_VALUES: tuple[str, ...] = ("level", "stems", "eq", "trim", "filter")


def reset_parts_value(key: str, raw: str) -> list[str]:
    """``<part>[,<part>...]`` for reset_deck_now (DECKGAP-7691h): distinct reset items."""
    parts = [chunk.strip() for chunk in raw.split(",") if chunk.strip() != ""]
    if not parts:
        raise ValueError(f"{key} needs at least one of {'|'.join(RESET_PART_VALUES)}")
    for part in parts:
        if part not in RESET_PART_VALUES:
            raise ValueError(f"{key} item must be one of {'|'.join(RESET_PART_VALUES)}, got {part!r}")
    if len(set(parts)) != len(parts):
        raise ValueError(f"{key} names an item twice: {raw!r}")
    return parts


# SAMPLER: twin of parseRef in apps/webui/frontend/src/lib/player/sampler/sampler-store-logic.ts.
_SAMPLER_REF = re.compile(r"^(?:S[1-9]\d*|R)-\d{2,}$")


def sampler_ref_value(key: str, raw: str) -> str:
    """An edge tab reference: ``S<set>-<nn>`` (1-based) or ``R-<nn>`` for search results."""
    if not _SAMPLER_REF.match(raw) or raw.endswith("-00"):
        raise ValueError(f"{key} must look like S1-05 or R-03, got {raw!r}")
    return raw


def any_text_value(_key: str, raw: str) -> str:
    """Free text that may be empty (a cleared search, a blank description)."""
    return raw


# SAMPLER: twins of the settings guard in sampler-ipc-guards.ts (sampler-types.ts SlotSettings).
SAMPLER_SETTINGS_ENUMS: dict[str, tuple[str, ...]] = {
    "launch": ("bar", "beat", "4bars", "off"),
    "key": ("auto", "master_tempo", "off"),
    "tempo": ("match", "own"),
    "mode": ("loop", "one_shot", "gate"),
}
SAMPLER_REPEAT_VALUES: tuple[str | int, ...] = ("once", "loop", 4, 2, 8)
SAMPLER_INT_RANGES: dict[str, tuple[int, int]] = {"downbeat_offset": (-3, 3), "level": (0, 100)}


def _sampler_setting(key: str, name: str, value: str) -> str | int:
    if name in SAMPLER_SETTINGS_ENUMS:
        parsed: str | int = enum_value(SAMPLER_SETTINGS_ENUMS[name])(f"{key}.{name}", value)
        return parsed
    if name == "repeat":
        repeat: str | int = int(value) if value.isdigit() else value
        if repeat not in SAMPLER_REPEAT_VALUES:
            raise ValueError(f"{key}.repeat must be one of once|loop|4|2|8, got {value!r}")
        return repeat
    if name in SAMPLER_INT_RANGES:
        low, high = SAMPLER_INT_RANGES[name]
        number = int_value(f"{key}.{name}", value)
        if number < low or number > high:
            raise ValueError(f"{key}.{name} must be within {low}..{high}, got {value!r}")
        return number
    fields = "|".join([*SAMPLER_SETTINGS_ENUMS, "repeat", *SAMPLER_INT_RANGES])
    raise ValueError(f"{key} field must be one of {fields}, got {name!r}")


def sampler_settings_value(key: str, raw: str) -> dict[str, Any]:
    """``field:value[,field:value...]`` -> a partial SlotSettings, e.g. ``launch:bar,level:80``."""
    out: dict[str, Any] = {}
    for chunk in raw.split(","):
        name, sep, value = chunk.strip().partition(":")
        if not sep:
            raise ValueError(f"{key} entry must be field:value, got {chunk!r}")
        if name in out:
            raise ValueError(f"{key} names {name} twice")
        out[name] = _sampler_setting(key, name, value)
    return out


# SAMPLER: twins of classic-sampler-logic.ts (4 banks x 16 slots, 0-based; gain domains in dB).
SAMPLER_UNIT_INT_RANGES: dict[str, tuple[int, int]] = {"bank": (0, 3), "slot": (0, 15)}
SAMPLER_UNIT_DB_RANGES: dict[str, tuple[float, float]] = {"slot": (-24.0, 6.0), "master": (-60.0, 0.0)}


def sampler_unit_index_value(key: str, raw: str) -> int:
    """A 0-based classic sampler bank (0..3) or slot (0..15), keyed by the arg name."""
    low, high = SAMPLER_UNIT_INT_RANGES[key]
    parsed = int_value(key, raw)
    if parsed < low or parsed > high:
        raise ValueError(f"{key} must be a whole number within {low}..{high} (0-based), got {raw!r}")
    return parsed


def sampler_unit_db_value(scope: str) -> Callable[[str, str], float]:
    """A gain in dB inside the slot (-24..+6) or master (-60..0) domain."""
    low, high = SAMPLER_UNIT_DB_RANGES[scope]

    def parse(key: str, raw: str) -> float:
        parsed = number_value(key, raw)
        if parsed < low or parsed > high:
            raise ValueError(f"{key} must be within {low:g}..{high:g} dB, got {raw!r}")
        return parsed

    return parse


def non_negative_int_value(key: str, raw: str) -> int:
    """A whole number of zero or more (a 0-based index)."""
    parsed = int_value(key, raw)
    if parsed < 0:
        raise ValueError(f"{key} must be a whole number >= 0, got {raw!r}")
    return parsed


def nonzero_int_value(key: str, raw: str) -> int:
    """A whole number other than zero (a signed page step)."""
    parsed = int_value(key, raw)
    if parsed == 0:
        raise ValueError(f"{key} must be a non-zero whole number, got {raw!r}")
    return parsed


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
