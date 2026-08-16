"""M25: a mutating CLI must require an explicit --dry-run or --live.

    "--dryrun and --live dont default to dry run. refuse if no arg given.
     this is best practice?"

A bare ``--live`` flag with a silent dry-run default reads as safe, but the
operator has no way to say which they meant and argparse cannot tell a typo
from an intention. Every subcommand that can mutate state therefore declares a
REQUIRED, mutually exclusive mode pair.

The rule is derived, not listed: any subcommand carrying ``--live`` is treated
as mode-bearing, so a new mutating subcommand is covered the day it is added.

Mini-PRD
========
* [if] a mode-bearing subcommand is invoked with neither flag [then] argparse
  exits non-zero [else -] it proceeds in a guessed mode.
* [if] both flags are passed [then] argparse rejects them as mutually exclusive
  [else -] one silently wins.
* [if] --dry-run is passed [then] the parsed namespace says dry-run, so the
  command body cannot read it as live.
"""
from __future__ import annotations

import argparse

import pytest

from apps.stems.cli import build_parser as build_stems_parser
from apps.vocals.cli import build_parser as build_vocals_parser

CLIS = {
    "apps.stems": build_stems_parser,
    "apps.vocals": build_vocals_parser,
}
MODE_FLAGS = {"--live", "--dry-run", "--dryrun"}


def _subparsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    raise AssertionError("CLI has no subcommands - the mode rule cannot be checked")


def _option_strings(parser: argparse.ArgumentParser) -> set[str]:
    return {opt for action in parser._actions for opt in action.option_strings}


def _mode_bearing(build) -> list[tuple[str, argparse.ArgumentParser]]:
    """Every subcommand that can mutate state, i.e. that declares --live."""
    return [
        (name, sub)
        for name, sub in _subparsers(build()).items()
        if "--live" in _option_strings(sub)
    ]


def _other_required_args(parser: argparse.ArgumentParser) -> list[str]:
    """Minimal placeholder values for required options that are NOT mode flags."""
    args: list[str] = []
    for action in parser._actions:
        if not action.required or not action.option_strings:
            continue
        if set(action.option_strings) & MODE_FLAGS:
            continue
        args.append(action.option_strings[0])
        if action.nargs != 0 and not isinstance(action, argparse._StoreTrueAction):
            args.append("placeholder")
    return args


def test_every_cli_has_at_least_one_mode_bearing_subcommand() -> None:
    """Guards against the rule passing vacuously if --live is renamed away."""
    for cli, build in CLIS.items():
        assert _mode_bearing(build), f"{cli} declares no --live subcommand any more"


@pytest.mark.parametrize("cli", sorted(CLIS))
def test_a_mode_bearing_subcommand_refuses_to_run_without_an_explicit_mode(cli: str) -> None:
    """Neither flag given must exit non-zero, never guess a mode."""
    for name, sub in _mode_bearing(CLIS[cli]):
        argv = [name, *_other_required_args(sub)]
        with pytest.raises(SystemExit) as excinfo:
            CLIS[cli]().parse_args(argv)
        assert excinfo.value.code != 0, (
            f"{cli} {name} accepted {argv} with no explicit mode - it will "
            "proceed in a guessed mode"
        )


@pytest.mark.parametrize("cli", sorted(CLIS))
def test_passing_both_modes_is_rejected_rather_than_letting_one_win(cli: str) -> None:
    """--dry-run and --live together is ambiguous, so argparse must refuse."""
    for name, sub in _mode_bearing(CLIS[cli]):
        if "--dry-run" not in _option_strings(sub):
            continue
        argv = [name, *_other_required_args(sub), "--dry-run", "--live"]
        with pytest.raises(SystemExit) as excinfo:
            CLIS[cli]().parse_args(argv)
        assert excinfo.value.code != 0, f"{cli} {name} accepted both modes at once"


@pytest.mark.parametrize("cli", sorted(CLIS))
def test_each_mode_parses_to_a_distinguishable_namespace(cli: str) -> None:
    """The command body must be able to tell dry-run from live."""
    for name, sub in _mode_bearing(CLIS[cli]):
        if "--dry-run" not in _option_strings(sub):
            continue
        common = _other_required_args(sub)

        live = CLIS[cli]().parse_args([name, *common, "--live"])
        assert live.live is True
        assert getattr(live, "dry_run", False) is False

        dry = CLIS[cli]().parse_args([name, *common, "--dry-run"])
        assert dry.live is False, f"{cli} {name} --dry-run parsed as live"
        assert dry.dry_run is True


def test_the_stems_trickle_help_no_longer_advertises_a_silent_default() -> None:
    """The regression this rule was written for: 'default is dry-run listing only'."""
    trickle = _subparsers(build_stems_parser())["trickle"]
    help_text = trickle.format_help()

    assert "--dry-run" in help_text, "trickle must offer an explicit dry-run mode"
    assert "default is dry-run" not in help_text, (
        "trickle still advertises a silent dry-run default; a mode-bearing "
        "subcommand must state its mode, not assume one"
    )
