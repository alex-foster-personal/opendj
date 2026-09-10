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
        assert page.mirror["decks"]["1"]["title"] == "warehouse"
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
