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

import copy
import importlib
import json
import socket
import threading
import time
import tomllib
from collections.abc import Callable
from pathlib import Path

import httpx
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
from apps.opendj_cli.orders import ramp_deadline_s, slowed_since
from apps.opendj_cli.verbs import InvocationError, parse_duration
from tests.opendj_cli.conftest import Engine, PerformancePage, blank_mirror

REPO_ROOT = Path(__file__).resolve().parents[2]

# Settle values in this file are asymmetric on purpose, and the asymmetry is the
# point rather than an accident of tuning.
#
# A test asserting EXIT_UNCONFIRMED wants a deadline it is CERTAIN to miss, so it
# passes a fraction of a second and a loaded runner only makes it surer. Those
# stay exactly as they are.
#
# A test asserting EXIT_CONFIRMED wants the opposite. The claim is "this settles",
# never "this settles quickly", and `--settle` is a MAXIMUM wait rather than a
# sleep, so a generous ceiling costs a passing run nothing while a tight one turns
# runner load into a false failure. Shard 1 of 5 returned EXIT_UNCONFIRMED on
# agentbox for a 0.5s case that passed 5 of 5 locally clean and again under a load
# average of 14 on 10 cores; the four other live confirm cases sat at 0.3s,
# strictly more fragile than the one that actually broke.
#
# Every live confirm case therefore waits on this, and nothing in this file
# asserts elapsed time. One pair is worth knowing about: the same
# `unload 1` argv appears twice, once expecting confirmed and once expecting
# unconfirmed, so the second one's short deadline is load-bearing and stays.
SETTLE_CONFIRM = "30"


def _argv(engine: Engine, *tokens: str) -> list[str]:
    return ["--lock", str(engine.lock_path), *tokens]


def _await_mirror(page: PerformancePage, ready: Callable[[dict], bool], what: str) -> None:
    """Block until the ENGINE serves the page's edit, then require it is ready.

    Editing `page.mirror` only changes what the page will publish on its next
    20ms tick, so a test that edits and immediately invokes the CLI is racing
    that tick. It won the race in isolation and lost it in the full suite,
    which is the worst way for a test to be wrong.

    So wait on the page's own signal: a publish that began after this call
    serialized the edit, and the engine's PUT replaces its copy before it
    answers 202 (apps/webui/server/routes/state.py), with the page thread as
    its only writer. From then on the document the CLI is about to GET holds
    the edit, so ONE read decides: a mirror that is not ready is a WRONG
    VALUE at once, not a longer wait. A page that never gets such a publish
    accepted fails inside the wait as HANG, REJECTED or DEAD instead.
    """
    page.wait_for_publish_after(page.publishes_started(), what)
    response = httpx.get(f"{page.base_url}/api/v1/state/ui-mirror", timeout=5.0)
    if response.status_code != 200 or not ready(response.json()):
        raise AssertionError(
            f"WRONG VALUE: the engine accepted a publish made after the edit but "
            f"does not serve {what}: {response.status_code} {response.text}"
        )


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
        json.dumps(
            {
                "pid": 1,
                "role": "opendj-engine",
                "host": "127.0.0.1",
                "port": dead_port,
                "boot_id": "dead-boot",
            }
        ),
        encoding="utf-8",
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
    elapsed = time.monotonic() - started
    assert elapsed >= 9.0

    captured = capsys.readouterr()
    assert "opendj open performance" in captured.err
    assert "409" in captured.err


def test_an_order_with_no_page_open_refuses_rather_than_hangs(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """There is no headless audio path, so the CLI must say so and stop."""

    started = time.monotonic()
    assert main(_argv(engine, "deck", "1", "play")) == EXIT_NO_PAGE
    elapsed = time.monotonic() - started

    assert elapsed >= 9.0, f"auto-ensure should wait ~10s before refusing, got {elapsed:.1f}s"
    assert "opendj open performance" in capsys.readouterr().err


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
        # `--settle 0.3` expired on loaded CI before the post-result republish
        # landed, so the CLI reported playing=False instead of the unpresented clock.
        assert main(_argv(engine, "--settle", "3", "deck", "1", "play")) == EXIT_UNCONFIRMED
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
    """A REPORTED failure, and the skip must say that is what happened.

    The other halt, a group the mirror would not confirm, prints its own
    reason: an agent choosing what to do next needs to know whether the page
    errored or merely failed to corroborate itself.
    """
    page = engine.page()
    page.fail_every_order = True
    page.start()
    try:
        code = main(_argv(engine, "do", "play 1", "then", "play 2"))
        assert code == EXIT_FAILED
    finally:
        page.stop()

    assert len(page.orders) == 1, "the second group must not be dispatched"
    captured = capsys.readouterr().out
    assert "skipped: play 2" in captured
    assert "an earlier step failed" in captured


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

        assert main(_argv(engine, "--settle", SETTLE_CONFIRM, "unload", "1")) == EXIT_CONFIRMED
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
    """``play 1`` then ``pause 1`` asked ONE mirror for true AND false.

    No single mirror can satisfy both, so a sequence that ran perfectly waited
    out the settle deadline and exited 4. Each group is now confirmed against
    the mirror IT left, so both checks hold and both are reported: the deck
    really was playing after the first order and really was not after the
    second.
    """

    page = engine.page()
    page.start()
    try:
        exit_code = main(
            _argv(engine, "--settle", SETTLE_CONFIRM, "do", "play 1", "then", "pause 1")
        )
        assert exit_code == EXIT_CONFIRMED
        assert page.mirror["decks"]["1"]["playing"] is False
    finally:
        page.stop()

    captured = capsys.readouterr()
    assert "verdict: confirmed" in captured.out
    assert "decks.1.playing == True" in captured.out
    assert "decks.1.playing == False" in captured.out


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


def _grid(
    bpm: float | None = 120.0, playing: bool = True, master_deck: int | None = 1
) -> dict:
    """A mirror carrying a clock the page could really ramp against."""
    mirror = blank_mirror()
    mirror["master_deck"] = master_deck
    for deck in mirror["decks"].values():
        deck["effective_bpm"] = bpm
        deck["playing"] = playing
    return mirror


@pytest.mark.parametrize(
    ("over", "bpm", "floor", "expected"),
    [
        ({"unit": "ms", "n": 400.0}, 120.0, 60.0, 60.0),      # short ramp: floor wins
        ({"unit": "ms", "n": 120000.0}, 120.0, 60.0, 130.0),  # Codex's --over 120000ms
        # Codex's 32 bars, twice. Same duration, same floor, two tempos: 128
        # beats is 128s at 60 BPM and 64s at 120, and only reading the grid
        # tells them apart. A constant cannot produce both rows.
        ({"unit": "bars", "n": 32}, 60.0, 60.0, 138.0),
        ({"unit": "bars", "n": 32}, 120.0, 60.0, 74.0),
        ({"unit": "beats", "n": 4}, 120.0, 60.0, 60.0),
    ],
)
def test_the_request_deadline_never_undercuts_the_ramp_it_asked_for(
    over: dict, bpm: float, floor: float, expected: float
) -> None:
    """Over-estimating costs nothing here; under-estimating is a false timeout."""
    assert ramp_deadline_s(over, floor, _grid(bpm)) == pytest.approx(expected)


def test_the_deadline_is_read_off_the_live_tempo_not_assumed(
    ) -> None:
    """A slow master must buy MORE time than a fast one, from the same order.

    The constant this replaced assumed 60 BPM was slower than any real deck.
    It is not: production `setTempoRatio` accepts any ratio inside the selected
    pitch range, and the +-100% range admits ratios down toward zero
    (`Math.abs(ratio - 1) * 100 > rangePct`, audio-engine.svelte.ts), so a deck
    can run at 30 BPM or less and a healthy 32-bar ramp still exits 5.
    """
    slow = ramp_deadline_s({"unit": "bars", "n": 32}, 0.0, _grid(30.0))
    fast = ramp_deadline_s({"unit": "bars", "n": 32}, 0.0, _grid(174.0))

    assert slow == pytest.approx(128 * 2.0 + 10.0)
    assert fast == pytest.approx(128 * (60.0 / 174.0) + 10.0)
    # The invariant, so this cannot rot into a pair of recorded numbers: half
    # the tempo is twice the wait.
    assert ramp_deadline_s({"unit": "bars", "n": 32}, 0.0, _grid(60.0)) == pytest.approx(
        (slow - 10.0) / 2 + 10.0
    )


@pytest.mark.parametrize(
    ("mirror", "why"),
    [
        (_grid(master_deck=None), "no master is elected, so `_ramp` throws no_master"),
        (_grid(playing=False), "the clock is stopped, so `_ramp` throws clock_not_playing"),
        (_grid(bpm=None), "the deck has no tempo yet, so nothing can be sized"),
        ({"master_deck": 1, "decks": {}}, "the clock deck is not in the mirror"),
        ({}, "the mirror answered nothing at all"),
    ],
)
def test_a_grid_that_cannot_size_the_wait_leaves_the_floor_alone(
    mirror: dict, why: str
) -> None:
    """Each of these is a page REFUSAL, not a long hold, so --timeout stands.

    Guessing a tempo for them is what the fixed constant did. This is also the
    overshoot control: a fix that always inflates the deadline would keep the
    CLI waiting 138s on an order the page rejected in a millisecond.

    The floor is deliberately SMALLER than the ramp margin. At floor 60 this
    assertion passed while the code still added a 10s margin to a hold it had
    just failed to measure, because 60 swallowed the 10 - a clean answer for a
    case that should have been messy. The live test over HTTP caught it.
    """
    assert ramp_deadline_s({"unit": "bars", "n": 32}, 0.5, mirror) == 0.5, why


def test_a_usable_grid_does_raise_the_deadline() -> None:
    """The control for the row above: the floor must not become the only answer."""
    assert ramp_deadline_s({"unit": "bars", "n": 32}, 0.5, _grid(60.0)) > 0.5


def test_a_named_clock_deck_is_the_one_the_deadline_is_sized_from() -> None:
    """`--clock 2` times against deck 2, so deck 2's tempo sizes the wait.

    Source: `_ramp` resolves `over.clock` to the named deck and only falls back
    to `before.master_deck` for 'master' (agent-orders.ts).
    """
    mirror = _grid(120.0)
    mirror["decks"]["2"]["effective_bpm"] = 60.0

    named = ramp_deadline_s({"unit": "beats", "n": 32, "clock": 2}, 0.0, mirror)
    default = ramp_deadline_s({"unit": "beats", "n": 32}, 0.0, mirror)

    assert named == pytest.approx(32 * 1.0 + 10.0)
    # The control: reading the master instead would give this number for both.
    assert default == pytest.approx(32 * 0.5 + 10.0)


def test_a_named_clock_that_is_stopped_leaves_the_floor_alone() -> None:
    """The named deck's own playing state decides, not the master's."""
    mirror = _grid(120.0)
    mirror["decks"]["2"]["playing"] = False

    assert ramp_deadline_s({"unit": "beats", "n": 32, "clock": 2}, 0.5, mirror) == 0.5


def test_a_ramp_reports_the_deadline_its_duration_required(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """The deadline must be DERIVED from the ramp, not merely large.

    Found by mutation: replacing the computed deadline with a big constant
    kept every other test green, because nothing asserted where the number
    came from. The first attempt at this test monkeypatched the client to
    watch the value go by, which AGENTS.md forbids and which tests the patch
    rather than the product. The CLI publishes the deadline it used in its own
    --json document instead, so an agent can read what it got and this test
    reads the same real output. The JSON field is the product-owned
    observation of the deadline; an arbitrary large constant still fails the
    exact derived-value assertion below.
    """

    page = engine.page()
    page.start()
    try:
        argv = _argv(
            engine, "--json", "--timeout", "0.2", "eq", "1", "low", "0.2", "--over", "400ms"
        )
        assert main(argv) == EXIT_CONFIRMED
        document = json.loads(capsys.readouterr().out)
    finally:
        page.stop()

    assert document["request_timeout_s"] == pytest.approx(
        ramp_deadline_s({"unit": "ms", "n": 400.0}, 0.2, _grid())
    )
    assert document["request_timeout_s"] == pytest.approx(10.4)


def test_a_duration_unit_with_no_wall_clock_bound_is_refused_not_guessed() -> None:
    """Fail loud rather than size a deadline off a unit nobody has mapped."""
    with pytest.raises(InvocationError):
        ramp_deadline_s({"unit": "fortnights", "n": 2}, 60.0, _grid())


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


# ----- a later command's side effects are not confined to its own paths ----

def test_play_then_unload_confirms_although_unload_clears_playing(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """Production `unload` swaps in `_emptyDeckState`, which also clears playing.

    Dropping same-path checks was not enough: `play` names `decks.1.playing`
    and `unload` names the title and id, so the `playing == true` check
    survived into a final mirror that correctly reads false and a perfect run
    exited 4. Each group is now confirmed against the mirror it left, which
    needs no model of what a command touches.
    """

    page = engine.page()
    page.start()
    try:
        assert main(_argv(engine, "load", "1", "a-track")) == EXIT_CONFIRMED
        capsys.readouterr()
        exit_code = main(
            _argv(engine, "--settle", SETTLE_CONFIRM, "do", "play 1", "then", "unload 1")
        )
        assert exit_code == EXIT_CONFIRMED
        assert page.mirror["decks"]["1"]["playing"] is False
        assert page.mirror["decks"]["1"]["stable_id"] is None
    finally:
        page.stop()

    captured = capsys.readouterr()
    assert "verdict: confirmed" in captured.out
    assert "decks.1.playing == True" in captured.out


def test_a_group_that_does_not_land_is_still_unconfirmed_mid_script(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """The control: per-group confirmation must not stop confirming anything.

    An implementation that simply dropped every earlier check would pass the
    test above just as happily.
    """

    page = engine.page()
    page.apply_commands = False
    page.start()
    try:
        exit_code = main(_argv(engine, "--settle", "0.3", "do", "play 1", "then", "unload 1"))
        assert exit_code == EXIT_UNCONFIRMED
    finally:
        page.stop()

    assert "decks.1.playing is False, expected True" in capsys.readouterr().out


# ----- an anchored ramp waits before it travels ----------------------------

@pytest.mark.parametrize(
    ("over", "expected"),
    [
        ({"unit": "beats", "n": 4}, 60.0),
        ({"unit": "beats", "n": 4, "anchor": "next_beat"}, 60.0),
        # Codex's case: a 2s move behind a wait of nearly a whole phrase.
        ({"unit": "beats", "n": 4, "anchor": "next_phrase"}, 142.0),
    ],
)
def test_the_deadline_covers_the_anchor_wait_not_only_the_travel(
    over: dict, expected: float
) -> None:
    """The page waits for the anchor boundary FIRST, then runs the ramp."""
    assert ramp_deadline_s(over, 60.0, _grid(60.0)) == pytest.approx(expected)


def test_an_ms_ramp_is_not_padded_for_an_anchor_production_ignores() -> None:
    """`_ramp` measures an ms ramp against the WALL CLOCK, not the plan.

    `progress = (performance.now() - startedAt) / over.n` for `unit === 'ms'`,
    and the resolved plan, anchor and all, goes unread on that branch. So an
    anchored ms ramp holds for exactly its own duration and padding the
    deadline for the anchor would be sizing for behavior production does not
    have. The beat-relative branch is the one that waits, and it is covered
    above.
    """
    padded = ramp_deadline_s(
        {"unit": "ms", "n": 2000.0, "anchor": "next_phrase"}, 0.2, _grid(60.0)
    )

    assert padded == pytest.approx(2.0 + 10.0)


def test_an_anchor_with_no_wall_clock_bound_is_refused_not_guessed() -> None:
    """The control: an unmapped anchor must fail loud, not silently add zero."""
    with pytest.raises(InvocationError):
        ramp_deadline_s(
            {"unit": "beats", "n": 4, "anchor": "next_eclipse"}, 60.0, _grid(60.0)
        )


# ----- a scripted command carries the text values a direct one does --------


# ----- the deadline is sized from the mirror the page really published -----

def test_a_ramp_sizes_its_deadline_from_the_live_mirror(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """End to end, over HTTP: two tempos, two deadlines, one order.

    The unit tests above pin the arithmetic; this pins the WIRING. The CLI has
    to read the mirror before it sizes the request deadline, and read the real
    one rather than a default, so the same `--over 4beats` against a 30 BPM
    master and a 240 BPM master must not come back with the same number.
    """

    page = engine.page()
    page.mirror["master_deck"] = 1
    page.mirror["decks"]["1"] |= {"playing": True, "effective_bpm": 30.0}
    page.start()
    try:
        argv = _argv(
            engine, "--json", "--timeout", "0.2", "eq", "1", "low", "0.2", "--over", "4beats"
        )
        _await_mirror(
            page, lambda m: m["decks"]["1"]["effective_bpm"] == 30.0, "the 30 BPM master"
        )
        assert main(argv) == EXIT_CONFIRMED
        slow = json.loads(capsys.readouterr().out)
        page.mirror["decks"]["1"]["effective_bpm"] = 240.0
        _await_mirror(
            page, lambda m: m["decks"]["1"]["effective_bpm"] == 240.0, "the 240 BPM master"
        )
        assert main(argv) == EXIT_CONFIRMED
        fast = json.loads(capsys.readouterr().out)
    finally:
        page.stop()

    assert slow["request_timeout_s"] == pytest.approx(4 * 2.0 + 10.0)
    assert fast["request_timeout_s"] == pytest.approx(4 * 0.25 + 10.0)


def test_a_ramp_with_no_master_to_time_against_keeps_the_floor(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """The control: the live path must keep the floor where the page refuses.

    A CLI that inflated every ramp deadline would pass the test above and then
    wait 10s on an order `_ramp` threw `no_master` at immediately.
    """

    page = engine.page()
    page.mirror["master_deck"] = None
    page.mirror["decks"]["1"]["playing"] = True
    page.start()
    try:
        argv = _argv(
            engine, "--json", "--timeout", "7.5", "eq", "1", "low", "0.2", "--over", "4beats"
        )
        _await_mirror(page, lambda m: m["master_deck"] is None, "a mirror with no master")
        assert main(argv) == EXIT_CONFIRMED
        document = json.loads(capsys.readouterr().out)
    finally:
        page.stop()

    assert document["request_timeout_s"] == pytest.approx(7.5)


# ----- a deadline that cannot expire is not a deadline ---------------------

@pytest.mark.parametrize("flag", ["--settle", "--timeout"])
@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "-1"])
def test_a_deadline_that_can_never_pass_is_refused(
    flag: str, value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--settle nan` polls the mirror forever: NaN loses every comparison.

    `time.monotonic() >= deadline` is False for a NaN deadline however long the
    CLI has waited, so an unconfirmed command never reaches its exit 4 and the
    agent driving it hangs with no error to read. `inf` wedges the same loop,
    and a negative deadline is a typo rather than an instruction. Both flags
    feed the same comparison, so both are validated - fixing only the reported
    one leaves the class open.
    """

    with pytest.raises(SystemExit) as refusal:
        main([flag, value, "state"])

    assert refusal.value.code == EXIT_FAILED
    assert f"argument {flag}: " in capsys.readouterr().err


def test_a_finite_deadline_is_still_accepted(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """The control: validating the flag must not reject the ordinary value.

    0 is accepted deliberately - a zero settle is 'do not wait', which is a
    real instruction unlike a negative one. It is asserted at parse level
    because a zero settle also races the page's republish, so a verdict is not
    what it pins.

    The live half below is where shard 1 of 5 failed on agentbox with
    EXIT_UNCONFIRMED: it used `--settle 0.5` and lost a sub-second mirror
    deadline to runner load. It now waits on SETTLE_CONFIRM, where the
    reasoning for every live confirm case in this file is stated once.
    """

    assert main(["--settle", "0", "--timeout", "0", "--list-verbs"]) == EXIT_CONFIRMED
    assert capsys.readouterr().out != ""

    page = engine.page()
    page.start()
    try:
        assert main(
            _argv(engine, "--settle", SETTLE_CONFIRM, "--timeout", "30", "play", "1")
        ) == EXIT_CONFIRMED
    finally:
        page.stop()

    assert "verdict: confirmed" in capsys.readouterr().out


# ----- a script stops where its confirmation stops -------------------------

def test_a_script_stops_after_a_group_the_mirror_will_not_confirm(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unconfirmed group must halt the script, exactly as a failed step does.

    `do "load 1 <id>" then "play 1"` is the case that matters. If the load
    never lands, the deck still holds whatever was there before, so dispatching
    `play` starts the PREVIOUS track: the CLI exits 4 afterwards, but the wrong
    music is already in the room. The page here reports every step succeeded
    and simply does not move, which is the live fault this CLI exists to catch.
    """

    page = engine.page()
    page.apply_commands = False
    page.start()
    try:
        exit_code = main(
            _argv(engine, "--settle", "0.3", "do", "load 1 new-id", "then", "play 1")
        )
        assert exit_code == EXIT_UNCONFIRMED
        claimed = [order["payload"]["type"] for order in page.orders]
    finally:
        page.stop()

    assert claimed == ["load"], "the page was handed play after the load never landed"
    captured = capsys.readouterr().out
    assert "skipped: play 1" in captured
    # The two halts must not read alike: the page reported this step
    # SUCCEEDED, and only the mirror refused it.
    assert "an earlier group was not confirmed" in captured
    assert "an earlier step failed" not in captured


def test_a_script_whose_groups_confirm_runs_all_of_them(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """The control, and the overshoot this fix could have made.

    Halting on anything short of confirmed would satisfy the report above just
    as well by never running a second group at all.
    """

    page = engine.page()
    page.start()
    try:
        exit_code = main(
            _argv(engine, "--settle", SETTLE_CONFIRM, "do", "load 1 new-id", "then", "play 1")
        )
        assert exit_code == EXIT_CONFIRMED
        claimed = [order["payload"]["type"] for order in page.orders]
        assert page.mirror["decks"]["1"]["playing"] is True
    finally:
        page.stop()

    assert claimed == ["load", "play"]
    assert "skipped" not in capsys.readouterr().out


# ----- a run is only as strong as its weakest group ------------------------

def test_a_script_is_only_as_confirmed_as_its_weakest_group(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """One accepted group means the run cannot claim confirmed.

    `slip` is real deck state `buildUiMirror` does not publish, so its group is
    accepted and never confirmed. A process-wide 'something affirmed' flag let
    the `play` group's confirmation stand in for it and reported the whole run
    confirmed, which tells an agent the slip was observed when nothing observed
    it. Both orders are checked because a flag set by the FIRST group is the
    shape the defect had.
    """

    page = engine.page()
    page.start()
    try:
        assert main(_argv(engine, "--json", "do", "play 1", "then", "slip 1 true")) == (
            EXIT_CONFIRMED
        )
        after = json.loads(capsys.readouterr().out)
        assert main(_argv(engine, "--json", "do", "slip 1 true", "then", "play 2")) == (
            EXIT_CONFIRMED
        )
        before = json.loads(capsys.readouterr().out)
        claimed = [order["payload"]["type"] for order in page.orders]
    finally:
        page.stop()

    assert after["verdict"] == "accepted"
    # Not just the LAST group's verdict, which would read confirmed here.
    assert before["verdict"] == "accepted"
    # And accepted is not a halt: only an unconfirmed group stops the script,
    # so every group in both runs still reached the page.
    assert claimed == ["play", "slip", "slip", "play"]


def test_a_script_every_group_of_which_affirms_is_still_confirmed(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """The control: weakest-wins must not make confirmed unreachable.

    The overshoot for this finding is a run that reports accepted whenever it
    has more than one group, which no report of the bug would object to.
    """

    page = engine.page()
    page.start()
    try:
        assert main(_argv(engine, "--json", "do", "play 1", "then", "play 2")) == (
            EXIT_CONFIRMED
        )
        document = json.loads(capsys.readouterr().out)
    finally:
        page.stop()

    assert document["verdict"] == "confirmed"


# ----- what the CLI can answer alone, it answers alone ---------------------

@pytest.mark.parametrize(
    "tokens",
    [
        ["frobnicate"],
        ["beat_loop", "1", "nope"],
        ["play", "9"],
        ["--over", "infms", "eq", "1", "low", "0.2"],
    ],
)
def test_a_malformed_command_is_a_usage_error_even_with_no_engine(
    tokens: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Exit 1, not 2, because the engine has nothing to do with a typo.

    `resolve_origin` ran BEFORE the invocation was read, so with the app closed
    a misspelled verb came back as `engine_not_running`. That makes a typo
    indistinguishable from a stopped app and stops a caller checking a
    command's syntax offline, which is exactly what an agent wants to do before
    it dispatches anything.
    """
    monkeypatch.setenv("OPENDJ_LIVE_LOCK_PATH", str(tmp_path / "absent" / ".engine.lock"))

    assert main(tokens) == EXIT_FAILED

    captured = capsys.readouterr()
    assert "opendj:" in captured.err
    assert "engine" not in captured.err.lower(), "a typo was reported as an environment fault"


def test_a_well_formed_command_with_no_engine_still_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The control: moving the parse earlier must not swallow the exit-2 case.

    Exit 2 and the lock path it checked are the load-bearing part of the
    contract for a VALID command, and a fix that reported usage for everything
    would satisfy the report above just as well.
    """
    missing = tmp_path / "absent" / ".engine.lock"
    monkeypatch.setenv("OPENDJ_LIVE_LOCK_PATH", str(missing))

    assert main(["play", "1"]) == EXIT_NO_ENGINE
    assert str(missing) in capsys.readouterr().err


# ----- a duration magnitude must be a number time can reach ----------------

@pytest.mark.parametrize("raw", ["infms", "1e309ms", "-infms", "nanms", "infbeats"])
def test_a_duration_that_is_not_a_finite_number_is_refused(raw: str) -> None:
    """`--over infms` became an INFINITE request deadline.

    The same never-expiring wait `--settle nan` produced, reached by another
    road, and my reply on that thread wrongly said this one was already closed.
    It was not: `nan` was refused only because `not nan > 0` happens to be True,
    which named the wrong problem, and `inf` was not refused at all on the `ms`
    path because `is_integer()` never runs there.
    """
    with pytest.raises(InvocationError, match="finite"):
        parse_duration(raw, anchor=None, clock=None)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("500ms", {"unit": "ms", "n": 500.0}),
        ("0.5ms", {"unit": "ms", "n": 0.5}),
        ("32bars", {"unit": "bars", "n": 32}),
        ("1e3ms", {"unit": "ms", "n": 1000.0}),
    ],
)
def test_a_finite_duration_is_still_read_exactly_as_before(
    raw: str, expected: dict
) -> None:
    """The control: the finiteness check must not narrow what a duration accepts.

    `ms` deliberately keeps fractional and exponent forms; only the beat units
    require whole numbers, and that rule is unchanged.
    """
    assert parse_duration(raw, anchor=None, clock=None) == expected


# ----- a slowed clock is not a wedged page ---------------------------------

def test_a_ramp_that_times_out_on_a_slowed_clock_says_so(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """The deadline is a snapshot, so at least say when the snapshot went stale.

    A deck slowed after dispatch travels the same musical distance in more wall
    time, and the CLI cannot extend a request already in flight: the engine
    holds `POST /api/v1/commands` open until the page answers and exposes no
    order-status route to poll. What it must not do is report that as a wedged
    page, because the two demand opposite responses and only one of them is
    fixed by a longer --timeout.

    This test costs about ten seconds of wall clock, and cannot cost less: the
    ramp margin is ten seconds, so any deadline sized off a real grid is at
    least that long. It is the only test here that waits out a real timeout.
    """

    page = engine.page()
    page.mirror["master_deck"] = 1
    page.mirror["decks"]["1"] |= {"playing": True, "effective_bpm": 600.0}
    page.start()
    # Wedge it: the mirror stays registered, nothing will ever claim the order.
    page._running = False
    if page._thread is not None:
        page._thread.join(timeout=5)
    slowed = copy.deepcopy(page.mirror)
    slowed["decks"]["1"]["effective_bpm"] = 60.0
    changer = threading.Timer(
        1.0,
        lambda: httpx.put(
            f"{engine.base_url}/api/v1/state/ui-mirror", json=slowed, timeout=5.0
        ),
    )
    changer.start()
    try:
        argv = _argv(engine, "--timeout", "0.5", "eq", "1", "low", "0.2", "--over", "1beats")
        assert main(argv) == EXIT_TIMEOUT
    finally:
        changer.cancel()
        page.stop()

    error = capsys.readouterr().err
    assert "SLOWED" in error
    assert "600 BPM then, 60 BPM now" in error
    assert "--timeout" in error


def test_a_timeout_with_no_slowdown_is_reported_plainly(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """The control, and the overshoot: not every timeout is a slowed clock.

    A wedged page with no grid to size against keeps the --timeout floor and
    must still read as a plain timeout. A fix that appended the slowdown
    sentence to every ramp timeout would satisfy the report and mislead in the
    commoner case.
    """

    page = engine.page()
    page.mirror["master_deck"] = None
    page.start()
    page._running = False
    if page._thread is not None:
        page._thread.join(timeout=5)
    try:
        argv = _argv(engine, "--timeout", "0.5", "eq", "1", "low", "0.2", "--over", "32bars")
        assert main(argv) == EXIT_TIMEOUT
    finally:
        page.stop()

    error = capsys.readouterr().err
    assert "did not answer within" in error
    assert "SLOWED" not in error


def test_the_json_document_names_the_tempo_the_deadline_was_sized_at(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """An agent should not have to infer WHY its deadline is the length it is.

    `request_timeout_s` says how long; `ramp_clock_bpm` says what that was
    computed from, which is also what a later mirror read is compared against.
    """

    page = engine.page()
    page.mirror["master_deck"] = 1
    page.mirror["decks"]["1"] |= {"playing": True, "effective_bpm": 96.0}
    page.start()
    try:
        _await_mirror(
            page, lambda m: m["decks"]["1"]["effective_bpm"] == 96.0, "the 96 BPM master"
        )
        argv = _argv(
            engine, "--json", "--timeout", "1", "eq", "1", "low", "0.2", "--over", "4bars"
        )
        assert main(argv) == EXIT_CONFIRMED
        ramped = json.loads(capsys.readouterr().out)
        assert main(_argv(engine, "--json", "eq", "1", "low", "0.3")) == EXIT_CONFIRMED
        plain = json.loads(capsys.readouterr().out)
    finally:
        page.stop()

    assert ramped["ramp_clock_bpm"] == pytest.approx(96.0)
    assert ramped["request_timeout_s"] == pytest.approx(16 * (60.0 / 96.0) + 10.0)
    # The control: a command with no ramp has no ramp clock, and must say so
    # rather than reporting a tempo it never used.
    assert plain["ramp_clock_bpm"] is None


@pytest.mark.parametrize(
    ("sized_at_bpm", "now_bpm", "expected"),
    [
        (120.0, 60.0, True),    # slowed: the deadline is now too short
        (120.0, 120.0, False),  # unchanged: a wedged page, not a slowed clock
        (120.0, 174.0, False),  # sped up: the deadline is if anything generous
        (120.0, None, False),   # the mirror can no longer answer
    ],
)
def test_only_a_slowdown_blames_the_clock_for_a_timeout(
    sized_at_bpm: float, now_bpm: float | None, expected: bool
) -> None:
    """The overshoot control, and the reason this decision is a pure function.

    My first cut kept the comparison inside the message builder, and the only
    control I had covered the case where no tempo was ever read. Mutating the
    guard to blame the clock for EVERY ramp timeout left the suite green,
    because that control returned before ever reaching the guard. A test that
    cannot fail for the reason under test is not a control.
    """
    sentence = slowed_since(
        {"unit": "bars", "n": 32}, 60.0 / sized_at_bpm, _grid(now_bpm)
    )

    assert (sentence is not None) is expected
    if expected:
        assert sentence is not None
        assert "120 BPM then, 60 BPM now" in sentence


def test_a_stopped_clock_does_not_blame_a_timeout_on_a_slowdown() -> None:
    """The other way the mirror stops answering: the deck was paused.

    A stopped clock reads as unmeasurable, not as infinitely slow, so it must
    not produce the sentence either.
    """
    assert slowed_since({"unit": "bars", "n": 32}, 0.5, _grid(60.0, playing=False)) is None
