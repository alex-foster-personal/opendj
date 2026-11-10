"""The one parseable `BLOCKER: <reason>` line review-triage prints when it exits 1.

Contract with the nucbox trunk-bridge (Trunk session, Thu 1 Oct 2026), which posts
`opendj/review-triage` from `just review-triage <PR>` at a pinned main SHA: exit 0 is a
pass, exit 1 is a blocker found and exactly one `BLOCKER:` line names the FIRST one,
and any other exit is "could not measure" with no BLOCKER line. The order is the
order the gate checks in: review threads, then carried review-body findings, then
the debt ledger, then the PR scope. Reviewer coverage and the control-plane review
are advisory since REVIEW-8220 (the maintainer, 19:30Z Mon 2 Nov 2026) and are never a BLOCKER.

Requirements (mini-PRD):
  / Exit 1 prints exactly one BLOCKER line; exit 0 and exit 3 print none.
    [if] a passing or unmeasured run prints BLOCKER [then] broken
  / A run that cannot measure says why on one parseable `UNKNOWN: <reason>` line.
    [if] exit 3 leaves the bridge with no reason to show [then] broken
  / REVIEW-8220: missing review coverage is never a blocker.
    [if] a run whose only gap is a missing review prints BLOCKER [then] broken
  / REVIEW-22: only an open P0 or human BLOCKING thread is a thread blocker.
    [if] a lone untriaged P0 thread yields no BLOCKER line [then] broken
    [if] a lone untriaged bot P1 thread yields a BLOCKER line [then] broken
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from scripts.review_thread_parse import Thread


def first_blocker(
    rc: int,
    failing: Sequence[Thread],
    debt_error: object,
    scope_rc: int,
    *,
    carried_findings: Sequence[object] = (),
) -> str | None:
    """Pure: the BLOCKER line for this verdict, or None when there is none to print."""
    if rc != 1:
        return None
    if failing:
        t = failing[0]
        where = f"{t.path}:{t.line}" if t.line else t.path
        more = f" (+{len(failing) - 1} more)" if len(failing) > 1 else ""
        reason = (
            f"review thread [{t.severity}/{t.blocking}] {where} not FIXED or REBUTTED: {t.permalink}{more}"
        )
    elif carried_findings:
        f = carried_findings[0]
        where = getattr(f, "where", "")
        link = getattr(f, "permalink", "")
        verdict = getattr(f, "verdict", "")
        severity = getattr(f, "severity", "")
        more = f" (+{len(carried_findings) - 1} more)" if len(carried_findings) > 1 else ""
        reason = (
            f"carried review finding [{verdict}/{severity}] {where} not FIXED or REBUTTED: {link}{more}"
        )
    elif debt_error:
        reason = f"debt ledger: {debt_error}"
    elif scope_rc == 1:
        reason = "PR scope exceeds its issue's declared 'Scope limit:' (see [pr-scope] above)"
    else:
        reason = "review-triage exited 1 without a recognized cause"
    return "BLOCKER: " + " ".join(reason.split())  # one line, whatever the cause text holds


def report(
    rc: int,
    failing: Sequence[Thread],
    debt_error: object,
    scope_rc: int,
    *,
    carried_findings: Sequence[object] = (),
) -> int:
    """Print the BLOCKER line (if any) and hand the exit code back unchanged."""
    line = first_blocker(rc, failing, debt_error, scope_rc, carried_findings=carried_findings)
    if line:
        print(line)
    return rc


def unknown(message: str) -> None:
    """Exit-3 reporting: the message on stderr as before, plus one `UNKNOWN:` line on stdout."""
    print(message, file=sys.stderr)
    print(f"UNKNOWN: {message}")
