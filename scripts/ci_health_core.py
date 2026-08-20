"""Shared primitives for the CI health watchdog: verdicts, exit codes, and gh plumbing.

Extracted from scripts/ci_health_check.py so that scripts/ci_health_metrics.py can reuse
them without importing the checker back, which would be a cycle. The dependency runs one
way only:

    ci_health_core  <-  ci_health_metrics  <-  ci_health_check

Nothing here talks to the iteration-metrics store or decides a verdict; it is the vocabulary
the checks are written in plus the subprocess layer they read GitHub through.

-Claude
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime

# ----- configuration ---------------------------------------------------------------

REPO = "maintainer/music-dj-tools"

GH_BIN = "gh"
GH_TIMEOUT_SECONDS = 60

EXIT_OK = 0
EXIT_BILLING = 2
EXIT_TRIGGER_DRIFT = 3
EXIT_FAILURE_RATE = 4
EXIT_STALENESS = 5
EXIT_ITERATION_SPEED = 6
EXIT_PRECONDITION = 10

STALENESS_REMEDIATION = (
    "Pushes are landing but nothing runs. Check Actions is enabled for the repo "
    f"-> https://github.com/{REPO}/settings/actions"
)

# ----- data ------------------------------------------------------------------------


@dataclass
class Run:
    """One completed workflow run, reduced to the fields the checks reason about."""

    run_id: int
    name: str
    event: str
    head_branch: str
    conclusion: str
    started_at: datetime
    updated_at: datetime

    @property
    def duration_seconds(self) -> float:
        return (self.updated_at - self.started_at).total_seconds()

    @property
    def is_failure(self) -> bool:
        return self.conclusion == "failure"


@dataclass
class CheckResult:
    """Verdict for one named check, carrying its own denominator."""

    check: str
    ok: bool
    classification: str
    detail: str
    sample_size: int
    exit_code: int
    remediation: str = ""

    def verdict_line(self) -> str:
        token = "[OK]" if self.ok else "[ERROR]"
        return f"{token} {self.check}: {self.detail}"


class PreconditionError(RuntimeError):
    """Raised when the checker cannot trust its own inputs. Never swallowed."""


# ----- gh plumbing -----------------------------------------------------------------


def _run_gh(args: list[str]) -> str:
    """Call gh and return stdout, failing loudly on any non-zero exit."""
    if shutil.which(GH_BIN) is None:
        raise PreconditionError("gh CLI not found on PATH. Install it and run 'gh auth login'.")
    # check=False on purpose: the returncode is inspected below so the raised error can
    # name the exact gh invocation and carry its stderr, which CalledProcessError does not.
    completed = subprocess.run(
        [GH_BIN, *args],
        capture_output=True,
        text=True,
        timeout=GH_TIMEOUT_SECONDS,
        check=False,
    )
    if completed.returncode != 0:
        invocation = " ".join(args)
        raise PreconditionError(
            f"gh {invocation} failed with exit {completed.returncode}: {completed.stderr.strip()}"
        )
    return completed.stdout


def _gh_api_json(path: str) -> object:
    """GET a GitHub API path and parse the JSON body, failing loudly on garbage."""
    raw = _run_gh(["api", path])
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PreconditionError(f"gh api {path} returned unparseable JSON: {exc}") from exc


def _parse_github_timestamp(value: str, field: str, run_id: object) -> datetime:
    """Parse a GitHub ISO-8601 Z timestamp, naming the field when it is malformed."""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise PreconditionError(f"run {run_id} has an unparseable {field}: {value!r}") from exc
