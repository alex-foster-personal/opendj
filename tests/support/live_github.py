"""The one opt-in gate for tests that call the real GitHub API through `gh`.

A test that reaches the live API spends a token's quota and depends on GitHub being up.
In PR CI the shared runner token is spent by every job on the fleet, so an ungated live
test turns an unrelated PR red whenever that hour's budget is gone: Thu 1 Oct 2026, run
36823458339, `gh: API rate limit exceeded for user ID 166056029` failed PR #4678's
shard 5 on a test the PR never touched. A rate limit, an outage or a missing token is a
measurement that could not be taken, so it must never surface as a pass or a fail.

So every live test is UNAVAILABLE (skipped, with that reason printed) unless
`MDT_LIVE_GITHUB=1`. Opting in is a CLAIM that `gh` is authenticated, so under the opt-in
the `gh_authenticated` fixture FAILS rather than skips. Registered as a plugin in the root
`conftest.py`, so the fixture needs no import; the decorator does.

Before this module, seven test files each carried their own copy of the flag, the reason,
the decorator and the fixture, and two live tests carried none.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from typing import TypeVar

import pytest

F = TypeVar("F", bound=Callable[..., object])

MDT_LIVE_GITHUB = os.environ.get("MDT_LIVE_GITHUB") == "1"
LIVE_GITHUB_UNAVAILABLE = (
    "UNAVAILABLE: live GitHub API checks not run. This is a capability report, not a "
    "pass. Enable with MDT_LIVE_GITHUB=1 and gh authenticated (gh auth status)."
)


def live_github(fn: F) -> F:
    """Mark AND opt-in-gate a real-`gh`-network test: `-m live_github` selects it, and it
    is UNAVAILABLE unless `MDT_LIVE_GITHUB=1`. Pair it with the `gh_authenticated`
    fixture, so that once opted in an unauthenticated `gh` fails loudly."""
    gated = pytest.mark.skipif(not MDT_LIVE_GITHUB, reason=LIVE_GITHUB_UNAVAILABLE)(fn)
    return pytest.mark.live_github(gated)


@pytest.fixture
def gh_authenticated() -> None:
    """Only ever runs under `MDT_LIVE_GITHUB=1` (the decorator skips otherwise). Opting in
    is a claim that `gh` is authenticated, so a failure here is a FAIL, never a skip."""
    result = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        pytest.fail(
            "MDT_LIVE_GITHUB=1 was set but gh is not authenticated, so the live contract "
            f"is UNVERIFIED rather than passing: {result.stderr.strip()}"
        )
