"""Evaluate a GitHub Actions `if:` condition against a PR context, for workflow tests.

Covers the grammar the Trunk queue-draft conditions use: `${{ ... }}`, `startsWith`,
`==`, `!=`, `&&`, `||`, `!` and parentheses over `github.*` context paths. A context path
the caller did not supply raises KeyError, and any other token fails to evaluate, so an
unmodelled condition is a loud test failure, never a silent guess.

-Claude
"""

from __future__ import annotations

import re
from dataclasses import dataclass

TRUNK_APP_LOGIN = "trunk-io[bot]"  # REST user.login on Trunk's draft PRs, e.g. #4355 (id 85644782)
REPO = "maintainer/music-dj-tools"


@dataclass(frozen=True)
class PrEvent:
    head_ref: str
    author: str
    head_repo: str = REPO

    def context(self) -> dict[str, str]:
        return {
            "github.head_ref": self.head_ref,
            "github.event.pull_request.user.login": self.author,
            "github.event.pull_request.head.repo.full_name": self.head_repo,
            "github.repository": REPO,
            "github.event_name": "pull_request",
        }


GENUINE_DRAFT = PrEvent("trunk-merge/pr-4227/0b1c2d3e", TRUNK_APP_LOGIN)
SPOOFED_BRANCH = PrEvent("trunk-merge/bypass", "some-human")
SPOOFED_FORK = PrEvent("trunk-merge/bypass", TRUNK_APP_LOGIN, head_repo="attacker/music-dj-tools")
ORDINARY_PR = PrEvent("af--some-feature", "some-human")
ALL_EVENTS = (GENUINE_DRAFT, SPOOFED_BRANCH, SPOOFED_FORK, ORDINARY_PR)


# -----------------------------------------------------------------------------
def condition_holds(condition: str | bool | None, event: PrEvent) -> bool:
    if condition is None:
        return True
    if isinstance(condition, bool):
        return condition
    text = condition.strip()
    if text.startswith("${{") and text.endswith("}}"):
        text = text[3:-2]
    # Operators first, context values last, so a value can never be read as an operator.
    python = text.replace("startsWith(", "_starts_with(")
    python = python.replace("&&", " and ").replace("||", " or ")
    python = re.sub(r"!(?!=)", " not ", python)
    context = event.context()
    python = re.sub(r"\bgithub(?:\.[\w-]+)+", lambda m: repr(context[m.group(0)]), python)
    return bool(eval(python, {"__builtins__": {}, "_starts_with": _starts_with}))


def _starts_with(value: str, prefix: str) -> bool:
    """GitHub's startsWith ignores case."""
    return value.lower().startswith(prefix.lower())
