"""Guards ops/fleet/kpi.sh's codex_reviews_per_merged_pr login matcher.

The Codex reviewer bot's login on the wire is `chatgpt-codex-connector[bot]`,
WITH the `[bot]` suffix. A bare equality test against the unsuffixed name
never matches it, so the ratio read 0.0 while Codex was reviewing normally:
measured live Wed 16 Sep 2026, 12 real Codex reviews across the 17 PRs merged
in a 6h window, zero usage-limit notices. A 0.0 there is a coverage claim the
fleet acted on (CLAUDE.md recorded "every PR after that merged un-reviewed" as
settled fact), so a silent regression back to bare equality is a real defect,
not a cosmetic one.

`scripts/review_coverage.py::_matches` already gets this right and is the
reference implementation: normalize case and strip the `[bot]` suffix, then
require FULL equality, never a substring test. The full-equality part is
load-bearing: issue #1016 P1 BLOCKING thread r3929765931 recorded that a
substring test accepted `chatgpt-codex-connector-attacker` as Codex.

The jq program is EXTRACTED out of ops/fleet/kpi.sh with a regex rather than
copied inline, so this test cannot pass against a stale duplicate of the
matcher -- if the matcher moves or is rewritten, extraction fails loudly
instead of silently testing dead code.

Regression lines:
  - [if] the live `chatgpt-codex-connector[bot]` login is not counted as a
    Codex review [then] broken (the case this test exists for).
  - [if] the bare `chatgpt-codex-connector` login (forward compatibility) is
    not counted [then] broken.
  - [if] login case is not normalized before comparison [then] broken.
  - [if] `chatgpt-codex-connector-attacker` (with or without `[bot]`) or
    `evil-chatgpt-codex-connector` is counted as Codex [then] broken (issue
    #1016: the equality must stay FULL, never a substring test).
  - [if] an unrelated login is counted [then] broken.
  - [if] an empty review list does not read as a real, error-free 0
    [then] broken (zero is a value, not the absence of a measurement).
  - MUTATION CONTROL: [if] the pre-fix bare-equality matcher scores the live
    `[bot]` login as anything other than 0 [then] this test would not have
    caught the bug it exists for, [else] the control proves the test bites.
  - INSTRUMENT CONTROL: [if] a deliberately malformed jq program does not
    error [then] broken (an instrument that cannot measure must say so, never
    render a failed measurement as a finding of zero).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

KPI_SH = Path(__file__).resolve().parents[2] / "ops" / "fleet" / "kpi.sh"

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq not on PATH")


def _extract_login_matcher_program() -> str:
    """Pull the codex_reviews_per_merged_pr jq program out of the real script.

    Matches on the marker string `chatgpt-codex-connector` inside a `jq -s
    '...'` invocation, so a rewrite that moves or renames the matcher fails
    this extraction loudly rather than silently testing stale text.
    """
    source = KPI_SH.read_text()
    match = re.search(r"jq -s '([^']*chatgpt-codex-connector[^']*)'", source)
    if match is None:
        pytest.fail(f"could not find the codex login jq matcher in {KPI_SH}")
    return match.group(1)


PROGRAM = _extract_login_matcher_program()

# The matcher this test exists to catch a regression back to: bare equality
# against the unsuffixed login, no case or suffix normalization. This is a
# literal snapshot of the historical bug, not a read of the current file --
# the mutation control below asserts it fails against the live-shape login.
PRE_FIX_PROGRAM = (
    '[.[] | (if type=="object" then .reviews[] else .[] end)] '
    '| map(select((.author.login // .user.login) == "chatgpt-codex-connector")) '
    "| length"
)


def _run_jq(program: str, payload: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["jq", "-s", program],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
    )


def _count(program: str, payload: object) -> int:
    result = _run_jq(program, payload)
    assert result.returncode == 0, (
        f"jq exited {result.returncode} for a well-formed payload "
        f"(stderr: {result.stderr!r}); a measurement failure must never be "
        f"read as a count of zero"
    )
    assert result.stderr == "", (
        f"jq wrote to stderr on a well-formed payload: {result.stderr!r} "
        f"(stderr must be visible: zero is both a value and an error signature)"
    )
    return int(result.stdout.strip())


# GraphQL shape: a single object with a `.reviews[]` array, each review
# carrying `.author.login`. This is the shape kpi.sh's fixtures use.
def _graphql(login: str) -> dict:
    return {"reviews": [{"author": {"login": login}}]}


# REST shape: a bare array of review objects, each carrying `.user.login`.
# This is the shape the live `gh api repos/.../pulls/<n>/reviews` call returns.
def _rest(login: str) -> list:
    return [{"user": {"login": login}}]


def test_instrument_control_malformed_program_errors_not_empty() -> None:
    """A tool that cannot measure must report an error, never a silent zero."""
    result = _run_jq(PROGRAM + " NONSENSE", [])
    assert result.returncode != 0, (
        "a malformed jq program did not error, so a real measurement failure "
        "here would be indistinguishable from a genuine zero count"
    )


def test_mutation_control_pre_fix_matcher_scores_live_login_as_zero() -> None:
    """Proves this test suite bites: the historical bug reads 0.0 for the live login."""
    got = _count(PRE_FIX_PROGRAM, _graphql("chatgpt-codex-connector[bot]"))
    assert got == 0, (
        f"expected the pre-fix bare-equality matcher to score the live "
        f"'[bot]' login as 0 (that is the bug this file exists to catch), "
        f"but it scored {got}; if this control does not read 0, the other "
        f"assertions in this file are not exercising a real regression"
    )


@pytest.mark.parametrize(
    "name,payload",
    [
        ("GraphQL, live [bot] suffix", _graphql("chatgpt-codex-connector[bot]")),
        ("REST, live [bot] suffix", _rest("chatgpt-codex-connector[bot]")),
        ("REST, bare login (forward compatible)", _rest("chatgpt-codex-connector")),
        ("REST, mixed case", _rest("ChatGPT-Codex-Connector[BOT]")),
    ],
)
def test_codex_login_counts(name: str, payload: object) -> None:
    assert _count(PROGRAM, payload) == 1, f"{name} should count as one Codex review"


@pytest.mark.parametrize(
    "name,payload",
    [
        ("impostor -attacker suffix", _rest("chatgpt-codex-connector-attacker")),
        ("impostor -attacker[bot] suffix", _rest("chatgpt-codex-connector-attacker[bot]")),
        ("impostor prefix", _rest("evil-chatgpt-codex-connector")),
        ("unrelated login", _rest("maintainer")),
    ],
)
def test_codex_login_does_not_count(name: str, payload: object) -> None:
    assert _count(PROGRAM, payload) == 0, (
        f"{name} must NOT count as Codex (issue #1016: equality must stay FULL, "
        f"never a substring test)"
    )


def test_mixed_payload_counts_only_the_real_codex_review() -> None:
    payload = [
        {"user": {"login": "maintainer"}},
        {"user": {"login": "chatgpt-codex-connector[bot]"}},
        {"user": {"login": "chatgpt-codex-connector-attacker"}},
    ]
    assert _count(PROGRAM, payload) == 1


def test_empty_review_list_is_a_real_zero_not_an_error() -> None:
    assert _count(PROGRAM, []) == 0
