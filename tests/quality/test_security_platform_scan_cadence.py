"""The full Semgrep AppSec Platform scan of main runs weekly, not daily.

Reads the shipped workflow, not a second hand-maintained representation.
ADR-NEW-semgrep-full-scan-weekly: measured Tue 22 Sep 2026, the daily full scan
was cancelled at its timeout on every run that got a runner (4 of 4), 16 billed
hosted minutes a day for zero uploads.

Regression lines:
  - if the daily cron reaches the semgrep job again, then broken (7x the hosted
    minutes for a scan that does not finish)
  - if the weekly cron no longer reaches the semgrep job, then broken (no full
    scan of main at all; PR scans are diff-aware only)
  - if a manual dispatch with cadence=weekly cannot reach it, then broken (the
    path becomes untestable without waiting for Monday)
  - if the daily cron is dropped from the workflow, then broken (this test would
    be proving the absence of a trigger that no longer exists)
"""

from __future__ import annotations

from pathlib import Path

import yaml

from tests.ci_cost_guard_workflow_reader import top_level_disjuncts

REPO = Path(__file__).resolve().parents[2]
SECURITY = REPO / ".github" / "workflows" / "security.yml"

DAILY_CRON = "17 6 * * *"
WEEKLY_CRON = "37 6 * * 1"
WEEKLY_ONLY = f"github.event.schedule == '{WEEKLY_CRON}'"
WEEKLY_DISPATCH = (
    "(github.event_name == 'workflow_dispatch' && inputs.cadence == 'weekly' "
    "&& github.ref == 'refs/heads/main')"
)
PR_CONJUNCTION_PREFIX = (
    "(vars.CI_HOSTED_SECURITY_JOBS == 'true' && github.event_name == 'pull_request' &&"
)


def _workflow() -> dict:
    assert SECURITY.is_file(), "security workflow is missing"
    document = yaml.safe_load(SECURITY.read_text(encoding="utf-8"))
    assert isinstance(document, dict), "security.yml is not a mapping"
    return document


def _semgrep_disjuncts(workflow: dict) -> list[str]:
    job = workflow["jobs"]["semgrep"]
    assert "if" in job, "semgrep job has no if field, so every trigger reaches it"
    return top_level_disjuncts(str(job["if"]))


def test_workflow_still_declares_both_crons() -> None:
    """CONTROL: the assertions below are about which cron reaches the job, which
    only means something while both crons exist on the workflow."""
    on = _workflow()[True]
    crons = {entry["cron"] for entry in on["schedule"]}
    assert {DAILY_CRON, WEEKLY_CRON} <= crons, f"expected both crons, got {sorted(crons)}"


def test_daily_cron_does_not_reach_the_platform_full_scan() -> None:
    """Allowlist, not denylist: every disjunct must be one of the three shapes that
    cannot admit the daily cron. A bare `github.event_name == 'schedule'`, a negated
    weekly check, `github.ref == 'refs/heads/main'` or `always()` would each let the
    daily cron through, and none of them contains the daily cron string (Claude review
    P3 x2 on #3787)."""
    disjuncts = _semgrep_disjuncts(_workflow())

    def _cannot_admit_the_daily_cron(disjunct: str) -> bool:
        # Exact strings for the two scheduled/manual shapes; a PREFIX for the PR
        # conjunction (its tail is the PR-scan policy, owned by other tests), so a
        # negation cannot wrap the pull_request equality. A nested `||` or an
        # `always()` anywhere is refused outright.
        if "||" in disjunct or "always()" in disjunct:
            return False
        if disjunct in (WEEKLY_ONLY, WEEKLY_DISPATCH):
            return True
        return disjunct.startswith(PR_CONJUNCTION_PREFIX)

    admits = [d for d in disjuncts if not _cannot_admit_the_daily_cron(d)]
    assert not admits, (
        "a disjunct is not one of the three shapes that exclude the schedule event, so "
        "the daily cron reaches the semgrep job again; the full scan of main never "
        f"finished inside its timeout and billed 16 hosted minutes a day: {admits}"
    )


def test_weekly_cron_reaches_the_platform_full_scan() -> None:
    disjuncts = _semgrep_disjuncts(_workflow())
    assert WEEKLY_ONLY in disjuncts, (
        f"no disjunct admits the weekly cron on its own; got {disjuncts}"
    )


def test_manual_weekly_dispatch_reaches_the_platform_full_scan() -> None:
    """The manual path is pinned to main: a full scan of any other ref would bill the
    same 16 minutes against a branch the platform does not track."""
    workflow = _workflow()
    disjuncts = _semgrep_disjuncts(workflow)
    assert WEEKLY_DISPATCH in disjuncts, (
        f"no disjunct admits workflow_dispatch with cadence=weekly on main only; got {disjuncts}"
    )
    # The disjunct reads `inputs.cadence`; if that input is renamed or loses the
    # `weekly` option, the expression is empty and the manual path dies silently.
    cadence = workflow[True]["workflow_dispatch"]["inputs"].get("cadence")
    assert cadence is not None, "workflow_dispatch declares no `cadence` input"
    assert "weekly" in cadence.get("options", []), (
        f"`cadence` input does not offer `weekly`; options={cadence.get('options')}"
    )


def test_daily_dependency_scan_is_unchanged() -> None:
    """The osv daily scan keeps its cron: only the platform full scan moved."""
    job = _workflow()["jobs"]["scheduled"]
    assert " ".join(str(job["if"]).split()) == "github.event_name != 'pull_request'"
