"""DIALCLOCK-8341 and COLORFX-8341 on the CLI / MCP verb path.

- [if] an agent writes ``filter 1 3pm`` [then] the filter lands at 0.8333, the 3 o'clock UI position, [else stop].
- [if] the token is ``1`` [then] it stays the dial value 1.0 (full right), so no script changes meaning, [else stop].
- [if] a clock time sits in the dead gap at the bottom of the dial [then] it is refused by name, never clamped, [else stop].
- [if] ``color_fx 2 next`` is sent [then] the command carries ``effect: next``, [else stop].
"""
from __future__ import annotations

import pytest

from apps.opendj_cli.kinds import dial_clock_value, dial_value
from apps.opendj_cli.verbs import InvocationError, parse_invocation

pytestmark = [pytest.mark.requirement("DIALCLOCK-8341"), pytest.mark.requirement("COLORFX-8341")]

THREE = 5 / 6
NINE = 1 / 6


@pytest.mark.parametrize("raw", ["3", "3pm", "3 pm", "3am", "3PM", "3 o'clock", "3oclock", "3:00", "15:00", "15"])
def test_every_spelling_of_three_is_the_same_position(raw: str) -> None:
    assert dial_value("value", raw) == pytest.approx(THREE)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("12", 0.5), ("12 o'clock", 0.5), ("9pm", NINE), ("21:00", NINE), ("7:30", 0.0), ("4:30", 1.0)],
)
def test_landmarks_match_the_drawn_dial(raw: str, expected: float) -> None:
    assert dial_value("value", raw) == pytest.approx(expected)


@pytest.mark.parametrize(("raw", "expected"), [("0", 0.0), ("1", 1.0), ("0.5", 0.5), (".25", 0.25)])
def test_plain_numbers_in_0_to_1_stay_dial_values(raw: str, expected: float) -> None:
    assert dial_value("value", raw) == expected


def test_one_oclock_needs_a_marker() -> None:
    assert dial_value("value", "1pm") == pytest.approx(0.5 + 1 / 9)


@pytest.mark.parametrize("raw", ["5", "6", "7 o'clock", "4:45", "19:00"])
def test_the_dead_gap_is_refused_by_name(raw: str) -> None:
    with pytest.raises(ValueError, match="past the dial's stops"):
        dial_value("value", raw)


@pytest.mark.parametrize("raw", ["1.5", "-0.1", "2.5", "noon", "3:5", "25:00", "25"])
def test_non_dial_tokens_are_refused(raw: str) -> None:
    with pytest.raises(ValueError):
        dial_value("value", raw)


def test_the_clock_parser_alone_reads_bare_one_as_one_oclock() -> None:
    assert dial_clock_value("value", "1") == pytest.approx(0.5 + 1 / 9)


def test_filter_eq_trim_and_color_fx_take_clock_times() -> None:
    assert parse_invocation(["filter", "1", "3pm"]).command["value"] == pytest.approx(THREE)
    assert parse_invocation(["eq", "2", "low", "9"]).command["value"] == pytest.approx(NINE)
    assert parse_invocation(["trim", "3", "12"]).command["value"] == pytest.approx(0.5)
    command = parse_invocation(["color_fx", "4", "effect=echo", "value=15:00"]).command
    assert command == {"type": "color_fx", "deck": 4, "effect": "echo", "value": pytest.approx(THREE)}


def test_faders_stay_plain_units() -> None:
    with pytest.raises(InvocationError):
        parse_invocation(["fader", "1", "3pm"])


def test_color_fx_next_cycles() -> None:
    assert parse_invocation(["color_fx", "2", "next"]).command == {"type": "color_fx", "deck": 2, "effect": "next"}
    with pytest.raises(InvocationError):
        parse_invocation(["color_fx", "2", "prev"])
