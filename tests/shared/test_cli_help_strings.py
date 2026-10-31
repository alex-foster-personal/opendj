"""Every CLI parser that carries the LIBM-41 `--allow-mass-missing` guard renders its help.

Python 3.14's argparse expands each help string with `%` formatting when the
argument is ADDED, so a bare "50% of" (read as the `%o` conversion) makes
`build_parser()` raise "badly formed help string" before any command runs.
Older Pythons raise the same TypeError only when `--help` is printed. Both hit
the e2e gate on agbox2-2 (runs 37266287123, Mon 5 Oct 2026, and 37989056399,
Fri 9 Oct 2026), where the deck-load fixture seeds through `apps.shared.state.cli`.

Regression line: if a parser's help cannot be rendered then broken.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable

import pytest

from apps.mik import cli as mik_cli
from apps.reconcile import index_disk
from apps.shared.state import cli as state_cli

PARSERS: dict[str, Callable[[], argparse.ArgumentParser]] = {
    "apps.shared.state.cli": state_cli.build_parser,
    "apps.mik.cli": mik_cli.build_parser,
    "apps.reconcile.index_disk": index_disk._build_parser,
}


def _all_help(parser: argparse.ArgumentParser) -> str:
    """The parser's help plus every subcommand's, as `--help` would print it."""
    texts = [parser.format_help()]
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            texts.extend(_all_help(child) for child in action.choices.values())
    return "\n".join(texts)


@pytest.mark.parametrize("name", sorted(PARSERS))
def test_help_renders_the_mass_missing_guard(name: str) -> None:
    text = _all_help(PARSERS[name]())
    # Presence, not just "no exception": the guard's help is in the output, with
    # the percent sign rendered as one literal "%".
    assert "--allow-mass-missing" in text
    assert "50% of" in " ".join(text.split())
