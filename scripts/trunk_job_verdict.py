#!/usr/bin/env python3
"""Compute a trunk run's health verdict from PER-JOB conclusions, never from its top line.

THE DEFECT
    A run's top-line conclusion can read 'cancelled' while a job inside it concluded
    FAILURE, and every dashboard renders 'cancelled' as neutral gray rather than as
    "unknown, look inside". Run 33421400044 at 5f7466d3 (Mon 31 Aug 2026) is exactly that:
    the quality ratchet caught the auto-play.ts merge-union regression in real time and
    reported into a channel nobody reads. A per-job sweep of that same 60-run window finds
    FIVE such runs, so the regression was caught five times and buried five times.

    PR #631 (b9a16ab8) made trunk cancellation rarer, which addressed the instance. It did
    not make a cancelled run's contents visible, so any cancellation from any cause - a
    manual cancel, a timeout, a runner eviction, a concurrency rule added later - still
    hides a red identically. This module closes the class.

THREE-VALUED ON PURPOSE
    failure  a job concluded failure or timed_out
    pass     every job reached a conclusive outcome and none failed
    unknown  nothing failed but something never concluded, INCLUDING a run reporting zero
             jobs (how GitHub renders a run cancelled while queued: run 33451709188)

    A tool that cannot say "unknown" reports a verdict it does not have, which is the very
    defect being closed. A cancelled run is not evidence of health; it is the absence of
    evidence. A 'failure' verdict on a run whose top line is NOT 'failure' is BURIED, and
    buried failures are the alert. An ordinary top-line failure is already red everywhere
    and is reported without being alerted on again.

RELEVANCE IS ANCESTRY, NEVER A TIMESTAMP
    Run 33451482070 at a9fc0504 concluded SUCCESS at 23:44:29Z, after #631 merged at
    ~23:41Z. By clock it is "a successful trunk run after the fix"; by ancestry it is
    PRE-fix, because b9a16ab8 is not its ancestor. It merely started earlier and finished
    later. Under a fast trunk, "started after X landed" wears the exact costume of "built a
    tree containing X", so --contains uses `git merge-base --is-ancestor`, and a SHA
    missing from the clone raises rather than degrading to a time comparison.

SURVIVAL, THE DISCRIMINATING TEST FOR THE CONCURRENCY GUARD
    --survival asks whether trunk runs RACED by a later merge outlived it. "Did a post-fix
    run conclude?" is passed by the broken state - pre-fix runs concluded too whenever
    nothing merged fast enough to kill them - so it measures nothing. Only raced runs are
    counted; a run nobody raced never tested the guard.

    Measured over the newest 40 trunk runs AFTER #631 landed: 27 of 32 raced runs were
    cancelled mid-flight. #631 changed cancel-in-progress but never touched the concurrency
    GROUP, and the flag is read from the run that STARTS, not the one being killed. Trunk
    cancellation is therefore still live, and until the real fix lands this per-job verdict
    is the only working instrument on trunk. Nothing here may degrade to "the run
    concluded, therefore fine".

USAGE
    python -m scripts.trunk_job_verdict --limit 60
    python -m scripts.trunk_job_verdict --limit 60 --contains b9a16ab8
    python -m scripts.trunk_job_verdict --survival --limit 40
    python -m scripts.trunk_job_verdict --run-id 33421400044 --json
    python -m scripts.trunk_job_verdict --runs-json fixture.json   # offline, no network

EXIT CODES
    0   nothing to report
    1   a buried failure, or a raced trunk run that was killed
    10  precondition failure (gh missing, unauthenticated, unparseable data, unknown SHA)

    'unknown' NEVER sets a nonzero exit. About 80 percent of trunk runs were cancelled in
    the window this was written against, and paging on each would train the reader to
    ignore the alert, which is the failure mode already being fixed. Unknown runs are
    counted and named in every report instead.

ACCEPTANCE CRITERIA
    Each one is a named test in tests/scripts/test_trunk_job_verdict.py, run against REAL
    captured runs in tests/fixtures/trunk_job_verdict_real_runs.json rather than synthetic
    fixtures, and guarded by a mutation test asserting that a top-line verdict would MISS
    the real buried failure. Merge blocking is out of scope: branch protection is
    unavailable on this repo's plan (403, "protected": false, verified twice Mon 31 Aug
    2026), so prevention cannot be delivered and AUDITABLE IS THE CEILING - optimize for a
    stranger finding the breach in the record later.

-Claude
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from scripts.ci_health_core import REPO, PreconditionError
from scripts.trunk_job_verdict_core import (
    DEFAULT_BRANCH,
    DEFAULT_LIMIT,
    DEFAULT_WORKFLOW,
    EXIT_BURIED_FAILURE,
    EXIT_OK,
    EXIT_PRECONDITION,
    VERDICT_PASS,
    VERDICT_UNKNOWN,
    RunOutcome,
    RunVerdict,
    SurvivalOutcome,
    assess_survival,
    classify_run,
    fetch_recent_runs,
    fetch_run,
    is_built_on,
    load_runs_json,
)

# ----- reporting ---------------------------------------------------------------------


def render_markdown(verdicts: list[RunVerdict], scope: str) -> str:
    """The audit record. Written so a stranger can reconstruct the breach from it alone."""
    buried = [item for item in verdicts if item.is_buried]
    unknown = [item for item in verdicts if item.verdict == VERDICT_UNKNOWN]
    passed = [item for item in verdicts if item.verdict == VERDICT_PASS]

    headline = "buried CI failure" if buried else "no buried CI failure"
    lines = [
        f"# Trunk job verdict: {headline}",
        "",
        f"- Scope: {scope}",
        f"- Runs examined: **{len(verdicts)}**",
        f"- Buried failures: **{len(buried)}** "
        "(a job concluded failure inside a run that does not read as failed)",
        f"- Verdict pass: **{len(passed)}**, verdict unknown: **{len(unknown)}**",
        "",
        "Verdicts are computed from PER-JOB conclusions. A run's top-line conclusion is "
        "reported only to show where it disagrees with its own jobs.",
        "",
    ]

    if buried:
        lines.extend(
            [
                "## Buried failures",
                "",
                "| Run | Commit | Run reads | Failing job |",
                "| --- | --- | --- | --- |",
            ]
        )
        for item in buried:
            run_cell = f"[{item.run_id}]({item.html_url})" if item.html_url else str(item.run_id)
            jobs = ", ".join(item.failing_jobs).replace("|", "\\|")
            lines.append(
                f"| {run_cell} | `{item.head_sha[:8]}` | `{item.run_conclusion}` | {jobs} |"
            )
        lines.append("")

    if unknown:
        lines.extend(
            [
                f"## Unknown ({len(unknown)})",
                "",
                "Not evidence of health and not evidence of breakage. These runs were "
                "cancelled or never concluded, so nothing was verified on those commits.",
                "",
            ]
        )
        lines.extend(
            f"- `{item.head_sha[:8]}` run {item.run_id}: {item.summary}" for item in unknown
        )
        lines.append("")

    lines.extend(
        [
            "Reproduce: `python -m scripts.trunk_job_verdict --limit 60`",
            "",
        ]
    )
    return "\n".join(lines)


def _write_github_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT", "")
    if path:
        with Path(path).open("a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")


def _write_step_summary(report: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if path:
        with Path(path).open("a", encoding="utf-8") as handle:
            handle.write(report)


def _emit_json(verdicts: list[RunVerdict], scope: str, exit_code: int) -> None:
    buried = [item for item in verdicts if item.is_buried]
    print(
        json.dumps(
            {
                "repo": REPO,
                "scope": scope,
                "runs_examined": len(verdicts),
                "buried_failures": len(buried),
                "exit_code": exit_code,
                "verdicts": [
                    {
                        "run_id": item.run_id,
                        "head_sha": item.head_sha,
                        "run_conclusion": item.run_conclusion,
                        "verdict": item.verdict,
                        "is_buried": item.is_buried,
                        "failing_jobs": list(item.failing_jobs),
                        "inconclusive_jobs": list(item.inconclusive_jobs),
                        "job_count": item.job_count,
                    }
                    for item in verdicts
                ],
            },
            indent=2,
        )
    )


def _emit_survival(outcomes: list[SurvivalOutcome], scope: str) -> int:
    """Report the survival rate of raced trunk runs, and fail when any was killed."""
    if not outcomes:
        print(f"[INFO] scope: {scope}")
        print(
            "[OK] no trunk run in this window was raced by a later merge, so this window "
            "is not a test of the concurrency guard - widen it before drawing a conclusion"
        )
        return EXIT_OK

    killed = [item for item in outcomes if not item.survived]
    for item in outcomes:
        token = "[OK]" if item.survived else "[ERROR]"
        verb = "survived" if item.survived else "was KILLED by"
        print(
            f"{token} {item.head_sha[:8]} run {item.run_id}: {verb} "
            f"{len(item.overlapped_by)} merge(s) landing mid-flight "
            f"(reads '{item.run_conclusion}')"
        )
    print(f"[INFO] scope: {scope}")
    survived = len(outcomes) - len(killed)
    print(f"[INFO] denominator: {len(outcomes)} raced runs, not the whole window")
    if killed:
        print(
            f"[ERROR] {len(killed)} of {len(outcomes)} raced trunk runs were cancelled "
            "mid-flight - the concurrency guard is NOT holding"
        )
        return EXIT_BURIED_FAILURE
    print(f"[OK] all {survived} raced trunk runs survived - the concurrency guard holds")
    return EXIT_OK


def _emit_text(verdicts: list[RunVerdict], scope: str) -> None:
    for item in verdicts:
        token = "[ERROR]" if item.is_buried else "[OK]"
        print(f"{token} {item.head_sha[:8]} run {item.run_id}: {item.summary}")
    buried = sum(1 for item in verdicts if item.is_buried)
    unknown = sum(1 for item in verdicts if item.verdict == VERDICT_UNKNOWN)
    print(f"[INFO] scope: {scope}")
    if buried:
        print(f"[ERROR] {buried} of {len(verdicts)} runs hide a failed job behind a non-failed run")
        return
    print(f"[OK] no buried failure in {len(verdicts)} runs ({unknown} unknown, nothing verified)")


# ----- orchestration -----------------------------------------------------------------


def _load_runs(args: argparse.Namespace) -> tuple[list[RunOutcome], str]:
    if args.runs_json:
        return load_runs_json(args.runs_json), f"fixture {args.runs_json}"
    if args.run_id:
        return [fetch_run(args.run_id)], f"run {args.run_id}"
    runs = fetch_recent_runs(args.workflow, args.branch, args.limit, with_jobs=not args.survival)
    return runs, f"newest {len(runs)} completed {args.workflow} runs on {args.branch}"


def _apply_contains(
    runs: list[RunOutcome], base_sha: str, scope: str
) -> tuple[list[RunOutcome], str]:
    kept = [run for run in runs if is_built_on(run.head_sha, base_sha)]
    return kept, f"{scope}, restricted to trees containing {base_sha[:8]} (ancestry, not clock)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compute a trunk run's health verdict from per-job conclusions."
    )
    parser.add_argument("--run-id", type=int, help="classify one run by id")
    parser.add_argument("--workflow", default=DEFAULT_WORKFLOW)
    parser.add_argument("--branch", default=DEFAULT_BRANCH)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument(
        "--contains",
        help=(
            "keep only runs whose commit has this SHA as an ancestor. Ancestry, never a "
            "timestamp: a run that merely started later may predate the commit entirely."
        ),
    )
    parser.add_argument(
        "--survival",
        action="store_true",
        help=(
            "report whether OVERLAPPED trunk runs outlived the next merge landing on top "
            "of them. The discriminating test for the concurrency guard: 'did a run "
            "conclude' is passed by the broken state, 'did a raced run survive' is not."
        ),
    )
    parser.add_argument("--runs-json", type=Path, help="read runs from a file instead of the API")
    parser.add_argument("--report-file", type=Path, help="write the markdown report here")
    parser.add_argument("--json", action="store_true", help="emit machine-readable results")
    args = parser.parse_args(argv)

    try:
        runs, scope = _load_runs(args)
        if args.contains:
            runs, scope = _apply_contains(runs, args.contains, scope)
    except PreconditionError as exc:
        # Still emit a zero count. The caller keys its alerting off this output, and an
        # UNSET output reads as truthy in a GitHub Actions `!= '0'` expression, which would
        # try to file an issue from a report that was never written. The job still fails
        # loudly with EXIT_PRECONDITION; it just does not fabricate a buried failure.
        _write_github_output("buried_failures", "0")
        print(f"[ERROR] precondition: {exc}")
        return EXIT_PRECONDITION

    if args.survival:
        return _emit_survival(assess_survival(runs), scope)

    verdicts = [classify_run(run) for run in runs]
    buried = sum(1 for item in verdicts if item.is_buried)
    exit_code = EXIT_BURIED_FAILURE if buried else EXIT_OK

    report = render_markdown(verdicts, scope)
    if args.report_file:
        args.report_file.write_text(report, encoding="utf-8")
    _write_step_summary(report)
    _write_github_output("buried_failures", str(buried))
    _write_github_output("report_file", str(args.report_file) if args.report_file else "")

    if args.json:
        _emit_json(verdicts, scope, exit_code)
    else:
        _emit_text(verdicts, scope)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
