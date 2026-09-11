"""Conservatively suppress non-blocking findings already in a PR debt file.

The reviewer must not re-open a finding the PR explicitly recorded as debt.
Matching is deliberately title-only after case, punctuation, and whitespace
normalization. Similar prose is not enough: an uncertain match stays open and
posts. P0/P1 never suppress, even if a malformed debt ledger records them.
"""

from __future__ import annotations

from scripts.review_lane import Finding, withheld


def add_suppressed_summary(summary: str, suppressed: list[Finding], marker: str) -> str:
    """Make every deliberate suppression visible in the submitted review."""
    if not suppressed:
        return summary
    names = "\n".join(f"- already debt-logged: {withheld(finding.title)}" for finding in suppressed)
    replacement = f"\n\nAlready debt-logged, not re-filed:\n{names}\n\n{marker}"
    return summary.replace(f"\n{marker}", replacement)
