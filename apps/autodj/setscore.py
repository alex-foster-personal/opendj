"""Set score: a whole set as one document, the track list plus each transition (AGENT-27).

A :class:`SetPlan` is an ordered chain of :class:`~apps.autodj.plan.TransitionPlan`
where each transition's incoming deck and track is the next one's outgoing
deck and track. It is what an agent writes to plan a set in one go, and what
a recorded performance folds into (AGENT-28), so one format serves AI control
and replay.

The text form is a mix score with a set header, one ``transition`` block per
handover::

    set "warmup"
    intent "keep the energy up"

    transition bass_swap_blend
    out D1 trk-a from 97 for 16
    in  D2 trk-b from 1 for 16
    ...

    transition cut
    out D2 trk-b from 81 for 1
    in  D1 trk-c from 17 for 1
    ...

As with a single score, the JSON (:meth:`SetPlan.to_json`) is canonical and
:func:`print_set_score` writes one text that :func:`parse_set_score` reads back
to the same set. Nothing here talks to the engine: :func:`next_transition`
reads a mirror snapshot and says which transition is due, and the service
schedules it through the same executor a single plan uses.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

from apps.autodj.notation import (
    ScoreError,
    print_score,
    score_document_lines,
    score_quoted,
    score_text,
    score_tokens,
)
from apps.autodj.plan import PlanError, TransitionPlan, parse_plan
from apps.autodj.profile import TrackProfile
from apps.autodj.propose import propose_transition

#: The partner deck a planned set alternates to (1 <-> 2, 3 <-> 4).
PARTNER = {1: 2, 2: 1, 3: 4, 4: 3}
#: Statuses of the page's ``agent_plan`` mirror that mean a plan holds the decks.
BUSY_STATUSES = frozenset({"scheduled", "running"})
#: Every status the page publishes (``AgentPlanStatus`` in ``agent-plan.ts``).
PLAN_STATUSES = BUSY_STATUSES | {"done", "aborted", "failed"}
#: The decks the page's mirror publishes, always all four.
MIRROR_DECKS = frozenset({"1", "2", "3", "4"})
#: The keys a set JSON may hold; anything else is refused.
SET_KEYS = frozenset({"set", "intent", "tracks", "transitions"})


@dataclass(frozen=True)
class SetPlan:
    transitions: tuple[TransitionPlan, ...]
    name: str | None = None
    intent: str | None = None

    def __post_init__(self) -> None:
        if not self.transitions:
            raise PlanError("a set needs at least one transition")
        errors = chain_errors(self.transitions)
        if errors:
            raise PlanError("; ".join(errors))

    @property
    def tracks(self) -> list[dict[str, Any]]:
        """The running order: the first outgoing track, then each incoming one."""
        first = self.transitions[0].out
        rows = [{"deck": first.deck, "track": first.track}]
        rows.extend({"deck": plan.in_.deck, "track": plan.in_.track} for plan in self.transitions)
        return rows

    def to_json(self) -> dict[str, Any]:
        return {
            "set": self.name,
            "intent": self.intent,
            "tracks": self.tracks,
            # to_document, not to_json: each transition keeps its extension keys.
            "transitions": [plan.to_document() for plan in self.transitions],
        }


#: The lead the live validator needs before an outgoing window starts (plan.py _check_out_position).
SCHEDULE_LEAD_BARS = 0.5


def in_end_bar(plan: TransitionPlan) -> float:
    """The incoming track's own bar when the transition ends."""
    plays = [e.at_bar for e in plan.events if e.kind == "do" and e.name == "play" and e.deck == plan.in_.deck]
    entered = plays[0] if plays else 0.0
    return plan.in_.start_bar + plan.bars - entered


def chain_errors(transitions: Sequence[TransitionPlan]) -> list[str]:
    """Why the transitions do not form one set (empty when they do).

    Each transition needs two decks and two tracks, each track plays once in
    the whole set, and consecutive transitions hand over to each other.
    """
    errors: list[str] = []
    for index, plan in enumerate(transitions, start=1):
        if plan.out.track == plan.in_.track:
            errors.append(
                f"transition {index}: mixes {plan.out.track} into itself; a set never hands a track over to itself"
            )
        if plan.out.deck == plan.in_.deck:
            errors.append(f"transition {index}: mixes out of and into D{plan.out.deck}; a handover needs two decks")
    errors.extend(_repeat_errors(transitions))
    for index, (before, after) in enumerate(pairwise(transitions), start=1):
        where = f"transition {index + 1}"
        if (after.out.deck, after.out.track) != (before.in_.deck, before.in_.track):
            errors.append(
                f"{where} mixes out of D{after.out.deck} {after.out.track}, but transition {index} "
                f"brought in D{before.in_.deck} {before.in_.track}"
            )
            continue
        ends = in_end_bar(before)
        if after.out.start_bar < ends:
            errors.append(
                f"{where} leaves {after.out.track} at bar {after.out.start_bar}, but transition "
                f"{index} only finishes at its bar {ends:g}"
            )
    return errors


def _repeat_errors(transitions: Sequence[TransitionPlan]) -> list[str]:
    """A track brought in twice (or brought back after it left) would make the set loop."""
    if not transitions:
        return []
    seen = {transitions[0].out.track: 0}
    errors: list[str] = []
    for index, plan in enumerate(transitions, start=1):
        track = plan.in_.track
        if track in seen and track != plan.out.track:  # a self-handover is reported above
            where = "the first track" if seen[track] == 0 else f"transition {seen[track]}"
            errors.append(
                f"transition {index}: brings in {track} again ({where} already played it); "
                "a set plays each track once"
            )
        seen.setdefault(track, index)
    return errors


def _optional_text(document: Mapping[str, Any], key: str) -> str | None:
    value = document.get(key)
    if value is not None and not isinstance(value, str):
        raise PlanError(f"{key} must be a string or null, got {type(value).__name__}")
    return value


def parse_set(document: Any) -> SetPlan:
    """A :class:`SetPlan` from its JSON, or :class:`PlanError` naming what is wrong."""
    if not isinstance(document, Mapping):
        raise PlanError("a set must be a JSON object with transitions")
    unknown = sorted(str(key) for key in document if key not in SET_KEYS)
    if unknown:
        raise PlanError(f"unknown key {unknown[0]!r} in the set; a set holds only {', '.join(sorted(SET_KEYS))}")
    raw = document.get("transitions")
    if not isinstance(raw, list) or not raw:
        raise PlanError("transitions must be a non-empty list of plans")
    transitions: list[TransitionPlan] = []
    for index, plan in enumerate(raw):
        try:
            transitions.append(parse_plan(plan))
        except PlanError as error:
            raise PlanError(f"transitions[{index}]: {error}") from error
    parsed = SetPlan(  # refuses a set that does not chain
        transitions=tuple(transitions),
        name=_optional_text(document, "set"),
        intent=_optional_text(document, "intent"),
    )
    tracks = document.get("tracks")
    if tracks is not None and not _same_tracks(tracks, parsed.tracks):
        raise PlanError("tracks does not match the transitions' decks and tracks; drop it or fix it")
    return parsed


def _same_tracks(given: Any, expected: list[dict[str, Any]]) -> bool:
    """``given`` is ``expected`` with the same types (a deck ``true`` or ``2.0`` is not deck 1 or 2)."""
    if not isinstance(given, list) or len(given) != len(expected):
        return False
    return all(
        isinstance(row, Mapping) and type(row.get("deck")) is int and dict(row) == want
        for row, want in zip(given, expected, strict=True)
    )


def _set_header(document: dict[str, Any], tokens: list[str], where: str) -> None:
    if len(tokens) != 2 or tokens[0] not in ("set", "intent"):
        raise ScoreError(f"{where}: before the first transition only 'set <name>' and 'intent \"...\"' belong")
    if tokens[0] in document:
        raise ScoreError(f"{where}: a second {tokens[0]!r} line; each set header line may appear once")
    document[tokens[0]] = score_text(tokens[1], where)


def _block_starts(lines: list[str]) -> list[int]:
    return [i for i, line in enumerate(lines) if (score_tokens(line) or [""])[0] == "transition"]


def set_score_document(text: str) -> dict[str, Any]:
    """The set JSON a set score describes.

    Each block is checked as a plan on the way, so its errors name the block
    and line; the set as a whole (the chain between blocks) is not yet checked.
    """
    document: dict[str, Any] = {"transitions": []}
    lines = text.splitlines()
    starts = _block_starts(lines)
    if not starts:
        raise ScoreError("a set score needs at least one 'transition <style>' block")
    for index, raw in enumerate(lines[: starts[0]]):
        tokens = score_tokens(raw)
        if tokens:
            _set_header(document, tokens, f"line {index + 1}")
    for block, start in enumerate(starts):
        end = starts[block + 1] if block + 1 < len(starts) else len(lines)
        try:
            plan, field_lines = score_document_lines("\n".join(lines[start:end]), first_line=start + 1)
        except ScoreError as error:  # a syntax error already names its own line
            raise ScoreError(f"transition {block + 1} (line {start + 1}): {error}") from error
        try:
            parse_plan(plan)  # refuse a bad block here, where its line is known
        except PlanError as error:
            # A field the map does not hold (the block as a whole) points at the block's first line.
            line = field_lines.get(error.path or "", start + 1)
            raise ScoreError(f"transition {block + 1} (line {start + 1}): line {line}: {error}") from error
        document["transitions"].append(plan)
    return document


_CHAIN_TRANSITION_RE = re.compile(r"transition (\d+): ")


def parse_set_score(text: str) -> SetPlan:
    """A :class:`SetPlan` from a set score; a chain error names the file line of the transition it is about."""
    try:
        return parse_set(set_score_document(text))
    except ScoreError:
        raise
    except PlanError as error:
        lines = text.splitlines()
        starts = _block_starts(lines)

        def named(match: re.Match[str]) -> str:
            block = int(match.group(1))
            return f"transition {block} (line {starts[block - 1] + 1}): " if 0 < block <= len(starts) else match.group(0)

        raise ScoreError(_CHAIN_TRANSITION_RE.sub(named, str(error))) from error


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """A JSON object whose keys are each given once (a repeated key would silently keep the last)."""
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError(f"duplicate key {key!r}")
        document[key] = value
    return document


def read_set(text: str) -> SetPlan:
    """A set from JSON (starts with ``{``) or a set score."""
    if text.lstrip().startswith("{"):
        try:
            document = json.loads(text, object_pairs_hook=_unique_keys)
        except ValueError as error:
            raise PlanError(f"the set is not valid JSON: {error}") from error
        except RecursionError as error:  # valid JSON nested past the decoder's stack
            raise PlanError("the set's JSON is nested too deeply") from error
        return parse_set(document)
    return parse_set_score(text)


def print_set_score(plan: SetPlan) -> str:
    """The canonical set score for ``plan``."""
    head: list[str] = []
    if plan.name is not None:
        head.append(f"set {score_quoted(plan.name)}")
    if plan.intent is not None:
        head.append(f"intent {json.dumps(plan.intent)}")
    blocks = [print_score(transition) for transition in plan.transitions]
    return ("\n".join(head) + "\n\n" if head else "") + "\n".join(blocks)


def plan_set(
    profiles: Sequence[TrackProfile],
    *,
    first_deck: int = 1,
    intent: str | None = None,
    name: str | None = None,
    after_bar: float | None = None,
) -> SetPlan:
    """The best validated transition for each neighbor pair, chained into one set.

    Decks alternate between ``first_deck`` and its partner. Each transition
    leaves the outgoing track after the bar where the previous one finished
    with it (``after_bar`` for the first, typically where the playing deck is
    now). A running order that repeats a track is refused, as is any result
    that does not chain (:class:`SetPlan` checks it).
    """
    if len(profiles) < 2:
        raise PlanError("a set needs at least two tracks")
    if first_deck not in PARTNER:
        raise PlanError(f"first_deck must be a deck 1..4, got {first_deck}")
    transitions: list[TransitionPlan] = []
    out_deck, floor = first_deck, after_bar
    for index, (out_profile, in_profile) in enumerate(pairwise(profiles), start=1):
        in_deck = PARTNER[out_deck]
        result = propose_transition(
            out_profile, in_profile, out_deck=out_deck, in_deck=in_deck, intent=intent, min_out_bar=floor, top=10
        )
        chosen = next((row for row in result["proposals"] if row["validation"]["ok"]), None)
        if chosen is None:
            raise PlanError(
                f"no valid transition from {out_profile.stable_id} to {in_profile.stable_id} "
                f"(transition {index}); try another order or intent"
            )
        plan = parse_plan(chosen["plan"])
        transitions.append(plan)
        # The next exit starts half a bar after the bar this blend ends: the live validator refuses any start
        # within half a bar of the deck's bar (plan.py _check_out_position), and chain_errors would accept an
        # exit on that very bar, which no schedule can start.
        out_deck, floor = in_deck, in_end_bar(plan) + SCHEDULE_LEAD_BARS
    return SetPlan(transitions=tuple(transitions), name=name, intent=intent)


@dataclass(frozen=True)
class NextStep:
    """What :func:`next_transition` decided: a transition index, or why none."""

    index: int | None
    reason: str

    def to_json(self) -> dict[str, Any]:
        return {"index": self.index, "reason": self.reason}


class MirrorError(ValueError):
    """The live mirror does not have the deck records a set needs."""


def _mirror_decks(mirror: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """Every deck record in the mirror, checked: decks 1..4, each with playing and stable_id."""
    decks = mirror.get("decks")
    if not isinstance(decks, Mapping):
        raise MirrorError("the live mirror has no decks object")
    unknown = sorted(str(deck) for deck in decks if deck not in MIRROR_DECKS)
    if unknown:
        raise MirrorError(f"the live mirror has an unknown deck {unknown[0]!r}; its decks are 1..4")
    missing = sorted(MIRROR_DECKS - set(decks))
    if missing:
        raise MirrorError(f"the live mirror has no deck {missing[0]}; the page publishes all four")
    for deck, state in decks.items():
        _check_deck(deck, state)
    _check_agent_plan(mirror)
    return dict(decks)


def _check_deck(deck: str, state: Any) -> None:
    if not isinstance(state, Mapping):
        raise MirrorError(f"mirror deck {deck} is not an object")
    if not isinstance(state.get("playing"), bool):
        raise MirrorError(f"mirror deck {deck} has no boolean playing")
    if "stable_id" not in state:
        raise MirrorError(f"mirror deck {deck} has no stable_id (a string, or null when empty)")
    stable_id = state["stable_id"]
    if not (stable_id is None or (isinstance(stable_id, str) and stable_id)):
        raise MirrorError(f"mirror deck {deck} stable_id must be a non-empty string or null")
    _check_position(deck, state.get("position"))


def _check_position(deck: str, position: Any) -> None:
    if position is not None and not isinstance(position, Mapping):
        raise MirrorError(f"mirror deck {deck} position must be an object or null")
    ms = (position or {}).get("ms")
    if ms is not None and (type(ms) not in (int, float) or not math.isfinite(ms)):
        raise MirrorError(f"mirror deck {deck} position.ms must be a finite number or null, got {ms!r}")


def _check_agent_plan(mirror: Mapping[str, Any]) -> None:
    running = mirror.get("agent_plan")
    if running is None:
        return
    status = running.get("status") if isinstance(running, Mapping) else None
    if not isinstance(status, str) or status not in PLAN_STATUSES:
        raise MirrorError(
            f"the live mirror's agent_plan must be null or an object whose status is one of "
            f"{', '.join(sorted(PLAN_STATUSES))}, got {running!r}"
        )


def check_mirror(mirror: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """The mirror's four deck records, checked, or :class:`MirrorError` naming what is wrong."""
    return _mirror_decks(mirror)


def _playing(decks: Mapping[str, Mapping[str, Any]], deck: int, track: str) -> bool:
    state = decks[str(deck)]
    return state["stable_id"] == track and state["playing"]


def _playing_anywhere(decks: Mapping[str, Mapping[str, Any]], track: str) -> bool:
    return any(state["stable_id"] == track and state["playing"] for state in decks.values())


def next_transition(plan: SetPlan, mirror: Mapping[str, Any]) -> NextStep:
    """The transition due now, read from the live mix and nothing else.

    It is the first one whose outgoing track is playing on its deck while its
    incoming track is not yet playing on any deck. A plan already running on the page
    holds the decks, so nothing is due until it ends. A mirror without a checked
    record for each of the four decks, or with an ``agent_plan`` that is not
    null or a known status, raises :class:`MirrorError`.
    """
    decks = _mirror_decks(mirror)
    status = (mirror.get("agent_plan") or {}).get("status")
    if status in BUSY_STATUSES:
        return NextStep(None, f"a plan is {status} on the page; schedule the next one after it ends")
    # Checked before any earlier handover: a finished set whose last track moved to another deck must not
    # replay an old handover when an earlier outgoing track is restarted.
    last = plan.transitions[-1]
    if _playing_anywhere(decks, last.in_.track):
        return NextStep(None, "the set is finished: its last track is playing")
    for index, transition in enumerate(plan.transitions):
        if _playing(decks, transition.out.deck, transition.out.track) and not _playing_anywhere(
            decks, transition.in_.track
        ):
            return NextStep(index, f"transition {index + 1}: D{transition.out.deck} into D{transition.in_.deck}")
    first = plan.transitions[0].out
    return NextStep(
        None,
        f"no deck plays an outgoing track of the set; start D{first.deck} {first.track} first with the command tool",
    )
