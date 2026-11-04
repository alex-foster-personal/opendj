"""AGENT-5858: the mix score, a text form of a TransitionPlan that round-trips with the JSON.

Every plan here is invented (trk-out / trk-in fixtures), never a library track.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from apps.autodj.notation import (
    MAX_GRID_BARS,
    MAX_NUMBER_CHARS,
    MAX_WINDOW_DIGITS,
    ScoreError,
    looks_like_score,
    parse_score,
    print_score,
    render_grid,
    score_document,
)
from apps.autodj.plan import EXTRA_MAX_DEPTH, SHAPES, PlanError, TransitionPlan, parse_plan
from apps.autodj.propose import propose_transition
from apps.autodj.styles import STYLES, style
from tests.autodj.fixtures import profile

OUT, IN = profile("trk-out"), profile("trk-in", key="9A")

SCORE = """# a 16-bar bass swap
transition bass_swap_blend
out D1 trk-out from 113 for 16
in  D2 trk-in  from 17 for 16
tempo sync
intent "keep the energy up"   # a comment after a value

@0       D2 play
@0       D2 eq_low kill
@0..8    D2 fader -> full
@8       D1 eq_low kill
@8       D2 eq_low flat
@8..16   mix -> full
@12..16  D1 fader -> 0
@16      D1 stop
"""


def _plans() -> list[TransitionPlan]:
    """Every available style at several lengths, plus every proposal for every intent."""
    plans: list[TransitionPlan] = []
    for name, entry in STYLES.items():
        if entry.builder is None or not entry.available:  # main: unavailable styles (stems) refuse to build
            continue
        for bars in sorted({entry.clamp_bars(b) for b in (3, 8, 15, 32)}):  # main bounds each style's length
            events = style(name).builder(bars, 1, 2)  # type: ignore[misc]
            plans.append(
                parse_plan(
                    {
                        "out": {"deck": 1, "track": "trk-out", "window": {"start_bar": 113, "bars": bars}},
                        "in": {"deck": 2, "track": "trk-in", "window": {"start_bar": 1, "bars": bars}},
                        "style": name,
                        "events": events,
                    }
                )
            )
    for intent in ("smooth", "keep the energy up", "wind down", "showy", "keep vocals clean"):
        result = propose_transition(OUT, IN, out_deck=1, in_deck=2, intent=intent, min_out_bar=60)
        plans.extend(parse_plan(row["plan"]) for row in result["proposals"])
    return plans


@pytest.mark.requirement("AGENT-26")
def test_every_style_and_proposal_round_trips_through_the_score() -> None:
    """[if] any plan is printed as a score and parsed back [then] its JSON is identical, [else stop]."""
    plans = _plans()
    assert len(plans) > 20  # control: the property ran over real plans, not an empty list
    for plan in plans:
        text = print_score(plan)
        assert parse_score(text).to_json() == plan.to_json(), text
        assert print_score(parse_score(text)) == text  # the printed form is a fixed point


@pytest.mark.requirement("AGENT-26")
def test_a_hand_written_score_compiles_to_the_plan_it_describes() -> None:
    """[if] a score has words, comments and spacing [then] it parses to the matching events, [else stop]."""
    plan = parse_score(SCORE)
    assert plan.style == "bass_swap_blend" and plan.intent == "keep the energy up"
    assert (plan.out.deck, plan.out.track, plan.out.start_bar, plan.out.bars) == (1, "trk-out", 113, 16)
    assert (plan.in_.deck, plan.in_.start_bar) == (2, 17)
    events = [event.to_json() for event in plan.events]
    assert {"at_bar": 0.0, "deck": 2, "ramp": {"fader": 1.0}, "over_bars": 8.0} in events
    assert {"at_bar": 8.0, "deck": None, "ramp": {"crossfader": 1.0}, "over_bars": 8.0} in events
    assert {"at_bar": 0.0, "deck": 2, "set": {"eq_low": 0.0}} in events
    assert {"at_bar": 16.0, "deck": 1, "do": "stop"} in events
    assert len(events) == 8
    # Formatting is normalized: words for EQ, numbers elsewhere, no comments.
    printed = print_score(plan)
    assert "@0..8    D2 fader -> 1\n" in printed and "@8       D2 eq_low flat\n" in printed
    assert "#" not in printed


@pytest.mark.requirement("AGENT-26")
def test_awkward_values_still_round_trip_exactly() -> None:
    """[if] a value needs an exact or quoted form [then] the printed form reads back, [else stop]."""
    plan = parse_plan(
        {
            "out": {"deck": 3, "track": "trk out #1", "window": {"start_bar": 9, "bars": 8}},
            "in": {"deck": 4, "track": "trk-in", "window": {"start_bar": 1, "bars": 8}},
            "style": "long_blend",
            "anchor": "next_phrase",
            "tempo": {"mode": "none"},
            "intent": 'say "hi" # not a comment',
            "rationale": "café",
            "events": [
                {"at_bar": 0.1, "deck": 4, "do": "play"},
                {"at_bar": 0.1, "deck": 4, "ramp": {"fader": 0.3}, "over_bars": 0.2},
                {"at_bar": 1, "deck": 4, "set": {"tempo": 1.0125}},
                {"at_bar": 4.125, "deck": 3, "ramp": {"fader": 0.0}, "over_bars": 3.875},
            ],
        }
    )
    text = print_score(plan)
    assert "@0.1+0.2 " in text  # 0.1 + 0.2 is not 0.3 in floating point
    assert "@4.125..8 D3 fader -> 0" in text  # a time wider than its column still gets a space
    assert '"trk out #1"' in text and "anchor next_phrase" in text and "tempo none" in text
    assert parse_score(text).to_json() == plan.to_json()


@pytest.mark.requirement("AGENT-26")
@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("@0 D5 play", "line 5: expected D1..D4 or mix"),
        ("@0 D2 fader -> 1", "line 5: a ramp needs a range"),
        ("@0..4 D2 fader 1", "line 5: use '-> <value>'"),
        ("@0..4 D2 play", "line 5: play happens on one bar"),
        ("@0 D2 volume 1", "line 5: expected play, stop, master or a deck control"),
        ("@4..2 D2 fader -> 1", "line 5: a range must end after it starts"),
        ("@0 D2 fader loud", "line 5: 'loud' is not a number"),
        ("D2 play", "line 5: cannot read"),
    ],
)
def test_a_bad_line_is_refused_by_number(line: str, message: str) -> None:
    """[if] a score line is malformed [then] ScoreError names the line and the problem, [else stop]."""
    head = "transition cut\nout D1 a from 1 for 4\nin D2 b from 1 for 4\n\n"
    with pytest.raises(ScoreError) as refused:
        parse_score(head + line + "\n")
    assert message in str(refused.value)


@pytest.mark.requirement("AGENT-26")
def test_trim_bass_entry_and_shape_survive_the_score() -> None:
    """[if] trim, bass_entry or shape leave a score [then] gain and shape checks fail, [else stop].

    Dropping in.trim_db or in.trim_reason loses the incoming gain at compile, and dropping
    bass_entry or shape makes validation refuse the shaped plan.
    """
    base = next(p for p in _plans() if p.bass_entry is not None)
    for trim_db, reason in ((-2.5, "louder master"), (None, "measured, no change needed"), (1.25, None)):
        for shape in (None, *sorted(SHAPES)):
            plan = replace(base, in_=replace(base.in_, trim_db=trim_db, trim_reason=reason), shape=shape)
            again = parse_score(print_score(plan))
            assert again.to_json() == plan.to_json()
            assert (again.in_.trim_db, again.in_.trim_reason, again.shape) == (trim_db, reason, shape)
            assert again.bass_entry == plan.bass_entry


@pytest.mark.requirement("AGENT-26")
def test_extension_keys_survive_json_to_score_to_json() -> None:
    """[if] a plan has keys beyond the score's lines [then] they round-trip as extra lines, [else stop]."""
    document = {
        "out": {"deck": 1, "track": "trk-a", "window": {"start_bar": 1, "bars": 4}},
        "in": {"deck": 2, "track": "trk-b", "window": {"start_bar": 1, "bars": 4}},
        "style": "cut",
        "events": [{"at_bar": 0, "deck": 2, "do": "play"}],
        "planner": {"name": "test", "notes": "a # b", "weights": [0.5, 1]},
        "seed": 7,
    }
    plan = parse_plan(document)
    text = print_score(plan)
    assert 'extra {"planner": {"name": "test", "notes": "a # b", "weights": [0.5, 1]}}' in text
    again = parse_score(text)
    assert again.to_json() == plan.to_json()
    assert dict(again.extra) == {"planner": document["planner"], "seed": 7}
    # Control: a plan with no extension keys prints no extra line.
    assert "extra" not in print_score(parse_plan({k: v for k, v in document.items() if k not in ("planner", "seed")}))
    # The full JSON view keeps the extension keys too.
    assert again.to_document() == {**plan.to_json(), "planner": document["planner"], "seed": 7}


@pytest.mark.requirement("AGENT-26")
@pytest.mark.parametrize("name", ["my blend", "drop#2", 'say "hi"', "esc\x1b[2Jstyle", "safe\u202eevil", "plain_style"])
def test_a_custom_style_name_round_trips_through_the_score(name: str) -> None:
    """[if] a style needs quoting [then] the score quotes it and reads it back, [else stop]."""
    plan = parse_plan(
        {
            "out": {"deck": 1, "track": "trk-a", "window": {"start_bar": 1, "bars": 4}},
            "in": {"deck": 2, "track": "trk-b", "window": {"start_bar": 1, "bars": 4}},
            "style": name,
            "events": [{"at_bar": 0, "deck": 2, "do": "play"}],
        }
    )
    text = print_score(plan)
    assert parse_score(text).style == name
    # A control character never reaches the terminal raw.
    assert all(char.isprintable() or char == "\n" for char in text)
    # Control: a bare style still prints bare.
    if name == "plain_style":
        assert text.startswith("transition plain_style\n")


@pytest.mark.requirement("AGENT-26")
@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("transition long_blend", "line 5: a second 'transition' line"),
        ("out D3 c from 1 for 4", "line 5: a second 'out' line"),
        ('intent "again"', "line 5: a second 'intent' line"),
        ('extra {"style": "cut"}', "line 5: 'style' has its own line"),
        ('extra {"seed": 1, "more": 2}', "line 5: extra needs a one-key JSON object"),
        ("extra not json", "line 5: extra needs a one-key JSON object"),
        ('extra {"seed": 1, "seed": 2}', "line 5: extra names 'seed' twice"),
        ('extra {"seed": {"a": 1, "a": 2}}', "line 5: extra names 'a' twice"),
    ],
)
def test_a_repeated_or_bad_header_is_refused(line: str, message: str) -> None:
    """[if] a header repeats or an extra line is malformed [then] ScoreError names the line, [else stop]."""
    head = 'transition cut\nout D1 a from 1 for 4\nin D2 b from 1 for 4\nintent "once"\n'
    with pytest.raises(ScoreError) as refused:
        parse_score(head + line + "\n@0 D2 play\n")
    assert message in str(refused.value)
    # Control: the same score without the repeated line parses.
    assert parse_score(head + "@0 D2 play\n").intent == "once"


@pytest.mark.requirement("AGENT-26")
def test_a_hash_starts_a_comment_anywhere_outside_quotes() -> None:
    """[if] a bare # follows a value [then] it starts a comment, but a quoted # stays text, [else stop]."""
    head = 'transition cut\nout D1 "trk #1" from 1 for 4\nin D2 b from 1 for 4\n'
    plan = parse_score(head + "@0 D2 play# go\n@0 D2 fader 1# fade\n")
    assert [(event.name, event.value) for event in plan.events] == [("play", None), ("fader", 1.0)]
    assert plan.out.track == "trk #1"
    with pytest.raises(ScoreError, match="not a valid quoted string"):
        parse_score(head.replace('"trk #1"', '"trk') + "@0 D2 play\n")


@pytest.mark.requirement("AGENT-26")
def test_an_extra_line_takes_a_trailing_comment_but_keeps_hashes_in_json() -> None:
    """[if] an extra line ends in a comment [then] it is dropped and a # in the JSON stays, [else stop]."""
    head = "transition cut\nout D1 a from 1 for 4\nin D2 b from 1 for 4\n"
    plan = parse_score(head + 'extra {"seed": 1} # note\nextra {"tag": "a # b"}#x\n@0 D2 play\n')
    assert dict(plan.extra) == {"seed": 1, "tag": "a # b"}
    # Control: text after the JSON that is not a comment is still refused.
    with pytest.raises(ScoreError, match="line 4: extra needs a one-key JSON object"):
        parse_score(head + 'extra {"seed": 1} trailing\n@0 D2 play\n')


@pytest.mark.requirement("AGENT-26")
def test_a_bar_count_too_large_for_a_float_round_trips_exactly() -> None:
    """[if] a window bar needs more than float precision [then] the score keeps it exact, [else stop]."""
    huge = 2**53 + 1  # a float would read this back as 2**53
    document = {
        "out": {"deck": 1, "track": "trk-a", "window": {"start_bar": huge, "bars": huge}},
        "in": {"deck": 2, "track": "trk-b", "window": {"start_bar": 1, "bars": 4}},
        "style": "cut",
        "events": [{"at_bar": 0, "deck": 2, "do": "play"}],
    }
    plan = parse_plan(document)
    assert parse_score(print_score(plan)).to_json() == plan.to_json()
    # Control: a fractional bar count is still refused.
    head = "transition cut\nout D1 a from 1 for 4.5\nin D2 b from 1 for 4\n"
    with pytest.raises(ScoreError, match="must be a whole number of bars"):
        parse_score(head + "@0 D2 play\n")


@pytest.mark.requirement("AGENT-26")
def test_a_set_or_ramp_without_a_value_is_refused_not_printed_as_zero() -> None:
    """[if] a set or ramp event has no value [then] the score and the grid refuse it, [else stop]."""
    head = "transition cut\nout D1 a from 1 for 4\nin D2 b from 1 for 4\n"
    plan = parse_score(head + "@0 D2 play\n@0 D2 fader 0\n@0..2 mix -> 1\n")
    for index in (1, 2):  # the set, then the ramp
        events = list(plan.events)
        events[index] = replace(events[index], value=None)
        broken = replace(plan, events=tuple(events))
        with pytest.raises(PlanError, match="has no value"):
            print_score(broken)
        with pytest.raises(PlanError, match="has no value"):
            render_grid(broken)
    # Control: a real value of 0 still prints as 0, not as a missing value.
    assert "@0       D2 fader 0\n" in print_score(plan)
    assert render_grid(plan)


@pytest.mark.requirement("AGENT-26")
def test_a_score_is_refused_for_the_same_reasons_as_json() -> None:
    """[if] a score parses but the plan is wrong [then] the validator refuses it as for JSON, [else stop]."""
    with pytest.raises(ScoreError, match="missing the 'out D<n>"):
        parse_score("transition cut\nin D2 b from 1 for 4\n@0 D2 play\n")
    head = "transition cut\nout D1 a from 1 for 4\nin D2 b from 1 for 4\n"
    with pytest.raises(PlanError, match=r"within 0\.\.1"):
        parse_score(head + "@0 D2 fader 2\n")
    with pytest.raises(PlanError, match="events must be a non-empty list"):
        parse_score(head)
    # Control: the same score with a legal value parses.
    assert parse_score(head + "@0 D2 fader 1\n").events[0].value == 1.0


def test_json_and_score_are_told_apart() -> None:
    assert looks_like_score(SCORE)
    assert not looks_like_score('  {"style": "cut"}')
    assert score_document(SCORE)["style"] == "bass_swap_blend"
    json.dumps(score_document(SCORE))  # the document is plain JSON


@pytest.mark.requirement("AGENT-26")
def test_the_grid_draws_each_lane_at_its_bars() -> None:
    """[if] a plan renders as a grid [then] each deck and control gets a lane of beat marks, [else stop]."""
    grid = render_grid(parse_score(SCORE))
    rows: dict[str, str] = {}
    for row in grid.splitlines():
        if row[:12].strip() and not row.startswith("one column"):
            rows[row[:12].strip()] = row[12:]
    assert rows["bar"].startswith("0") and rows["bar"].rstrip().endswith("16")
    assert rows["D2 deck"].startswith("play")
    fader = rows["D2 fader"]
    assert fader[0] == "[" and fader[8 * 4] == "]" and "-> 1" in fader  # bars 0..8, 4 columns a bar
    assert rows["mix xfader"].index("[") == 8 * 4
    assert rows["D1 deck"].index("stop") == 16 * 4
    assert rows["D1 eq_low"].index("|kill") == 8 * 4
    names = [name for name in rows if name != "bar"]
    assert names == sorted(names, key=lambda n: ("~" if n.startswith("mix") else n[:2], "deck" not in n, n))


@pytest.mark.requirement("AGENT-26")
def test_the_grid_widens_for_an_event_past_the_window_instead_of_clamping() -> None:
    """[if] an event sits past the window [then] the grid widens to draw it at its bar, [else stop]."""
    head = "transition cut\nout D1 a from 1 for 4\nin D2 b from 1 for 4\n"
    grid = render_grid(parse_score(head + "@0 D2 play\n@20 D2 stop\n"))
    rows = {row[:12].strip(): row[12:] for row in grid.splitlines()}
    assert rows["D2 deck"].index("stop") == 20 * 4
    assert rows["bar"].rstrip().endswith("20")
    # Control: an in-window plan keeps the window's width.
    grid = render_grid(parse_score(head + "@0 D2 play\n@4 D2 stop\n"))
    rows = {row[:12].strip(): row[12:] for row in grid.splitlines()}
    assert rows["bar"].rstrip().endswith("4") and rows["D2 deck"].index("stop") == 4 * 4


@pytest.mark.requirement("AGENT-26")
def test_the_grid_refuses_a_span_too_wide_to_draw_before_allocating_it() -> None:
    """[if] a plan spans over MAX_GRID_BARS bars [then] render_grid raises ScoreError, [else stop]."""
    def _plan(bars: int) -> TransitionPlan:
        head = f"transition cut\nout D1 a from 1 for {bars}\nin D2 b from 1 for {bars}\n"
        return parse_score(head + "@0 D2 play\n")

    with pytest.raises(ScoreError, match=f"more than {MAX_GRID_BARS} bars"):
        render_grid(_plan(10**9))
    with pytest.raises(ScoreError, match=r"0\.\.513"):
        render_grid(_plan(MAX_GRID_BARS + 1))
    # Control: exactly the cap still draws, one column per beat.
    rows = {row[:12].strip(): row[12:] for row in render_grid(_plan(MAX_GRID_BARS)).splitlines()}
    assert rows["bar"].rstrip().endswith(str(MAX_GRID_BARS))


def test_a_window_bar_count_is_bounded_before_it_becomes_an_int() -> None:
    """[if] a window bar has over MAX_WINDOW_DIGITS digits [then] the score is refused, [else stop]."""
    head = "transition cut\nout D1 a from 1 for {bars}\nin D2 b from 1 for 8\n@0 D2 play\n"
    for huge in ("1e1000000", "-1e1000000", "1" + "0" * MAX_WINDOW_DIGITS):
        with pytest.raises(ScoreError, match=f"more than {MAX_WINDOW_DIGITS} digits"):
            parse_score(head.format(bars=huge))
    # Control: the widest bar allowed is a window, written either way.
    widest = 10**MAX_WINDOW_DIGITS - 1
    assert parse_score(head.format(bars=widest)).out.bars == widest
    assert parse_score(head.format(bars="1e29")).out.bars == 10**29


def test_a_window_bar_token_longer_than_the_cap_is_refused_before_it_is_parsed() -> None:
    """[if] a window bar token is over MAX_NUMBER_CHARS [then] it is refused before parsing, [else stop].

    Zeros make a short value cost a long parse, and the digit bound cannot see them. Control: a normal bar parses."""
    head = "transition cut\nout D1 a from 1 for {bars}\nin D2 b from 1 for 8\n@0 D2 play\n"
    with pytest.raises(ScoreError, match=f"longer than {MAX_NUMBER_CHARS} characters"):
        parse_score(head.format(bars="0." + "0" * MAX_NUMBER_CHARS + "1"))
    assert parse_score(head.format(bars="32.0")).out.bars == 32


def test_a_json_window_the_score_cannot_read_back_is_refused_by_the_printer() -> None:
    """[if] a JSON window has over MAX_WINDOW_DIGITS digits [then] no score prints, [else stop]."""
    head = "transition cut\nout D1 a from 1 for 8\nin D2 b from 1 for 8\n@0 D2 play\n"
    document = parse_score(head).to_document()
    for side, key in (("out", "bars"), ("in", "start_bar")):
        wide = json.loads(json.dumps(document))
        wide[side]["window"][key] = 10**MAX_WINDOW_DIGITS
        with pytest.raises(ScoreError, match=rf"{side} window .* more than {MAX_WINDOW_DIGITS} digits"):
            print_score(parse_plan(wide))
        # Control: one digit fewer prints, and reads back to the same plan.
        wide[side]["window"][key] = 10**MAX_WINDOW_DIGITS - 1
        plan = parse_plan(wide)
        assert parse_score(print_score(plan)) == plan


def test_a_window_takes_bar_numbers_not_control_words() -> None:
    head = "transition cut\nout D1 a from {start} for {bars}\nin D2 b from 1 for 8\n@0 D2 play\n"
    for start, bars in (("full", "8"), ("1", "full"), ("1", "kill")):
        with pytest.raises(ScoreError, match="whole number of bars"):
            parse_score(head.format(start=start, bars=bars))
    assert parse_score(head.format(start="1", bars="8")).out.bars == 8  # control


def test_the_grid_refuses_an_event_at_an_infinite_bar_as_a_score_error() -> None:
    head = "transition cut\nout D1 a from 1 for 8\nin D2 b from 1 for 8\n@0 D2 play\n"
    for move, refusal in (
        ("@1e308+1e308 D1 fader -> 0", "infinite bar"),  # each finite, the end overflows
        ("@0+1e308 D1 fader -> 0", "more than 512 bars"),  # finite: the span cap refuses it
    ):
        with pytest.raises(ScoreError, match=refusal):
            render_grid(parse_score(head + move + "\n"))
    with pytest.raises(PlanError, match="must be finite"):  # an infinite start never parses
        parse_score(head + "@1e400 D1 fader 0\n")
    # Control: a finite move past the window still widens and draws.
    assert "D1 fader" in render_grid(parse_score(head + "@0+16 D1 fader -> 0\n"))


def test_proposals_carry_their_score() -> None:
    result = propose_transition(OUT, IN, out_deck=1, in_deck=2, intent="smooth", min_out_bar=60)
    row: dict[str, Any] = result["proposals"][0]
    assert parse_score(row["notation"]).to_json() == parse_plan(row["plan"]).to_json()


@pytest.mark.requirement("AGENT-26")
def test_an_extra_object_with_distinct_keys_still_parses() -> None:
    """[if] an extra value is an object with distinct keys [then] it parses whole, [else stop]."""
    head = 'transition cut\nout D1 a from 1 for 4\nin D2 b from 1 for 4\n'
    document = score_document(head + 'extra {"seed": {"a": 1, "b": 2}}\n@0 D2 play\n')
    assert document["seed"] == {"a": 1, "b": 2}


@pytest.mark.requirement("AGENT-26")
def test_numbers_and_json_past_the_decoders_limits_are_score_errors() -> None:
    """[if] a number or extra outruns its decoder [then] ScoreError names it, [else stop]."""
    head = "transition cut\nout D1 a from 1 for {bars}\nin D2 b from 1 for 8\n{extra}@0 D2 play\n"
    with pytest.raises(ScoreError, match=r"line 2: .* is not a whole number of bars"):
        parse_score(head.format(bars="1e" + "9" * 40, extra=""))
    deep = '{"seed": ' + "[" * 100_000 + "]" * 100_000 + "}"
    with pytest.raises(ScoreError, match="line 4: extra's JSON is nested too deeply"):
        parse_score(head.format(bars="8", extra=f"extra {deep}\n"))
    # Control: a nested value within the decoder's reach still parses.
    shallow = '{"seed": ' + "[" * 10 + "]" * 10 + "}"
    assert score_document(head.format(bars="8", extra=f"extra {shallow}\n"))["seed"] == [[[[[[[[[[]]]]]]]]]]


@pytest.mark.requirement("AGENT-26")
def test_an_extra_nested_too_deep_to_print_is_refused_at_parse() -> None:
    """[if] an extra nests past EXTRA_MAX_DEPTH [then] parse_plan raises PlanError, [else stop].

    So print_score never meets a plan nested too deep to print.
    """

    def _plan(depth: int) -> dict[str, Any]:
        nested: Any = []
        for _ in range(depth - 1):
            nested = [nested]
        return {
            "style": "cut",
            "out": {"deck": 1, "track": "trk-out", "window": {"start_bar": 1, "bars": 4}},
            "in": {"deck": 2, "track": "trk-in", "window": {"start_bar": 1, "bars": 4}},
            "events": [{"at_bar": 0, "deck": 2, "do": "play"}],
            "seed": nested,
        }

    with pytest.raises(PlanError, match="seed is nested too deeply"):
        parse_plan(_plan(1500))  # far past the encoder's limit, the case this guard exists for
    with pytest.raises(PlanError, match=f"at most {EXTRA_MAX_DEPTH} levels"):
        parse_plan(_plan(EXTRA_MAX_DEPTH + 1))
    # Control: an extension at the bound parses and prints.
    assert "extra {\"seed\": " in print_score(parse_plan(_plan(EXTRA_MAX_DEPTH)))
