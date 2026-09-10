"""AGENT-05 end to end: the CLI against a real engine and a real page.

Every test here runs ``main`` against the production app served on a loopback
socket, with the origin read out of a real lock file. The page is a real client
of the same routes the browser polls.

[if] the engine is not running [then ⛔] the CLI exits 2 and names the lock file
     it checked (REQUIREMENTS.md:4196).
[if] the engine is up with no performance page open [then ⛔] the CLI exits 3
     and says so, rather than hanging on an order nobody can claim.
[if] a page claims an order and reports succeeded without the state moving
     [then ⛔] the CLI exits 4 rather than printing done.
[if] a command exists on the bus [then ⛔] ``--list-verbs`` names it.
[if] an unload clears the deck title to null [then ⛔] the CLI confirms it,
     rather than reading null as "the control never moved".
[if] a script writes one mirrored control twice [then ⛔] only the last write is
     required of the final mirror, so the run confirms.
[if] the mirror names no control this command moves [then ⛔] the verdict is
     accepted, never confirmed - a settled clock rules a fault out, it does not
     rule the effect in.
[if] a load reports succeeded without the requested track landing [then ⛔] the
     CLI exits 4, even when the deck already held some other track.
[if] a ramp lasts longer than the request deadline [then ⛔] the deadline grows
     to outlast it, so a working ramp is never reported as a timeout.
[if] argparse itself refuses the invocation under --json [then ⛔] the refusal
     is a JSON document, not plain usage text.
"""
from __future__ import annotations

import importlib
import json
import socket
import time
import tomllib
from pathlib import Path

import pytest

from apps.opendj_cli import (
    EXIT_CONFIRMED,
    EXIT_FAILED,
    EXIT_NO_ENGINE,
    EXIT_NO_PAGE,
    EXIT_TIMEOUT,
    EXIT_UNCONFIRMED,
)
from apps.opendj_cli.__main__ import main
from apps.opendj_cli.orders import ramp_deadline_s
from apps.opendj_cli.verbs import InvocationError
from tests.opendj_cli.conftest import Engine, PerformancePage

REPO_ROOT = Path(__file__).resolve().parents[2]


def _argv(engine: Engine, *tokens: str) -> list[str]:
    return ["--lock", str(engine.lock_path), *tokens]


# ----- the engine is not there ---------------------------------------------

def test_exits_2_and_names_the_lock_file_it_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "absent" / ".engine.lock"
    monkeypatch.setenv("OPENDJ_LIVE_LOCK_PATH", str(missing))

    assert main(["state"]) == EXIT_NO_ENGINE

    captured = capsys.readouterr()
    assert str(missing) in captured.err
    assert "not running" in captured.err


def test_exits_2_with_the_lock_path_in_the_json_body(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / ".engine.lock"

    assert main(["--json", "--lock", str(missing), "state"]) == EXIT_NO_ENGINE

    body = json.loads(capsys.readouterr().out)
    assert body["error"]["code"] == "engine_not_running"
    assert str(missing) in body["error"]["message"]


def test_exits_2_when_the_lock_names_a_port_nothing_answers_on(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        dead_port = int(probe.getsockname()[1])
    lock = tmp_path / ".engine.lock"
    lock.write_text(
        json.dumps({"pid": 1, "host": "127.0.0.1", "port": dead_port}), encoding="utf-8"
    )

    dead = Engine(base_url="", port=dead_port, lock_path=lock)
    assert main(_argv(dead, "state")) == EXIT_NO_ENGINE

    captured = capsys.readouterr()
    assert str(lock) in captured.err
    assert str(dead_port) in captured.err


def test_a_lock_file_without_a_port_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lock = tmp_path / ".engine.lock"
    lock.write_text(json.dumps({"pid": 7, "role": "opendj-engine"}), encoding="utf-8")

    assert main(["--lock", str(lock), "state"]) == EXIT_NO_ENGINE
    assert "no usable port" in capsys.readouterr().err


# ----- the engine is up ----------------------------------------------------

def test_state_prints_the_mirror_as_json(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    page = engine.page()
    page.mirror["decks"]["1"]["title"] = "Warehouse Loop"
    page.start()
    try:
        assert main(_argv(engine, "--json", "state")) == EXIT_CONFIRMED
    finally:
        page.stop()

    body = json.loads(capsys.readouterr().out)
    assert body["decks"]["1"]["title"] == "Warehouse Loop"
    assert body["context_state"] == "running"


def test_state_exits_3_when_no_performance_page_is_open(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    started = time.monotonic()
    assert main(_argv(engine, "state")) == EXIT_NO_PAGE
    assert time.monotonic() - started < 5.0

    captured = capsys.readouterr()
    assert "no performance page" in captured.err
    assert "409" in captured.err


def test_an_order_with_no_page_open_refuses_rather_than_hangs(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """There is no headless audio path, so the CLI must say so and stop."""

    started = time.monotonic()
    assert main(_argv(engine, "deck", "1", "play")) == EXIT_NO_PAGE
    elapsed = time.monotonic() - started

    assert elapsed < 5.0, f"the refusal took {elapsed:.1f}s, which is a hang"
    assert "no performance page" in capsys.readouterr().err


def test_deck_play_dispatches_and_is_confirmed_against_the_mirror(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    page = engine.page()
    page.start()
    try:
        assert main(_argv(engine, "deck", "1", "play")) == EXIT_CONFIRMED
        assert page.mirror["decks"]["1"]["playing"] is True
    finally:
        page.stop()

    captured = capsys.readouterr()
    assert page.orders == [
        {"kind": "single", "payload": {"type": "play", "deck": 1, "playing": True}}
    ]
    assert "confirmed" in captured.out


def test_a_succeeded_order_that_moved_nothing_is_not_reported_as_done(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """The live fault of Thu 10 Sep 2026: 200 succeeded, deck still stopped."""

    page = engine.page()
    page.apply_commands = False
    page.start()
    try:
        assert main(_argv(engine, "--settle", "0.3", "deck", "1", "play")) == EXIT_UNCONFIRMED
        assert page.mirror["decks"]["1"]["playing"] is False
    finally:
        page.stop()

    captured = capsys.readouterr()
    assert "unconfirmed" in captured.out
    assert "decks.1.playing" in captured.out


def test_a_presentation_clock_that_never_catches_up_is_not_confirmed(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """The deck went to playing, but the page never presented the change."""

    page = engine.page()
    page.settle = False
    page.start()
    try:
        assert main(_argv(engine, "--settle", "0.3", "deck", "1", "play")) == EXIT_UNCONFIRMED
        assert page.mirror["decks"]["1"]["playing"] is True
    finally:
        page.stop()

    assert "has not been presented" in capsys.readouterr().out


def test_a_wedged_page_is_a_timeout_not_a_hang(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """A page on record that never polls cannot claim the order it is offered."""

    page = engine.page()
    page.start()
    page._running = False  # the mirror stays published; the poll stops
    page._thread.join(timeout=5) if page._thread else None
    try:
        started = time.monotonic()
        assert main(_argv(engine, "--timeout", "0.5", "deck", "1", "cue")) == EXIT_TIMEOUT
        assert time.monotonic() - started < 10.0
    finally:
        page.stop()

    assert "did not answer within" in capsys.readouterr().err


def test_a_failed_step_exits_1_and_stops_the_script(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    page = engine.page()
    page.fail_every_order = True
    page.start()
    try:
        assert main(_argv(engine, "deck", "1", "cue")) == EXIT_FAILED
    finally:
        page.stop()

    assert "the page refused this order" in capsys.readouterr().out


# ----- the orders the CLI sends --------------------------------------------

def test_eq_over_a_duration_sends_the_agent_04_ramp(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    page = engine.page()
    page.start()
    try:
        code = main(_argv(engine, "eq", "2", "low", "0.2", "--over", "4beats"))
        assert code == EXIT_CONFIRMED
        assert page.mirror["mixer"]["channels"]["2"]["eq_low"] == pytest.approx(0.2)
    finally:
        page.stop()

    assert page.orders == [
        {
            "kind": "ramp",
            "payload": {
                "command": {"type": "eq", "deck": 2, "band": "low", "value": 0.2},
                "to": 0.2,
                "over": {"unit": "beats", "n": 4},
            },
        }
    ]
    assert "confirmed" in capsys.readouterr().out


def test_a_ramp_can_name_its_anchor_and_clock(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    page = engine.page()
    page.start()
    try:
        assert main(
            _argv(
                engine, "fader", "1", "0.4", "--over", "2bars",
                "--anchor", "next_downbeat", "--clock", "2",
            )
        ) == EXIT_CONFIRMED
    finally:
        page.stop()

    assert page.orders[0]["payload"]["over"] == {
        "unit": "bars",
        "n": 2,
        "anchor": "next_downbeat",
        "clock": 2,
    }


def test_over_is_refused_for_a_command_the_page_cannot_ramp(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    page = engine.page()
    page.start()
    try:
        assert main(_argv(engine, "deck", "1", "cue", "--over", "4beats")) == EXIT_FAILED
        assert page.orders == []
    finally:
        page.stop()

    assert "ramps eq, fader, trim or filter" in capsys.readouterr().err


def test_do_then_and_runs_a_sequence_of_groups(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    page = engine.page()
    page.start()
    try:
        code = main(_argv(engine, "do", "load 1 warehouse", "then", "play 1", "and", "play 2"))
        assert code == EXIT_CONFIRMED
        assert page.mirror["decks"]["1"]["stable_id"] == "warehouse"
        assert page.mirror["decks"]["1"]["playing"] is True
        assert page.mirror["decks"]["2"]["playing"] is True
    finally:
        page.stop()

    assert page.orders == [
        {"kind": "single", "payload": {"type": "load", "deck": 1, "stable_id": "warehouse"}},
        {
            "kind": "parallel",
            "payload": [
                {"type": "play", "deck": 1, "playing": True},
                {"type": "play", "deck": 2, "playing": True},
            ],
        },
    ]
    assert "confirmed" in capsys.readouterr().out


def test_a_script_that_stops_on_an_error_skips_the_rest(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    page = engine.page()
    page.fail_every_order = True
    page.start()
    try:
        code = main(_argv(engine, "do", "play 1", "then", "play 2"))
        assert code == EXIT_FAILED
    finally:
        page.stop()

    assert len(page.orders) == 1, "the second group must not be dispatched"
    assert "skipped" in capsys.readouterr().out


def test_a_separator_with_nothing_between_it_is_refused(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(_argv(engine, "do", "play 1", "then")) == EXIT_FAILED
    assert "must sit between two quoted commands" in capsys.readouterr().err


# ----- the console script itself -------------------------------------------

def test_the_console_script_is_declared_and_resolves_to_main() -> None:
    """The issue's premise: pyproject.toml had no [project.scripts] at all."""

    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    target = pyproject["project"]["scripts"]["opendj"]
    module_name, attribute = target.split(":")
    assert callable(getattr(importlib.import_module(module_name), attribute))


def test_list_verbs_names_every_command_on_the_bus(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--json", "--list-verbs"]) == EXIT_CONFIRMED
    rows = json.loads(capsys.readouterr().out)
    assert {row["command"] for row in rows} >= {"play", "eq", "crossfader", "hot_cue_save"}
    assert all("verb" in row and "usage" in row for row in rows)


def test_list_verbs_needs_no_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OPENDJ_LIVE_LOCK_PATH", str(tmp_path / "absent.lock"))
    assert main(["--list-verbs"]) == EXIT_CONFIRMED
    assert "play" in capsys.readouterr().out


def test_an_unknown_verb_is_a_usage_error_that_does_not_exit_2(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit 2 means one thing only: the engine is not running."""

    assert main(_argv(engine, "scratch", "1")) == EXIT_FAILED
    assert "unknown verb" in capsys.readouterr().err


def test_the_page_that_claims_nothing_never_sees_a_command(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    page: PerformancePage = engine.page()
    page.start()
    page.stop()
    try:
        assert main(_argv(engine, "deck", "1", "cue")) == EXIT_NO_PAGE
        assert page.orders == []
    finally:
        page.stop()
    assert "no performance page" in capsys.readouterr().err


# ----- what the mirror can and cannot answer for ---------------------------

def test_unload_is_confirmed_when_the_deck_title_goes_null(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """Production answers an unload with ``title: null`` (_emptyDeckState).

    Read as "the control never moved", that made every successful unload wait
    out the settle deadline and exit 4.
    """

    page = engine.page()
    page.start()
    try:
        assert main(_argv(engine, "load", "1", "track-a")) == EXIT_CONFIRMED
        assert page.mirror["decks"]["1"]["stable_id"] == "track-a"

        assert main(_argv(engine, "--settle", "0.3", "unload", "1")) == EXIT_CONFIRMED
        assert page.mirror["decks"]["1"]["title"] is None
        assert page.mirror["decks"]["1"]["stable_id"] is None
    finally:
        page.stop()

    captured = capsys.readouterr()
    assert "verdict: confirmed" in captured.out
    # The exit code alone does not pin this. A settle read can land in the
    # window between the result POST and the page's republish, where the
    # mirror still holds the PRE-unload title - and there the old, wrong
    # expectation passes too. Asserting what the CLI DEMANDED is what makes
    # this test discriminate; found by mutating the expectation back.
    assert "decks.1.title == null" in captured.out


def test_unload_is_unconfirmed_when_the_title_never_clears(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """The control on the fix above: expecting null must still be able to FAIL.

    Reading a null expectation as "anything goes" would satisfy this run just
    as happily as the correct one, which is the over-correction the fix has to
    survive.
    """

    page = engine.page()
    page.start()
    try:
        assert main(_argv(engine, "load", "1", "track-a")) == EXIT_CONFIRMED
        page.apply_commands = False
        assert main(_argv(engine, "--settle", "0.3", "unload", "1")) == EXIT_UNCONFIRMED
        assert page.mirror["decks"]["1"]["stable_id"] == "track-a"
    finally:
        page.stop()

    captured = capsys.readouterr()
    assert "unconfirmed" in captured.out
    assert "decks.1.title" in captured.out
    assert "expected null" in captured.out


def test_a_script_that_writes_one_control_twice_confirms_the_last_write(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """``play 1`` then ``pause 1`` asked one mirror for true AND false.

    No final mirror can satisfy both, so a sequence that ran perfectly waited
    out the settle deadline and exited 4.
    """

    page = engine.page()
    page.start()
    try:
        exit_code = main(_argv(engine, "--settle", "0.3", "do", "play 1", "then", "pause 1"))
        assert exit_code == EXIT_CONFIRMED
        assert page.mirror["decks"]["1"]["playing"] is False
    finally:
        page.stop()

    captured = capsys.readouterr()
    assert "confirmed" in captured.out
    assert "decks.1.playing == False" in captured.out
    assert "decks.1.playing == True" not in captured.out


def test_a_script_whose_last_write_never_lands_is_unconfirmed(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """The control: dropping superseded checks must not drop the final one.

    Ordered pause-then-play deliberately. A blank mirror already reads
    ``playing: false``, so keeping the SUPERSEDED check would pass this run
    just as happily as keeping none at all; only keeping the LAST one reports
    the truth, which is that nothing moved.
    """

    page = engine.page()
    page.apply_commands = False
    page.start()
    try:
        exit_code = main(_argv(engine, "--settle", "0.3", "do", "pause 1", "then", "play 1"))
        assert exit_code == EXIT_UNCONFIRMED
    finally:
        page.stop()

    captured = capsys.readouterr()
    assert "unconfirmed" in captured.out
    # The FINAL expectation is the one demanded of the mirror. Had the
    # superseded check survived instead, this run would have confirmed.
    assert "decks.1.playing is False, expected True" in captured.out


def test_a_command_the_mirror_cannot_observe_is_accepted_not_confirmed(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """``slip`` is real deck state that ``buildUiMirror`` does not publish.

    The deck's presentation clock reads 0 == 0 whether or not the slip landed,
    so calling this confirmed claimed an affirmation the mirror never made.
    """

    page = engine.page()
    page.start()
    try:
        assert main(_argv(engine, "slip", "1", "true")) == EXIT_CONFIRMED
        text = capsys.readouterr().out
        assert main(_argv(engine, "--json", "slip", "1", "true")) == EXIT_CONFIRMED
        document = json.loads(capsys.readouterr().out)
        assert main(_argv(engine, "--json", "play", "1")) == EXIT_CONFIRMED
        affirmed = json.loads(capsys.readouterr().out)
    finally:
        page.stop()

    assert "verdict: accepted" in text
    assert "verdict: confirmed" not in text
    # Exit 0 covers both verdicts, so the exit code alone cannot tell an agent
    # which one it got. `verdict` is where that distinction lives, which is why
    # the exit-code table in apps/opendj_cli/__init__.py points at it.
    assert document["verdict"] == "accepted"
    assert affirmed["verdict"] == "confirmed"


def test_an_unobservable_command_on_a_lagging_deck_is_still_unconfirmed(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """The control: accepted must not become a verdict nothing can fail.

    The clock stops being an affirmation, but it stays a precondition, so a
    deck the page never presented still refuses to pass.
    """

    page = engine.page()
    page.start()
    page.settle = False
    page.mirror["decks"]["1"]["presentation_clock"] |= {
        "desired_revision": 5,
        "presented_revision": 1,
    }
    try:
        assert main(_argv(engine, "--settle", "0.3", "slip", "1", "true")) == EXIT_UNCONFIRMED
    finally:
        page.stop()

    assert "has not been presented" in capsys.readouterr().out


def test_tempo_is_confirmed_against_the_pitch_the_mirror_publishes(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """``setTempoRatio`` lands as ``st.pitch``, which the mirror does publish."""

    page = engine.page()
    page.start()
    try:
        assert main(_argv(engine, "tempo", "1", "1.04")) == EXIT_CONFIRMED
        assert page.mirror["decks"]["1"]["pitch"] == pytest.approx(1.04)
    finally:
        page.stop()

    assert "decks.1.pitch" in capsys.readouterr().out


def test_a_knob_does_not_advance_the_decks_presentation_clock(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """The fixture models production: only a scheduled command moves the clock.

    ``acknowledgePresentedTransportSchedule`` is the one writer of
    ``desired_revision``, and an EQ turn never reaches it. A page that bumped
    the clock for every deck command made the clock check look like evidence
    it cannot be.
    """

    page = engine.page()
    page.start()
    try:
        assert main(_argv(engine, "eq", "1", "low", "0.2")) == EXIT_CONFIRMED
    finally:
        page.stop()

    clock = page.mirror["decks"]["1"]["presentation_clock"]
    assert clock == {"source": "audio_output", "desired_revision": 0, "presented_revision": 0}
    assert "confirmed" in capsys.readouterr().out


# ----- which track landed, not merely that one did -------------------------

def test_a_load_onto_a_loaded_deck_is_unconfirmed_when_the_track_never_lands(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """A deck that already holds a track has a title either way.

    Confirming a load off title presence therefore confirmed the PREVIOUS
    track: the check passed on state that predated the order, and an
    already-settled clock let it through as exit 0.
    """

    page = engine.page()
    page.start()
    try:
        assert main(_argv(engine, "load", "1", "first-track")) == EXIT_CONFIRMED
        capsys.readouterr()
        page.apply_commands = False
        exit_code = main(_argv(engine, "--settle", "0.3", "load", "1", "second-track"))
        assert exit_code == EXIT_UNCONFIRMED
        assert page.mirror["decks"]["1"]["stable_id"] == "first-track"
        assert page.mirror["decks"]["1"]["title"] is not None
    finally:
        page.stop()

    captured = capsys.readouterr()
    assert "unconfirmed" in captured.out
    assert "decks.1.stable_id" in captured.out
    assert "second-track" in captured.out


def test_a_load_onto_a_loaded_deck_confirms_when_the_track_does_land(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """The control: replacing a track must still be confirmable.

    An over-correction that refused any load onto a non-empty deck would pass
    the test above just as happily.
    """

    page = engine.page()
    page.start()
    try:
        assert main(_argv(engine, "load", "1", "first-track")) == EXIT_CONFIRMED
        capsys.readouterr()
        assert main(_argv(engine, "load", "1", "second-track")) == EXIT_CONFIRMED
        assert page.mirror["decks"]["1"]["stable_id"] == "second-track"
    finally:
        page.stop()

    assert "decks.1.stable_id == 'second-track'" in capsys.readouterr().out


# ----- a ramp outlives the deadline that guards a wedged page --------------

def test_a_ramp_longer_than_the_deadline_is_not_reported_as_a_timeout(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """The page holds a ramp order open for the ramp's whole duration.

    A fixed request deadline therefore fired during a perfectly healthy ramp
    and reported exit 5. The deadline now grows to outlast the ramp the CLI
    itself asked for; `--timeout` still sets the floor for a wedged page.
    """

    page = engine.page()
    page.start()
    try:
        exit_code = main(
            _argv(engine, "--timeout", "0.2", "eq", "1", "low", "0.2", "--over", "400ms")
        )
        assert exit_code == EXIT_CONFIRMED
        assert page.mirror["mixer"]["channels"]["1"]["eq_low"] == pytest.approx(0.2)
    finally:
        page.stop()

    assert "confirmed" in capsys.readouterr().out


def test_the_deadline_still_fires_when_no_ramp_justifies_waiting(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """The control: growing the deadline for ramps must not disarm it.

    Same wedged page and same short --timeout, with no --over to extend it.
    """

    page = engine.page()
    page.start()
    page._running = False
    page._thread.join(timeout=5) if page._thread else None
    try:
        assert main(_argv(engine, "--timeout", "0.5", "eq", "1", "low", "0.2")) == EXIT_TIMEOUT
    finally:
        page.stop()

    assert "did not answer within" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("over", "floor", "expected"),
    [
        ({"unit": "ms", "n": 400.0}, 60.0, 60.0),        # short ramp: floor wins
        ({"unit": "ms", "n": 120000.0}, 60.0, 130.0),    # Codex's --over 120000ms
        ({"unit": "bars", "n": 32}, 60.0, 138.0),        # Codex's 32 bars
        ({"unit": "beats", "n": 4}, 60.0, 60.0),
    ],
)
def test_the_request_deadline_never_undercuts_the_ramp_it_asked_for(
    over: dict, floor: float, expected: float
) -> None:
    """Over-estimating costs nothing here; under-estimating is a false timeout."""
    assert ramp_deadline_s(over, floor) == pytest.approx(expected)


def test_a_ramp_hands_the_client_the_deadline_its_duration_requires(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deadline must be DERIVED from the ramp, not merely large.

    Found by mutation: replacing the computed deadline with a big constant
    kept every other test green, because nothing asserted where the number
    came from. Waiting one out in real time would cost the suite ten seconds
    per case, so the wiring is read off the client instead.
    """
    import apps.opendj_cli.__main__ as cli

    seen: list[float] = []
    real = cli.EngineClient

    class Recorder:
        def __init__(self, **kwargs: object) -> None:
            seen.append(float(kwargs["timeout_s"]))  # type: ignore[arg-type]
            self._inner = real(**kwargs)  # type: ignore[arg-type]

        def __getattr__(self, name: str) -> object:
            return getattr(self._inner, name)

    monkeypatch.setattr(cli, "EngineClient", Recorder)

    page = engine.page()
    page.start()
    try:
        argv = _argv(engine, "--timeout", "0.2", "eq", "1", "low", "0.2", "--over", "400ms")
        assert main(argv) == EXIT_CONFIRMED
    finally:
        page.stop()

    assert seen == [pytest.approx(ramp_deadline_s({"unit": "ms", "n": 400.0}, 0.2))]
    assert seen[0] == pytest.approx(10.4)


def test_a_duration_unit_with_no_wall_clock_bound_is_refused_not_guessed() -> None:
    """Fail loud rather than size a deadline off a unit nobody has mapped."""
    with pytest.raises(InvocationError):
        ramp_deadline_s({"unit": "fortnights", "n": 2}, 60.0)


# ----- --json means --json, including before there is a namespace ----------

def test_an_argparse_refusal_under_json_is_a_json_document(
    capsys: pytest.CaptureFixture[str]
) -> None:
    """argparse calls error() while PARSING, so --json had to be read off argv."""

    with pytest.raises(SystemExit) as refusal:
        main(["--json", "--anchor", "bogus", "deck", "1", "play"])

    assert refusal.value.code == EXIT_FAILED
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert document["error"]["code"] == "usage"
    assert document["exit_code"] == EXIT_FAILED
    assert captured.err == ""


def test_an_argparse_refusal_without_json_stays_plain_text(
    capsys: pytest.CaptureFixture[str]
) -> None:
    """The control: JSON mode must not become the only mode."""

    with pytest.raises(SystemExit) as refusal:
        main(["--anchor", "bogus", "deck", "1", "play"])

    assert refusal.value.code == EXIT_FAILED
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "opendj:" in captured.err
