"""The trunk-verified check: did the current default-branch head actually get verified?

Split out of scripts/ci_health_check.py so that module stays under the 600-line file-size
ratchet. The dependency runs one way only, matching the rule ci_health_core states:

    ci_health_core  <-  ci_health_trunk  <-  ci_health_check

Nothing here reads the iteration-metrics store; it needs the run listing the checker
already fetched plus one jobs call per verdict.

-Claude
"""

from __future__ import annotations

from scripts.ci_health_core import (
    EXIT_OK,
    EXIT_TRUNK_UNVERIFIED,
    CheckResult,
    PreconditionError,
    Run,
    RunJobsPayloadCache,
    fetch_run_jobs,
)

GATING_WORKFLOW_NAME = "CI"


def _check_trunk_verified(
    runs: list[Run],
    default_branch: str,
    job_payloads: RunJobsPayloadCache | None = None,
) -> CheckResult:
    """(f) The current trunk head was actually verified, not merely not-failed.

    Every other check here reasons about failures. This one reasons about ABSENCE, which
    is a different question and the one that bit us: on Mon 31 Aug 2026 six merges landed
    in 25 seconds, cancel-in-progress cancelled each run's predecessor, and main's head was
    left with no completed pytest or frontend job at all. Nothing was red. Nothing alerted.
    A failure-rate check cannot see that, because there was no failure to count.

    It also catches the inverse, which is the case that actually escaped earlier the same
    day: the ratchet job concluded 'failure' on 5f7466d3 inside a run whose own conclusion
    read 'cancelled', so a run-level verdict scored it as not-a-failure and it went unseen
    for hours. Reading per-job conclusions catches both, and they are reported as distinct
    classifications because they need different fixes - one is a red to go and fix, the
    other is a gap to go and re-run.
    """
    trunk = [
        run
        for run in runs
        if run.head_branch == default_branch
        and run.event == "push"
        and run.name == GATING_WORKFLOW_NAME
    ]
    if not trunk:
        # Not a pass. If the gating workflow has been renamed or has stopped running on
        # trunk, that is exactly the blind spot this check exists to refuse to have, so it
        # alarms rather than skipping. A check that goes quiet when its subject disappears
        # is a check that cannot fail.
        return CheckResult(
            check="trunk-verified",
            ok=False,
            classification="insufficient-data",
            detail=(
                f"no completed {GATING_WORKFLOW_NAME!r} push runs on {default_branch} "
                f"in the sample, so trunk verification cannot be confirmed at all"
            ),
            sample_size=0,
            exit_code=EXIT_TRUNK_UNVERIFIED,
            remediation=(
                f"Confirm the gating workflow is still named {GATING_WORKFLOW_NAME!r} and "
                f"still triggers on push to {default_branch}"
            ),
        )

    newest = max(trunk, key=lambda run: run.started_at)
    # Keep the one-argument form when no shared cache was supplied so the check remains a
    # standalone unit. The watchdog supplies the cache and avoids re-fetching a run that
    # failure-rate or metrics already inspected.
    jobs = (
        fetch_run_jobs(newest.run_id)
        if job_payloads is None
        else fetch_run_jobs(newest.run_id, job_payloads)
    )
    sha = newest.head_sha[:8]
    if not jobs:
        raise PreconditionError(f"run {newest.run_id} reports no jobs at all")

    failed = [job for job in jobs if job.failed]
    unexecuted = [job for job in jobs if job.never_executed]

    if failed:
        names = ", ".join(sorted(job.name for job in failed))
        return CheckResult(
            check="trunk-verified",
            ok=False,
            classification="trunk-failed",
            detail=(
                f"{default_branch}@{sha} has {len(failed)} failed job(s) "
                f"in run {newest.run_id} (run conclusion {newest.conclusion!r}): {names}"
            ),
            sample_size=len(jobs),
            exit_code=EXIT_TRUNK_UNVERIFIED,
            remediation=f"gh run view {newest.run_id} --log-failed",
        )

    if unexecuted:
        names = ", ".join(sorted(job.name for job in unexecuted))
        return CheckResult(
            check="trunk-verified",
            ok=False,
            classification="trunk-unverified",
            detail=(
                f"{default_branch}@{sha} has {len(unexecuted)} job(s) that never executed "
                f"in run {newest.run_id}: {names}. Trunk is unverified, not green"
            ),
            sample_size=len(jobs),
            exit_code=EXIT_TRUNK_UNVERIFIED,
            remediation=f"gh run rerun {newest.run_id}",
        )

    return CheckResult(
        check="trunk-verified",
        ok=True,
        classification="healthy",
        detail=(
            f"{default_branch}@{sha} verified: all {len(jobs)} job(s) of run "
            f"{newest.run_id} concluded success or skipped"
        ),
        sample_size=len(jobs),
        exit_code=EXIT_OK,
    )
