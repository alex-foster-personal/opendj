"""AGENT-27: a whole set as one document, the track list plus chained transitions.

Every track here is invented (trk-a .. trk-d fixtures), never a library track.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

import pytest

from apps.autodj import plan as plan_module
from apps.autodj.notation import ScoreError
from apps.autodj.plan import PlanError
from apps.autodj.setscore import (
    MirrorError,
    SetPlan,
    chain_errors,
    check_mirror,
    in_end_bar,
    next_transition,
    parse_set,
    parse_set_score,
    plan_set,
    print_set_score,
    read_set,
)
from tests.autodj.fixtures import CHORUS, DOWN, INTRO, OUTRO, UP, profile

PROFILES = [
    profile("trk-a"),
    profile("trk-b", key="9A"),
    profile("trk-c", key="8B", bpm=126.0),
    profile("trk-d", key="9A"),
]


def _mirror(
    playing: dict[int, str], loaded: dict[int, str] | None = None, plan_status: str | None = None
) -> dict[str, Any]:
    decks: dict[str, Any] = {}
    for deck in (1, 2, 3, 4):
        track = playing.get(deck) or (loaded or {}).get(deck)
        decks[str(deck)] = {"stable_id": track, "playing": deck in playing}
    return {"decks": decks, "agent_plan": None if plan_status is None else {"status": plan_status}}


@pytest.mark.requirement("AGENT-27")
@pytest.mark.parametrize("intent", [None, "keep the energy up", "wind down", "showy", "keep vocals clean"])
def test_a_planned_set_chains_and_round_trips(intent: str | None) -> None:
    """[if] a set spans four tracks [then] each transition hands over and reads back, [else stop]."""
    planned = plan_set(PROFILES, intent=intent, name="warmup")
    assert [row["track"] for row in planned.tracks] == ["trk-a", "trk-b", "trk-c", "trk-d"]
    assert [row["deck"] for row in planned.tracks] == [1, 2, 1, 2]  # decks alternate
    assert chain_errors(planned.transitions) == []
    for before, after in zip(planned.transitions, planned.transitions[1:], strict=False):
        assert after.out.start_bar >= in_end_bar(before)  # leave only after the last handover ends
    text = print_set_score(planned)
    assert text.startswith("set warmup\n")
    assert text.count("\ntransition ") + text.startswith("transition ") == 3
    assert parse_set_score(text).to_json() == planned.to_json()
    assert print_set_score(parse_set_score(text)) == text  # fixed point
    assert parse_set(planned.to_json()).to_json() == planned.to_json()


@pytest.mark.requirement("AGENT-27")
def test_set_json_keeps_each_transitions_extension_keys() -> None:
    """[if] a transition has an extension key [then] the set's JSON keeps it, [else stop]."""
    document = plan_set(PROFILES).to_json()
    document["transitions"][0]["note_x"] = "keep"
    kept = parse_set(document).to_json()
    assert kept["transitions"][0]["note_x"] == "keep"
    assert "note_x" not in kept["transitions"][1]  # control: only the transition that had it
    assert parse_set_score(print_set_score(parse_set(document))).to_json() == kept


#: A track whose favorite exit (a breakdown at bar 17) comes before a long
#: blend into it has finished: the planner must skip that window.
EARLY_EXIT = profile(
    "trk-early",
    arrangement=(
        (INTRO, 16, 0.4, 0.2, False),
        (DOWN, 32, 0.3, 0.1, False),
        (CHORUS, 16, 1.0, 1.0, True),
        (UP, 16, 0.6, 0.5, False),
        (CHORUS, 16, 1.0, 1.0, True),
        (OUTRO, 16, 0.4, 0.8, False),
    ),
)


@pytest.mark.requirement("AGENT-27")
@pytest.mark.parametrize("intent", [None, "wind down"])
def test_the_next_exit_waits_for_the_last_entry_to_finish(intent: str | None) -> None:
    """[if] an exit comes before the blend into it ends [then] the planner leaves later, [else stop]."""
    planned = plan_set([PROFILES[0], EARLY_EXIT, PROFILES[2]], intent=intent)
    first, second = planned.transitions
    assert in_end_bar(first) > 17  # the blend into trk-early is still running at its bar 17
    assert second.out.start_bar >= in_end_bar(first)
    assert chain_errors(planned.transitions) == []
    # The same floor applies to the first transition when the set starts mid-track.
    assert plan_set(PROFILES[:2], after_bar=100.0).transitions[0].out.start_bar > 100


@pytest.mark.requirement("AGENT-27")
def test_a_set_that_does_not_hand_over_is_refused() -> None:
    """[if] a transition leaves a track not yet in, or too early [then] the set is refused, [else stop]."""
    good = plan_set(PROFILES[:3]).to_json()
    assert parse_set(good).transitions  # control: the planned chain parses
    wrong_track = plan_set(PROFILES[:3]).to_json()
    wrong_track["tracks"] = None
    wrong_track["transitions"][1]["out"]["track"] = "trk-d"
    with pytest.raises(PlanError, match="transition 2 mixes out of D2 trk-d, but transition 1 brought in D2 trk-b"):
        parse_set(wrong_track)
    too_early = plan_set(PROFILES[:3]).to_json()
    del too_early["tracks"]
    too_early["transitions"][1]["out"]["window"]["start_bar"] = 2
    with pytest.raises(PlanError, match="transition 2 leaves trk-b at bar 2, but transition 1 only finishes"):
        parse_set(too_early)
    mismatched = plan_set(PROFILES[:3]).to_json()
    mismatched["tracks"][0]["deck"] = 3
    with pytest.raises(PlanError, match="tracks does not match"):
        parse_set(mismatched)
    with pytest.raises(PlanError, match=r"transitions\[0\]: events must be a non-empty list"):
        parse_set({"transitions": [{**good["transitions"][0], "events": []}]})


@pytest.mark.requirement("AGENT-27")
def test_a_bad_line_in_a_set_names_its_transition_and_file_line() -> None:
    """[if] block 2 has a bad move [then] the error names the block and its file line, [else stop]."""
    text = print_set_score(plan_set(PROFILES[:3], name="warmup"))
    lines = text.splitlines()
    second = [i for i, line in enumerate(lines) if line.startswith("transition ")][1]
    bad = next(i for i in range(second, len(lines)) if lines[i].startswith("@"))  # a move in block 2
    lines[bad] = "@0 D9 play"
    with pytest.raises(PlanError, match=rf"transition 2 \(line {second + 1}\): line {bad + 1}: expected D1..D4"):
        parse_set_score("\n".join(lines))
    with pytest.raises(PlanError, match="only 'set <name>'"):
        parse_set_score("set warmup\nstyle loud\ntransition cut\n")
    with pytest.raises(PlanError, match="at least one 'transition"):
        parse_set_score("set warmup\n")


def test_read_set_takes_a_single_block_and_refuses_bad_json() -> None:
    single = print_set_score(SetPlan(transitions=plan_set(PROFILES[:2]).transitions))
    assert read_set(single).tracks == [{"deck": 1, "track": "trk-a"}, {"deck": 2, "track": "trk-b"}]
    with pytest.raises(PlanError, match="not valid JSON"):
        read_set("{nope")
    # Valid JSON nested past the decoder's stack is a PlanError too, not a RecursionError.
    with pytest.raises(PlanError, match="nested too deeply"):
        read_set('{"transitions": ' + "[" * 100_000 + "]" * 100_000 + "}")


@pytest.mark.requirement("AGENT-27")
def test_the_due_transition_follows_the_live_mix() -> None:
    """[if] the mirror shows the set's place [then] next_transition picks the due transition, [else stop]."""
    planned = plan_set(PROFILES)
    # Only the first track plays: transition 1 is due.
    assert next_transition(planned, _mirror({1: "trk-a"})).index == 0
    # The incoming deck is already loaded but idle: still transition 1.
    assert next_transition(planned, _mirror({1: "trk-a"}, loaded={2: "trk-b"})).index == 0
    # Mid-handover the page's plan holds the decks, so nothing else is due.
    assert next_transition(planned, _mirror({1: "trk-a", 2: "trk-b"}, plan_status="running")).index is None
    # Once trk-b plays (with or without trk-a's tail), transition 2 is due.
    assert next_transition(planned, _mirror({1: "trk-a", 2: "trk-b"})).index == 1
    assert next_transition(planned, _mirror({2: "trk-b"})).index == 1
    assert next_transition(planned, _mirror({1: "trk-c"})).index == 2
    finished = next_transition(planned, _mirror({2: "trk-d"}))
    assert finished.index is None and "finished" in finished.reason
    silent = next_transition(planned, _mirror({}))
    assert silent.index is None and "start D1 trk-a first" in silent.reason
    busy = next_transition(planned, _mirror({1: "trk-a"}, plan_status="running"))
    assert busy.index is None and "running" in busy.reason
    # Control: a finished page plan does not hold the decks.
    assert next_transition(planned, _mirror({1: "trk-a"}, plan_status="done")).index == 0


def test_a_set_needs_two_tracks_and_a_real_deck() -> None:
    with pytest.raises(PlanError, match="at least two tracks"):
        plan_set(PROFILES[:1])
    with pytest.raises(PlanError, match=r"deck 1\.\.4"):
        plan_set(PROFILES[:2], first_deck=5)
    assert plan_set(PROFILES[:2], first_deck=3).tracks == [{"deck": 3, "track": "trk-a"}, {"deck": 4, "track": "trk-b"}]


@pytest.mark.requirement("AGENT-27")
def test_an_incoming_track_playing_on_another_deck_is_not_due() -> None:
    """[if] a transition's incoming track already plays [then] it is not due, [else stop]."""
    planned = plan_set(PROFILES)
    # trk-b plays on deck 3, not its planned deck 2: transition 1 must not fire again.
    assert next_transition(planned, _mirror({1: "trk-a", 3: "trk-b"})).index != 0
    # Control: trk-b only loaded (not playing) on deck 3 leaves transition 1 due.
    assert next_transition(planned, _mirror({1: "trk-a"}, loaded={3: "trk-b"})).index == 0


@pytest.mark.requirement("AGENT-27")
@pytest.mark.parametrize(("key", "value"), [("set", 7), ("intent", ["up"]), ("set", {"name": "x"})])
def test_set_metadata_must_be_text_or_null(key: str, value: Any) -> None:
    """[if] a set JSON's set or intent is not a string or null [then] PlanError names it, [else stop]."""
    good = plan_set(PROFILES[:2], name="warmup").to_json()
    with pytest.raises(PlanError, match=rf"{key} must be a string or null"):
        parse_set({**good, key: value})
    # Control: null and absence are both accepted.
    assert parse_set({**good, key: None}).to_json()[key] is None


@pytest.mark.requirement("AGENT-27")
def test_a_semantically_bad_block_names_its_transition_and_file_line() -> None:
    """[if] a block parses but is not a valid plan [then] the error names it and its line, [else stop]."""
    text = print_set_score(plan_set(PROFILES[:3], name="warmup"))
    lines = text.splitlines()
    second = [i for i, line in enumerate(lines) if line.startswith("transition ")][1]
    fader = next(i for i in range(second, len(lines)) if " fader " in lines[i] and "->" not in lines[i])
    lines[fader] = lines[fader].rsplit(" ", 1)[0] + " 2"  # fader 2 is outside 0..1
    with pytest.raises(PlanError, match=rf"transition 2 \(line {second + 1}\): line {fader + 1}: events\[\d+\]: .*within 0\.\.1"):
        parse_set_score("\n".join(lines))
    # The out and in lines are named for their own fields, not the block's first line.
    for offset, side in ((1, "out"), (2, "in")):
        header = list(text.splitlines())
        header[second + offset] = re.sub(r" from \d+ ", " from 0 ", header[second + offset], count=1)
        with pytest.raises(PlanError, match=rf"transition 2 \(line {second + 1}\): line {second + offset + 1}: {side}\."):
            parse_set_score("\n".join(header))
    # Control: the untouched text parses.
    assert len(parse_set_score(text).transitions) == 2


@pytest.mark.requirement("AGENT-27")
def test_the_offending_line_comes_from_the_error_path_not_its_wording(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] a plan error's wording changes [then] the set score still names the line, [else stop]."""
    text = print_set_score(plan_set(PROFILES[:3], name="warmup"))
    lines = text.splitlines()
    second = [i for i, line in enumerate(lines) if line.startswith("transition ")][1]
    moves = [i for i in range(second, len(lines)) if lines[i].startswith("@")]
    real_event = plan_module._event
    third_moves_seen = []

    def reworded(document: Any, index: int) -> Any:
        if index == 2:
            third_moves_seen.append(document)
            if len(third_moves_seen) == 2:  # block 1 parses first, so this is block 2's third move
                raise PlanError("this move is wrong, said in words no pattern expects")
        return real_event(document, index)

    monkeypatch.setattr(plan_module, "_event", reworded)
    with pytest.raises(PlanError, match=rf"transition 2 \(line {second + 1}\): line {moves[2] + 1}: this move"):
        parse_set_score(text)
    monkeypatch.setattr(plan_module, "_event", real_event)
    # A header field the score names (anchor) points at its own line too.
    lines.insert(second + 3, "anchor nowhere")
    with pytest.raises(PlanError, match=rf"transition 2 \(line {second + 1}\): line {second + 4}: anchor must be"):
        parse_set_score("\n".join(lines))
    # Control: the untouched text parses.
    assert len(parse_set_score(text).transitions) == 2


@pytest.mark.requirement("AGENT-27")
def test_a_line_that_cannot_be_tokenized_names_its_transition() -> None:
    """[if] a block 2 line has an open quote [then] the error names block 2 and the line, [else stop]."""
    text = print_set_score(plan_set(PROFILES[:3], name="warmup"))
    lines = text.splitlines()
    second = [i for i, line in enumerate(lines) if line.startswith("transition ")][1]
    intent = next(i for i in range(second, len(lines)) if lines[i].startswith("intent "))
    lines[intent] = 'intent "never closed'
    with pytest.raises(PlanError, match=rf"^transition 2 \(line {second + 1}\): line {intent + 1}: "):
        parse_set_score("\n".join(lines))
    # Control: a quoted intent with a hash and spaces still parses.
    lines[intent] = 'intent "keep # it"'
    assert parse_set_score("\n".join(lines)).transitions[1].intent == "keep # it"



@pytest.mark.requirement("AGENT-27")
@pytest.mark.parametrize("header", ["set again", 'intent "again"'])
def test_a_repeated_set_header_is_refused(header: str) -> None:
    """[if] a set score repeats its set or intent line [then] ScoreError names the line, [else stop]."""
    text = print_set_score(plan_set(PROFILES[:2], name="warmup", intent="wind down"))
    head, rest = text.split("\ntransition ", 1)
    with pytest.raises(PlanError, match=rf"line 4: a second '{header.split(maxsplit=1)[0]}' line"):
        parse_set_score(f"{head}\n{header}\ntransition {rest}")
    # Control: the score with each header once still parses.
    assert parse_set_score(text).name == "warmup"


@pytest.mark.requirement("AGENT-27")
def test_a_transition_from_a_track_into_itself_is_refused() -> None:
    """[if] a transition's two tracks are the same [then] the set is refused, [else stop]."""
    good = plan_set(PROFILES[:2]).to_json()
    same = {"transitions": [dict(good["transitions"][0])]}
    same["transitions"][0]["in"] = {**same["transitions"][0]["in"], "track": "trk-a"}
    with pytest.raises(PlanError, match="transition 1: mixes trk-a into itself"):
        parse_set(same)
    # Control: the planned set parses.
    assert len(parse_set(good).transitions) == 1


@pytest.mark.requirement("AGENT-27")
@pytest.mark.parametrize(
    ("mirror", "message"),
    [
        ({"agent_plan": None}, "no decks object"),
        ({"decks": {"1": {"stable_id": "trk-a", "playing": True}}, "agent_plan": None}, "no deck 2"),
        ({"decks": {**_mirror({1: "trk-a"})["decks"], "2": None}}, "mirror deck 2 is not an object"),
        ({"decks": {**_mirror({1: "trk-a"})["decks"], "2": {"stable_id": None}}}, "mirror deck 2 has no boolean playing"),
        ({"decks": {**_mirror({1: "trk-a"})["decks"], "3": {"stable_id": 7, "playing": False}}}, "deck 3 stable_id"),
        ({"decks": {**_mirror({1: "trk-a"})["decks"], "3": {"playing": False}}}, "deck 3 has no stable_id"),
        ({"decks": {**_mirror({1: "trk-a"})["decks"], "3": {"stable_id": "", "playing": False}}}, "deck 3 stable_id"),
        ({"decks": {k: v for k, v in _mirror({1: "trk-a"})["decks"].items() if k != "3"}}, "no deck 3"),
        ({"decks": {**_mirror({1: "trk-a"})["decks"], "x": {"stable_id": None, "playing": False}}}, "unknown deck 'x'"),
        ({"decks": {**_mirror({1: "trk-a"})["decks"], "5": {"stable_id": None, "playing": False}}}, "unknown deck '5'"),
    ],
)
def test_a_malformed_mirror_is_refused_not_read_as_idle(mirror: dict[str, Any], message: str) -> None:
    """[if] the mirror lacks a deck the set uses [then] MirrorError, not "nothing due", [else stop]."""
    planned = plan_set(PROFILES[:3])
    with pytest.raises(MirrorError, match=message):
        next_transition(planned, mirror)
    # Controls: a complete mirror with only empty, stopped decks still reads as not started,
    # and the page's own shape (all four decks, agent_plan absent) is complete.
    assert next_transition(planned, _mirror({})).index is None
    assert next_transition(planned, {"decks": _mirror({1: "trk-a"})["decks"]}).index == 0


@pytest.mark.requirement("AGENT-27")
def test_a_set_that_repeats_a_track_is_refused() -> None:
    """[if] a running order or set brings a track in twice [then] it is refused, [else stop]."""
    with pytest.raises(PlanError, match="transition 2: brings in trk-a again"):
        plan_set([PROFILES[0], PROFILES[1], PROFILES[0]])
    repeated = plan_set(PROFILES[:3]).to_json()
    del repeated["tracks"]
    repeated["transitions"][1]["in"]["track"] = "trk-a"
    with pytest.raises(PlanError, match=r"transition 2: brings in trk-a again \(the first track already played it\)"):
        parse_set(repeated)
    # Controls: a set of distinct tracks chains, and a self-handover is reported once, not twice.
    assert chain_errors(plan_set(PROFILES).transitions) == []
    good = plan_set(PROFILES[:2]).to_json()
    good["transitions"][0]["in"]["track"] = "trk-a"
    self_mix = _chain_errors_of(good)
    assert len(self_mix) == 1 and "into itself" in self_mix[0]


def _chain_errors_of(document: dict[str, Any]) -> list[str]:
    from apps.autodj.plan import parse_plan

    return chain_errors([parse_plan(plan) for plan in document["transitions"]])


@pytest.mark.requirement("AGENT-27")
def test_plan_set_never_builds_a_set_its_parser_refuses() -> None:
    """[if] a SetPlan is empty or does not chain [then] PlanError, [else stop]."""
    with pytest.raises(PlanError, match="mixes trk-a into itself"):
        plan_set([PROFILES[0], PROFILES[0]])
    with pytest.raises(PlanError, match="at least one transition"):
        SetPlan(transitions=())
    first, second = plan_set(PROFILES[:3]).transitions
    with pytest.raises(PlanError, match="transition 2 mixes out of"):
        SetPlan(transitions=(second, first))
    # Control: the planned transitions, in order, still make a set.
    assert SetPlan(transitions=(first, second)).tracks[-1] == {"deck": 1, "track": "trk-c"}


@pytest.mark.requirement("AGENT-27")
def test_a_transition_on_one_deck_is_refused() -> None:
    """[if] a transition mixes out of and into the same deck [then] the set is refused, [else stop]."""
    good = plan_set(PROFILES[:2]).to_json()
    same = {"transitions": [{**good["transitions"][0], "in": {**good["transitions"][0]["in"], "deck": 1}}]}
    with pytest.raises(PlanError, match="transition 1: mixes out of and into D1"):
        parse_set(same)
    # Control: the same transition across two decks parses.
    assert parse_set({"transitions": good["transitions"]}).tracks[1]["deck"] == 2


@pytest.mark.requirement("AGENT-27")
@pytest.mark.parametrize("key", ["name", "intnet", "transition"])
def test_an_unknown_set_key_is_refused(key: str) -> None:
    """[if] a set JSON holds an unknown top-level key [then] PlanError names it, [else stop]."""
    good = plan_set(PROFILES[:2], name="warmup", intent="wind down").to_json()
    with pytest.raises(PlanError, match=rf"unknown key '{key}'"):
        parse_set({**good, key: "x"})
    # Control: every known key together is accepted.
    assert set(good) == {"set", "intent", "tracks", "transitions"}
    assert parse_set(good).name == "warmup"


@pytest.mark.requirement("AGENT-27")
@pytest.mark.parametrize("where", ["top", "nested"])
def test_a_repeated_json_key_is_refused(where: str) -> None:
    """[if] a set's JSON repeats a key at any depth [then] read_set refuses it, [else stop]."""
    text = json.dumps(plan_set(PROFILES[:2], name="warmup").to_json())
    top = '{"set": "other", ' + text[1:]
    bad = top if where == "top" else text.replace('"style": ', '"style": "cut", "style": ', 1)
    assert bad != text
    with pytest.raises(PlanError, match="duplicate key 'set'" if where == "top" else "duplicate key 'style'"):
        read_set(bad)
    # Control: the same text without the repeat reads.
    assert read_set(text).name == "warmup"


@pytest.mark.requirement("AGENT-27")
@pytest.mark.parametrize("deck", [True, 1.0])
def test_a_tracks_row_deck_must_be_an_integer(deck: Any) -> None:
    """[if] a tracks row gives its deck as true or 1.0 [then] the set is refused, [else stop]."""
    good = plan_set(PROFILES[:2]).to_json()
    bad = json.loads(json.dumps(good))
    bad["tracks"][0]["deck"] = deck
    with pytest.raises(PlanError, match="tracks does not match"):
        parse_set(bad)
    # Control: the integer deck matches.
    assert parse_set(good).tracks[0]["deck"] == 1


@pytest.mark.requirement("AGENT-27")
@pytest.mark.parametrize(
    "agent_plan",
    [{"status": "Scheduled"}, {"status": "armed"}, {}, {"status": None}, {"status": []}, {"status": {"a": 1}}, ["running"], "running"],
)
def test_a_malformed_agent_plan_is_refused_not_read_as_idle(agent_plan: Any) -> None:
    """[if] the mirror's agent_plan is malformed [then] MirrorError, not a due transition, [else stop]."""
    planned = plan_set(PROFILES[:3])
    with pytest.raises(MirrorError, match="agent_plan"):
        next_transition(planned, {**_mirror({1: "trk-a"}), "agent_plan": agent_plan})
    # Controls: null and every status the page publishes are read; only the busy ones hold the decks.
    assert next_transition(planned, _mirror({1: "trk-a"})).index == 0
    for status in ("done", "aborted", "failed"):
        assert next_transition(planned, _mirror({1: "trk-a"}, plan_status=status)).index == 0
    for status in ("scheduled", "running"):
        assert next_transition(planned, _mirror({1: "trk-a"}, plan_status=status)).index is None


@pytest.mark.requirement("AGENT-27")
def test_a_busy_plan_does_not_hide_a_malformed_mirror() -> None:
    """[if] a plan runs but the mirror's decks are malformed [then] MirrorError, [else stop]."""
    planned = plan_set(PROFILES[:3])
    broken = _mirror({1: "trk-a"}, plan_status="running")
    del broken["decks"]["2"]["playing"]
    with pytest.raises(MirrorError):
        next_transition(planned, broken)
    with pytest.raises(MirrorError):
        next_transition(planned, {"agent_plan": {"status": "running"}})
    # Control: the same busy mirror with its decks whole reads as busy.
    assert "running" in next_transition(planned, _mirror({1: "trk-a"}, plan_status="running")).reason


@pytest.mark.requirement("AGENT-27")
@pytest.mark.parametrize(
    ("position", "message"),
    [
        ({"ms": True}, "position.ms must be a finite number"),
        ({"ms": math.nan}, "position.ms must be a finite number"),
        ({"ms": math.inf}, "position.ms must be a finite number"),
        ({"ms": "1000"}, "position.ms must be a finite number"),
        ([1000], "position must be an object or null"),
    ],
)
def test_a_deck_position_must_be_a_finite_number(position: Any, message: str) -> None:
    """[if] a deck's position lacks a finite ms [then] check_mirror refuses it, [else stop]."""
    mirror = _mirror({1: "trk-a"})
    mirror["decks"]["1"]["position"] = position
    with pytest.raises(MirrorError, match=rf"deck 1 {message}"):
        check_mirror(mirror)
    # Controls: an integer, a float, a null ms and a null position are all fine.
    for good in ({"ms": 1000}, {"ms": 1000.5}, {"ms": None}, None):
        mirror["decks"]["1"]["position"] = good
        assert set(check_mirror(mirror)) == {"1", "2", "3", "4"}


@pytest.mark.requirement("AGENT-27")
@pytest.mark.parametrize(("offset", "planned"), [(1, True), (0, False)])
def test_the_next_exit_starts_half_a_bar_after_the_last_blend_ends(
    monkeypatch: pytest.MonkeyPatch, offset: int, planned: bool
) -> None:
    """[if] the only exit starts a bar after the blend into it ends [then] it is used, [else stop].

    An exit on the very bar the blend ends is refused: the live validator needs half a bar of lead, so no schedule
    could start it. Control: the same exit one bar later is planned."""
    from apps.autodj import propose as propose_module
    from apps.autodj.windows import window_at

    ends = int(in_end_bar(plan_set(PROFILES[:2]).transitions[0]))
    real = propose_module.mix_windows

    def only_exit(
        profile: Any, role: Any, *, limit: int = 5, min_start_bar: float | None = None
    ) -> list[Any]:
        if profile.stable_id == "trk-b" and role == "out":
            windows = [window_at(profile, "out", ends + offset)]
            if min_start_bar is not None:
                windows = [w for w in windows if w.start_bar > min_start_bar]
            return windows[: max(1, limit)]
        return real(profile, role, limit=limit, min_start_bar=min_start_bar)

    monkeypatch.setattr(propose_module, "mix_windows", only_exit)
    if planned:
        second = plan_set(PROFILES[:3]).transitions[1]
        assert second.out.start_bar == ends + offset
    else:
        # The exit on the blend's own bar is refused, as the live validator would refuse it.
        with pytest.raises(PlanError, match="no valid transition from trk-b to trk-c"):
            plan_set(PROFILES[:3])


@pytest.mark.requirement("AGENT-27")
@pytest.mark.parametrize(
    ("field", "value"),
    [("anchor", ["window"]), ("tempo", {"mode": ["none"]}), ("events", [{"at_bar": 1, "deck": 2, "do": ["play"]}])],
)
def test_a_malformed_plan_field_is_a_plan_error_not_a_type_error(field: str, value: object) -> None:
    """[if] a plan's anchor, tempo mode or event action is an array [then] PlanError naming it, [else stop].

    Control: the same plan with string values parses, so the refusal is the field's type and nothing else."""
    document = plan_set(PROFILES[:2]).to_json()
    document["transitions"][0][field] = value
    with pytest.raises(PlanError):
        parse_set(document)
    assert parse_set(plan_set(PROFILES[:2]).to_json()).transitions


@pytest.mark.requirement("AGENT-27")
def test_a_chain_error_in_a_set_score_names_the_block_file_line() -> None:
    """[if] a score's third block brings in a track already played [then] the error names its file line, [else stop].

    Control: the printed score of a distinct-track set parses."""
    text = print_set_score(plan_set(PROFILES, name="warmup"))
    lines = text.splitlines()
    third = [i for i, line in enumerate(lines) if line.startswith("transition ")][2]
    lines = [line.replace("trk-d", "trk-a") if i >= third else line for i, line in enumerate(lines)]
    with pytest.raises(ScoreError, match=rf"transition 3 \(line {third + 1}\): brings in trk-a again"):
        parse_set_score("\n".join(lines))
    assert parse_set_score(text).transitions


@pytest.mark.requirement("AGENT-27")
def test_a_finished_set_whose_last_track_moved_does_not_replay_an_earlier_handover() -> None:
    """[if] the set's last track plays on another deck while an earlier outgoing track restarts [then] it is finished, [else stop].

    Control: with the last track not playing, the same earlier handover is due."""
    planned = plan_set(PROFILES[:3])
    assert next_transition(planned, _mirror({1: "trk-a", 3: "trk-c"})).index is None
    assert "finished" in next_transition(planned, _mirror({1: "trk-a", 3: "trk-c"})).reason
    assert next_transition(planned, _mirror({1: "trk-a"})).index == 0
