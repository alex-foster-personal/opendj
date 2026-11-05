"""Set target length, skips and key clashes: the pure rules (LIBUX-8679).

One function per rule, mirrored line for line by
``apps/webui/frontend/src/lib/rb/set-target.ts``. Both sides run the cases in
``parity_cases.json`` so the UI readout and the HTTP/CLI/MCP summary cannot drift.

Played length (estimate of how long a track is heard in a mix):
- stored mix-in and mix-out points: ``mix_out - mix_in``;
- else duration minus a 32-bar overlap at the track's BPM, never below 50 %
  of the duration;
- no BPM: 75 % of the duration.

The estimate itself is :func:`apps.shared.played_length.estimate_track`
(LIBUX-8673, the multi-select readout); this module only sums it per set.

Camelot (adjacent NON-skipped tracks; skips are bridged):
- compatible: same code, +-1 wheel step with the same letter, or the same
  number with the other letter;
- anything else is a clash; a GROSS clash is 3+ wheel steps, or a letter change
  with 2+ steps. Unknown key on either side: no verdict.

-Claude
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from itertools import pairwise
from typing import Any

from apps.shared.played_length import PlayedLengthInput, estimate_track

_CAMELOT_RE = re.compile(r"^\s*(1[0-2]|[1-9])\s*([ABab])\s*$")


# ----------------------------------------------------------------- dataclasses


@dataclass(frozen=True)
class SetEntry:
    stable_id: str
    duration_s: float | None
    bpm: float | None
    key: str | None
    skipped: bool = False
    mix_in_s: float | None = None
    mix_out_s: float | None = None


@dataclass(frozen=True)
class KeyClash:
    from_id: str
    to_id: str
    from_key: str
    to_key: str
    steps: int
    gross: bool


@dataclass(frozen=True)
class SetSummary:
    played_s: float
    target_s: float | None
    over_s: float | None
    skipped: int
    clashes: list[KeyClash]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ----------------------------------------------------------------- played length


def played_length_s(
    duration_s: float | None,
    bpm: float | None,
    mix_in_s: float | None = None,
    mix_out_s: float | None = None,
) -> float:
    """Estimated seconds a track is heard in a mix (the shared LIBUX-8673 rule); 0 without a duration."""
    return estimate_track(PlayedLengthInput(duration_s, bpm, mix_in_s, mix_out_s))[0]


# ----------------------------------------------------------------- camelot


def parse_camelot(key: str | None) -> tuple[int, str] | None:
    """(number, letter) for a Camelot code like '8A'; None for unknown or other notation."""
    if not key:
        return None
    match = _CAMELOT_RE.match(key)
    if match is None:
        return None
    return int(match.group(1)), match.group(2).upper()


def wheel_steps(a: int, b: int) -> int:
    diff = abs(a - b)
    return min(diff, 12 - diff)


def key_clash(from_key: str | None, to_key: str | None) -> tuple[int, bool] | None:
    """(steps, gross) when the pair clashes; None when compatible or a key is unknown."""
    left, right = parse_camelot(from_key), parse_camelot(to_key)
    if left is None or right is None:
        return None
    steps = wheel_steps(left[0], right[0])
    letter_change = left[1] != right[1]
    if steps == 0 or (steps == 1 and not letter_change):
        return None
    gross = steps >= 3 or (letter_change and steps >= 2)
    return steps, gross


# ----------------------------------------------------------------- summary


def summarize_set(entries: list[SetEntry], target_s: float | None) -> SetSummary:
    """Played length of the non-skipped tracks, target delta and every adjacent key clash."""
    live = [e for e in entries if not e.skipped]
    played = sum(played_length_s(e.duration_s, e.bpm, e.mix_in_s, e.mix_out_s) for e in live)
    clashes: list[KeyClash] = []
    for left, right in pairwise(live):
        verdict = key_clash(left.key, right.key)
        if verdict is None:
            continue
        steps, gross = verdict
        clashes.append(KeyClash(left.stable_id, right.stable_id, str(left.key).strip().upper(),
                                str(right.key).strip().upper(), steps, gross))
    over = None if target_s is None else played - target_s
    return SetSummary(played_s=played, target_s=target_s, over_s=over,
                      skipped=len(entries) - len(live), clashes=clashes)


def format_readout(played_s: float, target_s: float | None) -> str:
    """'~48m of 60m · 12m left' / '~67m of 60m · 7m over' / '~48m' without a target."""
    played_m = round(played_s / 60)
    if target_s is None:
        return f"~{played_m}m"
    target_m = round(target_s / 60)
    delta = played_m - target_m
    if delta > 0:
        return f"~{played_m}m of {target_m}m · {delta}m over"
    return f"~{played_m}m of {target_m}m · {-delta}m left"
