"""AGENT-03 order documents, and the ``do ... then ... and ...`` script grammar.

The four order kinds are the bus's own (``single``, ``sequence``, ``parallel``,
``ramp``); this module only builds them. The script grammar is deliberately
small: ``then`` separates steps that must not overlap, ``and`` groups commands
the caller declares may run together. A group of one command is a ``single``
order and a group of several is a ``parallel`` one, and groups run one after
another with the sequence rule the bus already states - stop on the first
error and mark the rest skipped.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from apps.opendj_cli.catalog import RAMPABLE_TYPES
from apps.opendj_cli.verbs import Invocation, InvocationError, parse_invocation

SINGLE = "single"
SEQUENCE = "sequence"
PARALLEL = "parallel"
RAMP = "ramp"

SEQUENCE_WORD = "then"
PARALLEL_WORD = "and"
_SEPARATORS = (SEQUENCE_WORD, PARALLEL_WORD)

# A ramp is the ONE order the page deliberately holds open: `_ramp` in
# agent-orders.ts loops on a 16ms tick until the plan completes, and
# `executeAgentOrder` awaits it before posting a result. So the request
# deadline has to outlast the ramp the CLI itself asked for, or a perfectly
# healthy 32-bar move exits 5 (#1739).
#
# The tempo comes from the LIVE mirror, never a constant. A constant was tried
# and was wrong: `setTempoRatio` accepts any ratio inside the selected pitch
# range, and the +-100% range admits ratios down toward zero, so an effective
# tempo has no floor the engine enforces and no fixed "slower than any real
# deck" number is an upper bound.
#
# A beat and a bar ARE exact here (`_downbeats` groups the grid in fours). A
# phrase is not: `clock.phrases` comes from analysis and its rows vary, so 128
# beats is the nominal 32-bar phrase, an estimate rather than a bound. The
# margin below absorbs the ordinary case; an unusually long analyzed phrase
# would still need --timeout, which is why the deadline is reported in --json.
_BEATS_PER: dict[str, float] = {"beats": 1.0, "bars": 4.0, "phrases": 128.0}
# An anchored ramp does not start when the order is claimed: the page waits for
# the next beat, downbeat or phrase boundary FIRST, and only then travels, so
# the wait is part of what the page holds. Each entry is that wait at its
# longest, in beats, on the same nominal-phrase footing as above.
_ANCHOR_BEATS: dict[str, float] = {
    "next_beat": 1.0,
    "next_downbeat": 4.0,
    "next_phrase": 128.0,
}
RAMP_MARGIN_S = 10.0


def _anchor_beats(anchor: str | None) -> float:
    """The longest the page can wait for an anchor, in beats."""
    if anchor is None:
        return 0.0
    if anchor not in _ANCHOR_BEATS:
        raise InvocationError(f"cannot size a request deadline for anchor {anchor!r}")
    return _ANCHOR_BEATS[anchor]


def clock_beat_s(over: dict[str, Any], mirror: Mapping[str, Any]) -> float | None:
    """Wall seconds per beat on the deck this duration times against.

    ``None`` when the page will NOT hold the order open for beats, so the
    caller's floor stands. Each case is a refusal in `_ramp`
    (apps/webui/frontend/src/lib/rb/agent-orders.ts), not a long wait:

    * no elected master and no named clock -> ``no_master``
    * the clock deck is not playing -> ``clock_not_playing``

    plus the cases where the mirror simply cannot answer (deck absent, tempo
    null before analysis lands). Guessing a tempo for any of them is what the
    fixed constant did.

    ``effective_bpm`` is the right field and ``bpm`` is not: it is
    ``playbackBpm(..., tempoRatio: st.pitch, ...)``
    (apps/webui/frontend/src/lib/player/state.svelte.ts), so it already carries
    the pitch fader. A deck tagged 124 BPM running at ratio 0.5 travels a beat
    in 0.97s, not 0.48s, and only the pitched tempo says so.
    """
    clock = over.get("clock", "master")
    deck = mirror.get("master_deck") if clock in (None, "master") else clock
    if deck is None:
        return None
    decks = mirror.get("decks")
    if not isinstance(decks, Mapping):
        return None
    state = decks.get(str(deck))
    if not isinstance(state, Mapping) or state.get("playing") is not True:
        return None
    bpm = state.get("effective_bpm")
    if isinstance(bpm, bool) or not isinstance(bpm, (int, float)) or bpm <= 0:
        return None
    return 60.0 / float(bpm)


def ramp_hold_s(over: dict[str, Any], mirror: Mapping[str, Any]) -> float | None:
    """How long the page may hold a ramp order open, read off the live grid.

    ``None`` means the grid cannot answer, which is NOT the same as zero: zero
    would still collect the margin below and hand a refused order a 10s
    deadline it has no use for. A tool that cannot measure reports that it
    cannot measure.

    A beat-relative ramp covers the anchor wait as well as the travel, because
    `durationProgress` measures the clock deck's position against a plan whose
    ``start_ms`` sits at or after the anchor: `--over 4beats --anchor
    next_phrase` is a short move behind a wait that can approach a whole
    phrase.

    An ``ms`` ramp does NOT: its progress is `(performance.now() - startedAt) /
    over.n`, pure wall time, and the resolved plan goes unread. So an anchor on
    an ms duration costs the page no extra hold, and sizing for one here would
    be sizing for behavior production does not have.
    """
    unit = over["unit"]
    n = float(over["n"])
    if unit == "ms":
        return n / 1000.0
    if unit not in _BEATS_PER:
        raise InvocationError(f"cannot size a request deadline for duration unit {unit!r}")
    beats = n * _BEATS_PER[unit] + _anchor_beats(over.get("anchor"))
    beat_s = clock_beat_s(over, mirror)
    return None if beat_s is None else beats * beat_s


def ramp_deadline_s(
    over: dict[str, Any], floor_s: float, mirror: Mapping[str, Any]
) -> float:
    """``floor_s`` unless the ramp we asked for needs longer than that."""
    hold_s = ramp_hold_s(over, mirror)
    if hold_s is None:
        return floor_s
    return max(floor_s, hold_s + RAMP_MARGIN_S)


def single(invocation: Invocation) -> dict[str, Any]:
    return {SINGLE: invocation.command}


def parallel(invocations: Sequence[Invocation]) -> dict[str, Any]:
    return {PARALLEL: [invocation.command for invocation in invocations]}


def sequence(invocations: Sequence[Invocation]) -> dict[str, Any]:
    return {SEQUENCE: [invocation.command for invocation in invocations]}


def ramp(invocation: Invocation, over: dict[str, Any]) -> dict[str, Any]:
    """One control ramp: the verb's own value is the target it travels to."""
    command = invocation.command
    if command["type"] not in RAMPABLE_TYPES:
        raise InvocationError(
            f"--over ramps eq, fader, trim or filter; {command['type']} is not one "
            "of them (the page refuses any other command type)"
        )
    return {RAMP: {"command": command, "to": command["value"], "over": over}}


@dataclass(frozen=True)
class Group:
    """One bus order built from one or more verb invocations."""

    kind: str
    invocations: tuple[Invocation, ...]

    def order(self) -> dict[str, Any]:
        if self.kind == SINGLE:
            return single(self.invocations[0])
        if self.kind == PARALLEL:
            return parallel(self.invocations)
        raise AssertionError(f"unknown group kind {self.kind}")

    def label(self) -> str:
        return " and ".join(_label(invocation) for invocation in self.invocations)


def _label(invocation: Invocation) -> str:
    parts = [invocation.verb.name]
    parts.extend(
        str(invocation.values[argument.key])
        for argument in invocation.verb.args
        if argument.key in invocation.values
    )
    return " ".join(parts)


def _tokenize(command: str) -> list[str]:
    """Split one quoted script command the way a shell would."""
    try:
        return shlex.split(command)
    except ValueError as error:
        raise InvocationError(f"cannot read {command!r}: {error}") from None


def _reject_stray_separators(items: Sequence[str]) -> None:
    """``then``/``and`` are infixes: one at either end joins nothing."""
    for index, item in enumerate(items):
        if item not in _SEPARATORS:
            continue
        if index == 0 or index == len(items) - 1 or items[index + 1] in _SEPARATORS:
            raise InvocationError(
                f"`{item}` must sit between two quoted commands, as in: "
                'opendj do "load 1 x" then "play 1" and "play 2"'
            )


def _segments(items: Sequence[str]) -> list[list[str]]:
    """Group the quoted commands: ``and`` joins, anything else starts a step."""
    segments: list[list[str]] = []
    for index, item in enumerate(items):
        if item in _SEPARATORS:
            continue
        if index > 0 and items[index - 1] == PARALLEL_WORD and segments:
            segments[-1].append(item)
        else:
            segments.append([item])
    return segments


def parse_script(items: Sequence[str], deck_scope: int | None = None) -> tuple[Group, ...]:
    """Parse the tokens after ``do`` into the groups to run, in order."""
    if not items:
        raise InvocationError("do needs at least one quoted command")
    _reject_stray_separators(items)
    groups = []
    for segment in _segments(items):
        # shlex, not str.split: a text argument may contain spaces, and
        # `hot_cue_save 1 A 100 rev "my comment"` has to mean the same thing
        # inside `do` as it does on the command line. str.split() both breaks
        # the value apart AND keeps the quote characters, so nesting quotes
        # could not rescue it.
        invocations = tuple(
            parse_invocation(_tokenize(tokens), deck_scope) for tokens in segment
        )
        kind = SINGLE if len(invocations) == 1 else PARALLEL
        groups.append(Group(kind=kind, invocations=invocations))
    return tuple(groups)


__all__ = [
    "PARALLEL",
    "RAMP",
    "SEQUENCE",
    "SINGLE",
    "Group",
    "parallel",
    "parse_script",
    "ramp",
    "sequence",
    "single",
]


def slowed_since(
    over: dict[str, Any], beat_s: float | None, mirror: Mapping[str, Any]
) -> str | None:
    """Has the ramp clock SLOWED since a deadline was sized at ``beat_s``?

    Returns the sentence to say so, or ``None`` when it has not, which includes
    the clock speeding up and the mirror no longer being able to answer. Only a
    slowdown can make a deadline too short; reporting anything else would blame
    the clock for a page that is simply wedged, and those two demand opposite
    responses from whoever reads the error.
    """
    if beat_s is None:
        return None
    now_s = clock_beat_s(over, mirror)
    if now_s is None or now_s <= beat_s:
        return None
    return (
        f"The ramp clock has SLOWED since that deadline was sized "
        f"({60.0 / beat_s:.4g} BPM then, {60.0 / now_s:.4g} BPM now), so the page is "
        "most likely still running this ramp rather than wedged; re-run with a "
        "larger --timeout"
    )
