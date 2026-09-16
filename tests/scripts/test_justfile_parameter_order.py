"""Every justfile recipe parses under the OLDEST just the CI runners carry.

just 1.21 (agentbox and nucbox-wsl runners, Wed 16 Sep 2026) rejects a recipe whose
parameter with a default precedes one without ("Non-default parameter `ARGS` follows
default parameter"), and the rejection is for the WHOLE justfile: every `just` invocation
on every runner failed, including the two tests that shell out to `just -n` and
`just --show`. just 1.58 on the Macs accepts the same header, which is how the recipe
merged (#3340) with a green local run.

Single-line intent:
  - if a recipe declares a defaulted parameter before a required or variadic one then broken

[if] a recipe parameter has a default [then] every later parameter has one too, [else stop].
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest

pytestmark = pytest.mark.requirement("INFRA-03")

REPO = Path(__file__).resolve().parents[2]
JUSTFILE = REPO / "justfile"
NAME = re.compile(r"^(?P<name>[A-Za-z_][\w-]*)(?=\s|:)")


def header_params(line: str) -> str | None:
    """The parameter text of a recipe header at column 0, or None for anything else.

    The header ends at the first `:` OUTSIDE quotes that is not `:=` (an assignment). A
    colon inside a quoted default (`beatbench-r0 base="http://127.0.0.1:8685" ...`) is part
    of the default, and stopping there truncated the parameter list so a later required
    parameter went unseen (Codex, PR #3371).
    """
    match = NAME.match(line)
    if match is None:
        return None
    quote: str | None = None
    for index in range(match.end(), len(line)):
        char = line[index]
        if quote:
            if char == quote:
                quote = None
        elif char in ('"', "'"):
            quote = char
        elif char == ":":
            if line.startswith(":=", index):
                return None
            return line[match.end() : index]
    return None


def _params(raw: str) -> list[str]:
    """Split a header's parameters like just does: a quoted default is one token."""
    try:
        return shlex.split(raw)
    except ValueError:  # an apostrophe inside a default, e.g. `note="it's"`: split plainly
        return raw.split()


def recipe_headers(text: str) -> list[tuple[str, list[str]]]:
    headers = []
    for line in text.splitlines():
        if line.startswith((" ", "\t", "#", "set ", "import ", "mod ", "export ", "alias ")):
            continue
        params = header_params(line)
        if params is not None:
            # shlex, not str.split: `*args="python-engine 5"` is ONE parameter whose
            # default holds a space, and splitting on whitespace read its tail as a
            # second, defaultless parameter (a false offender on the first run).
            headers.append((NAME.match(line).group("name"), _params(params)))
    return headers


def defaulted_before_required(params: list[str]) -> bool:
    seen_default = False
    for param in params:
        has_default = "=" in param
        if seen_default and not has_default:
            return True
        seen_default = seen_default or has_default
    return False


def test_no_recipe_puts_a_defaulted_parameter_before_a_required_one() -> None:
    """if a recipe declares a defaulted parameter before a required or variadic one then broken"""
    offenders = [
        f"{name} {' '.join(params)}"
        for name, params in recipe_headers(JUSTFILE.read_text(encoding="utf-8"))
        if defaulted_before_required(params)
    ]
    assert offenders == [], (
        "these recipe headers fail to parse under the runners' just 1.21 and take every "
        f"`just` call in CI down with them: {offenders}"
    )


def test_the_guard_bites_on_the_shape_that_shipped() -> None:
    """control: the header that merged in #3340 is exactly what this test refuses"""
    assert defaulted_before_required(['TIER="fast"', "*ARGS"])
    assert not defaulted_before_required(["*ARGS"])
    assert not defaulted_before_required(["TIER", "*ARGS"])
    assert not defaulted_before_required(['lane=""'])
    assert not defaulted_before_required(["*args=python-engine 5"]), "a defaulted variadic is legal"


def test_a_colon_inside_a_quoted_default_does_not_end_the_header() -> None:
    """control: a required parameter AFTER a quoted URL default is still seen and refused"""
    line = 'recipe base="http://127.0.0.1:8685" REQUIRED:'
    assert _params(header_params(line) or "") == ['base=http://127.0.0.1:8685', "REQUIRED"]
    assert defaulted_before_required(_params(header_params(line) or ""))
    assert header_params("pytest_scope := \"tests/webui tests/x\"") is None, "an assignment"
    assert header_params("    indented body: not a header") is None


def test_the_header_scanner_sees_real_recipes() -> None:
    """control: an absent scan reads as zero offenders, so prove the scanner finds recipes"""
    names = {name for name, _ in recipe_headers(JUSTFILE.read_text(encoding="utf-8"))}
    assert {"fast-tier", "dmg", "pin-merged", "feedback-harvest", "beatbench-r0"} <= names
    quoted = dict(recipe_headers(JUSTFILE.read_text(encoding="utf-8")))["beatbench-r0"]
    assert any(param.startswith("base=http://") for param in quoted), quoted
