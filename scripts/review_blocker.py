"""The one parseable `BLOCKER: <reason>` line review-triage prints when it exits 1.

Contract with the nucbox trunk-bridge (Trunk session, Thu 1 Oct 2026), which posts
`opendj/review-triage` from `just review-triage <PR>` at a pinned main SHA: exit 0 is a
pass, exit 1 is a blocker found and exactly one `BLOCKER:` line names the FIRST one,
and any other exit is "could not measure" with no BLOCKER line. The order is the
order the gate checks in: reviewer coverage (which includes the control-plane dual
review, REVIEW-13), then review threads, then the debt ledger, then the PR scope.

Requirements (mini-PRD):
  / Exit 1 prints exactly one BLOCKER line; exit 0 and exit 3 print none.
    [if] a passing or unmeasured run prints BLOCKER [then] broken
  / An unanswered P2 thread is a blocker like any other untriaged thread.
    [if] a lone untriaged P2/NON-BLOCKING thread yields no BLOCKER line [then] broken
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol


class _Thread(Protocol):
    severity: str
    blocking: str
    path: str
    line: int | None
    permalink: str


def first_blocker(
    rc: int, coverage_rc: int, failing: Sequence[_Thread], debt_error: object, scope_rc: int
) -> str | None:
    """Pure: the BLOCKER line for this verdict, or None when there is none to print."""
    if rc != 1:
        return None
    if coverage_rc:
        reason = "review coverage (see the [review-coverage] FAIL line above)"
    elif failing:
        t = failing[0]
        where = f"{t.path}:{t.line}" if t.line else t.path
        more = f" (+{len(failing) - 1} more)" if len(failing) > 1 else ""
        reason = (
            f"review thread [{t.severity}/{t.blocking}] {where} not FIXED, REBUTTED or DEBT-LOGGED: {t.permalink}{more}"
        )
    elif debt_error:
        reason = f"debt ledger: {debt_error}"
    elif scope_rc == 1:
        reason = "PR scope exceeds its issue's declared 'Scope limit:' (see [pr-scope] above)"
    else:
        reason = "review-triage exited 1 without a recognized cause"
    return f"BLOCKER: {reason}"


def report(rc: int, coverage_rc: int, failing: Sequence[_Thread], debt_error: object, scope_rc: int) -> int:
    """Print the BLOCKER line (if any) and hand the exit code back unchanged."""
    line = first_blocker(rc, coverage_rc, failing, debt_error, scope_rc)
    if line:
        print(line)
    return rc
