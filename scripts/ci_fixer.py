#!/usr/bin/env python3
"""ci-fixer lane: propose diffs for failed self-hosted CI runs as PR comments.

Design (#1017): on every failed self-hosted job on an open PR, spawn ONE bounded
codex worker with the job's log excerpt, the PR diff, and the failing step name,
in a throwaway worktree. THIS LANE NEVER PUSHES, never re-runs a gate, never
edits a workflow/ratchet/budget file. If the worker's diff reproduces green
locally within the time cap, post ONE PR comment (root cause, diff, `git apply`
command) plus the `ci-fix:proposed` label. If it can't go green in the cap, or
its diff touches a guarded path, or the failure matches a KNOWN-UNFIXABLE
signature (ci_fixer_core, #1029 is the live case), post diagnosis-only and
propose nothing. The PR's owning worker or the merge lane decides whether to
apply, adapt, or reject.

Usage
    python -m scripts.ci_fixer --poll                    # live: gh + a real codex worker
    python -m scripts.ci_fixer --poll --dry-run           # classify/render only, no worker/post
    python -m scripts.ci_fixer --jobs-json fixture.json --dry-run   # fully offline
    python -m scripts.ci_fixer --kpi-report               # derive KPIs from recorded events

Exit codes: 0 ran to completion; 10 precondition failure (gh missing,
unauthenticated, unparseable data).

-Claude
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

try:
    from scripts import ci_fixer_core as cf
except ModuleNotFoundError as exc:
    if exc.name == "scripts":
        raise SystemExit("uv run --no-sync python -m scripts.ci_fixer") from None
    raise
from scripts.ci_fixer_sandbox import run_recheck
from scripts.ci_health_core import REPO, PreconditionError, _gh_api_json, _run_gh

# ----- configuration -----------------------------------------------------------------

CODEX_BIN = "codex"
DEFAULT_MODEL = "gpt-5.6-terra"
LOG_EXCERPT_MAX_CHARS = 20_000
WORKTREE_ROOT = Path("../ci-fixer-wt")
WORKER_TRANSCRIPT_FILENAME = ".ci-fixer-last-message.txt"

ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
LOG_MESSAGE = re.compile(r"^.*?\d{4}-\d{2}-\d{2}T[\d:.]+Z (.*)$")

# ----- gh: discovery -------------------------------------------------------------------


def list_open_prs() -> list[dict]:
    payload = json.loads(
        _run_gh(["pr", "list", "--state", "open", "--json", "number,headRefName,headRefOid"])
    )
    if not isinstance(payload, list):
        raise PreconditionError("gh pr list returned a non-list payload")
    return payload


def fetch_failed_jobs_for_pr(pr_number: int, head_branch: str, head_sha: str) -> list[cf.FailedJob]:
    """One representative FailedJob per failed run on this PR's branch.

    A run with several failing self-hosted jobs still yields ONE FailedJob (the
    first found): #1017 dedupes fixers by run id, so the collapse happens here too.
    """
    listing = json.loads(
        _run_gh(
            [
                "run",
                "list",
                "--branch",
                head_branch,
                "--commit",
                head_sha,
                "--status",
                "failure",
                "--json",
                "databaseId,workflowName,headSha,url,createdAt",
                "--limit",
                "20",
            ]
        )
    )
    if not isinstance(listing, list):
        raise PreconditionError("gh run list returned a non-list payload")

    failed: list[cf.FailedJob] = []
    for run in listing:
        run_id = int(run["databaseId"])
        jobs_payload = _gh_api_json(f"repos/{REPO}/actions/runs/{run_id}/jobs?per_page=100")
        if not isinstance(jobs_payload, dict) or "jobs" not in jobs_payload:
            raise PreconditionError(f"actions/runs/{run_id}/jobs has no jobs key")
        for job in jobs_payload["jobs"]:
            if job.get("conclusion") != "failure":
                continue
            runner_name = str(job.get("runner_name") or "")
            if runner_name not in cf.LIVE_RUNNERS:
                # Not one of the self-hosted runners actually taking traffic
                # (issue #1017's live context) -- out of scope for this lane.
                continue
            failed.append(
                cf.FailedJob(
                    run_id=run_id,
                    job_id=int(job["id"]),
                    job_name=str(job["name"]),
                    pr_number=pr_number,
                    head_sha=str(run.get("headSha") or ""),
                    runner_name=runner_name,
                    workflow=str(run.get("workflowName") or ""),
                    html_url=str(job.get("html_url") or run.get("url") or ""),
                    log_excerpt="",
                    created_at=str(run.get("createdAt") or ""),
                )
            )
            break  # one representative job per run; see docstring
    return failed


def fetch_failed_job_log(job: cf.FailedJob) -> str:
    return _run_gh(
        ["run", "view", str(job.run_id), "--job", str(job.job_id), "--log-failed"]
    )


def fetch_pr_diff(pr_number: int) -> str:
    return _run_gh(["pr", "diff", str(pr_number)])


def fetch_pr_head_sha(pr_number: int) -> str:
    payload = json.loads(_run_gh(["pr", "view", str(pr_number), "--json", "headRefOid"]))
    head_sha = payload.get("headRefOid") if isinstance(payload, dict) else None
    if not isinstance(head_sha, str) or not head_sha:
        raise PreconditionError(f"PR #{pr_number} has no readable head SHA")
    return head_sha


# ----- worker: worktree + bounded codex run --------------------------------------------


def _build_prompt(job: cf.FailedJob, log_excerpt: str, pr_diff: str) -> str:
    guarded = ", ".join(cf.GUARDED_PATH_PREFIXES)
    return f"""You are a bounded CI-fixer worker. A self-hosted CI job failed on an
open pull request. Your ONLY task: make the failing check pass locally, with
the smallest correct diff.

Failing job: {job.job_name}
Workflow: {job.workflow}
Run: {job.html_url}
Runner: {job.runner_name}

Hard rules, no exceptions:
- Never edit any path under: {guarded}
- Never touch a file whose name contains "ratchet", "baseline", or "budget".
- Never weaken, delete, or skip a test, gate, or threshold to make this pass.
- Never invent or fabricate data (a commit SHA, a provenance record, a
  timestamp) to satisfy an assertion. If the true fix needs data this clone
  does not have, stop and explain that instead of guessing.
- Do not commit, do not push, do not touch git remotes.
- Make ONLY the change needed to fix this failure. No unrelated refactors.

Failed job log (tail):
```
{log_excerpt}
```

PR diff so far, for context on what is already in flight:
```diff
{pr_diff[:LOG_EXCERPT_MAX_CHARS]}
```

When you believe the check now passes, stop. Your final message must open
with a two-line root cause summary.
"""


class WorkerResult:
    def __init__(self, completed: bool, last_message: str) -> None:
        self.completed = completed
        self.last_message = last_message


def spawn_worker(
    worktree_dir: Path,
    prompt: str,
    model: str = DEFAULT_MODEL,
    timeout_s: int = cf.WORKER_TIMEOUT_SECONDS,
) -> WorkerResult:
    """Run ONE bounded, non-interactive codex worker inside `worktree_dir`.

    Real subprocess, real timeout, no mock: a worker that can't finish in
    `timeout_s` is reported not-completed, and the caller falls back to diagnosis-only.
    """
    if shutil.which(CODEX_BIN) is None:
        raise PreconditionError(f"{CODEX_BIN} CLI not found on PATH")
    descriptor, transcript_name = tempfile.mkstemp(prefix="ci-fixer-worker-", suffix=".txt")
    os.close(descriptor)
    last_message_path = Path(transcript_name)
    args = [
        CODEX_BIN,
        "exec",
        "-C",
        str(worktree_dir),
        "--sandbox",
        "workspace-write",
        "-c",
        "approval_policy=never",
        "-m",
        model,
        "-o",
        str(last_message_path),
        prompt,
    ]
    try:
        try:
            subprocess.run(args, capture_output=True, text=True, timeout=timeout_s, check=False)
            completed = True
        except subprocess.TimeoutExpired:
            completed = False
        last_message = ""
        if last_message_path.exists():
            last_message = last_message_path.read_text(encoding="utf-8")
        return WorkerResult(completed=completed, last_message=last_message)
    finally:
        last_message_path.unlink(missing_ok=True)


def create_worktree(job: cf.FailedJob) -> Path:
    dest = WORKTREE_ROOT / f"run-{job.run_id}"
    branch = worktree_branch_name(job.run_id)
    subprocess.run(
        ["git", "worktree", "add", str(dest), "-b", branch, job.head_sha],
        capture_output=True,
        text=True,
        check=True,
    )
    return dest


def worktree_branch_name(run_id: int) -> str:
    return f"ci-fixer/run-{run_id}"


def remove_worktree(worktree_dir: Path, branch: str) -> None:
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(worktree_dir)],
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        ["git", "branch", "-D", branch],
        capture_output=True,
        text=True,
        check=True,
    )


def capture_diff(worktree_dir: Path) -> str:
    untracked = subprocess.run(
        ["git", "-C", str(worktree_dir), "ls-files", "--others", "--exclude-standard", "-z"],
        capture_output=True,
        check=True,
    ).stdout.split(b"\0")
    paths = [
        path.decode("utf-8")
        for path in untracked
        if path and path.decode("utf-8") != WORKER_TRANSCRIPT_FILENAME
    ]
    if paths:
        subprocess.run(
            ["git", "-C", str(worktree_dir), "add", "--intent-to-add", "--", *paths],
            capture_output=True,
            text=True,
            check=True,
        )
    completed = subprocess.run(
        ["git", "-C", str(worktree_dir), "diff", "--binary"],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout


def _log_message(line: str) -> str:
    clean = ANSI_ESCAPE.sub("", line).rstrip()
    match = LOG_MESSAGE.match(clean)
    return match.group(1) if match is not None else clean


def extract_failed_step_command(raw_log: str) -> list[str] | None:
    """Return the exact shell-free command from the failed Actions step.

    GitHub emits the command after ``##[group]Run`` and then echoes its
    continuation lines. The last group before the failing exit is the command
    that failed, rather than a smaller command inferred from pytest output.
    """
    commands: list[list[str]] = []
    lines = [_log_message(line) for line in raw_log.splitlines()]
    index = 0
    while index < len(lines):
        marker = "##[group]Run "
        if not lines[index].startswith(marker):
            index += 1
            continue
        parts = [lines[index][len(marker) :]]
        index += 1
        while index < len(lines) and not lines[index].startswith("shell:"):
            message = lines[index].strip()
            if len(parts) == 1 and message == parts[0].strip():
                index += 1
                continue
            if message and not message.startswith("env:") and not re.match(
                r"^[A-Za-z_][A-Za-z0-9_]*=", message
            ):
                parts.append(message)
            index += 1
        command_text = " ".join(parts).replace("\\", " ")
        try:
            command = shlex.split(command_text)
        except ValueError:
            continue
        is_python_pytest = command[:3] in (["python", "-m", "pytest"], ["python3", "-m", "pytest"])
        if command and (command[0] in {"pytest", "ruff", "pnpm", "uv"} or is_python_pytest):
            commands.append(command)
    return commands[-1] if commands else None


def find_recheck_working_directory(command: list[str], worktree_dir: Path) -> Path:
    """Find the unique step-level Actions working directory for `command`."""
    command_text = " ".join(command)
    candidates: set[Path] = set()
    for workflow_path in (worktree_dir / ".github" / "workflows").glob("*.y*ml"):
        lines = workflow_path.read_text(encoding="utf-8").splitlines()
        step_directory: Path | None = None
        for index, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("- "):
                step_directory = None
            if stripped.startswith("working-directory:"):
                value = stripped.split(":", maxsplit=1)[1].strip().strip("\"'")
                step_directory = Path(value)
            if not stripped.startswith("run:"):
                continue
            run_indent = len(line) - len(line.lstrip())
            run_lines = [stripped.split(":", maxsplit=1)[1].strip()]
            for following in lines[index + 1 :]:
                if following.strip() and len(following) - len(following.lstrip()) <= run_indent:
                    break
                run_lines.append(following.strip())
            if command_text not in " ".join(run_lines).replace("\\", " "):
                continue
            candidates.add(step_directory or Path("."))
    if len(candidates) > 1:
        locations = ", ".join(str(path) for path in sorted(candidates))
        raise PreconditionError(f"failed command has ambiguous working directories: {locations}")
    return candidates.pop() if candidates else Path(".")


def _root_cause_summary(last_message: str) -> str:
    lines = [line for line in last_message.strip().splitlines() if line.strip()]
    return " ".join(lines[:2]) if lines else "worker produced no summary"


# ----- one job, start to finish ---------------------------------------------------------


def _diagnosis_outcome(job: cf.FailedJob, reason: str) -> cf.FixOutcome:
    body = cf.render_diagnosis_comment(job, reason)
    return cf.FixOutcome(job=job, disposition="diagnosis-only", reason=reason, comment_body=body)


def _handle_worker_job(
    job: cf.FailedJob,
    raw_log: str,
    model: str,
    pr_diff: str,
) -> cf.FixOutcome:
    worktree_dir = create_worktree(job)
    branch = worktree_branch_name(job.run_id)
    try:
        prompt = _build_prompt(job, job.log_excerpt, pr_diff)
        result = spawn_worker(worktree_dir, prompt, model=model)
        diff_text = capture_diff(worktree_dir)

        cap_minutes = cf.WORKER_TIMEOUT_SECONDS // 60
        if not result.completed:
            return _diagnosis_outcome(job, f"worker exceeded the {cap_minutes}-minute cap")

        guarded_hits = cf.diff_touches_guarded_paths(diff_text)
        if guarded_hits:
            hits = ", ".join(guarded_hits)
            return _diagnosis_outcome(job, f"worker's diff touched guarded path(s): {hits}")

        if not diff_text.strip():
            return _diagnosis_outcome(job, "worker produced no changes")

        recheck = extract_failed_step_command(raw_log)
        root_cause = _root_cause_summary(result.last_message)
        if recheck is None:
            return _diagnosis_outcome(
                job, f"{root_cause} (could not capture the failed CI step command to verify green)"
            )

        try:
            working_directory = find_recheck_working_directory(recheck, worktree_dir)
            verify = run_recheck(recheck, worktree_dir, working_directory)
        except subprocess.TimeoutExpired:
            cap_minutes = cf.RECHECK_TIMEOUT_SECONDS // 60
            return _diagnosis_outcome(
                job, f"{root_cause} (recheck exceeded the {cap_minutes}-minute cap)"
            )
        if verify.returncode != 0:
            recheck_cmd = " ".join(recheck)
            return _diagnosis_outcome(job, f"{root_cause} (recheck still failed: `{recheck_cmd}`)")

        body = cf.render_fix_comment(job, root_cause, diff_text)
        return cf.FixOutcome(
            job=job,
            disposition="fix-proposed",
            reason=root_cause,
            diff_text=diff_text,
            comment_body=body,
        )
    finally:
        remove_worktree(worktree_dir, branch)


def handle_job(
    job: cf.FailedJob,
    dry_run: bool,
    model: str = DEFAULT_MODEL,
    pr_diff: str | None = None,
) -> cf.FixOutcome:
    raw_log = job.log_excerpt or fetch_failed_job_log(job)
    log_excerpt = raw_log[-LOG_EXCERPT_MAX_CHARS:]
    job = dataclasses.replace(job, log_excerpt=log_excerpt)

    reason = cf.classify_known_unfixable(log_excerpt)
    if reason is not None:
        body = cf.render_known_unfixable_comment(job, reason)
        return cf.FixOutcome(
            job=job, disposition="known-unfixable", reason=reason, comment_body=body
        )
    if dry_run:
        return _diagnosis_outcome(job, "dry-run: worker not spawned")
    if pr_diff is None:
        pr_diff = fetch_pr_diff(job.pr_number)
    return _handle_worker_job(job, raw_log, model, pr_diff)


def post_outcome(outcome: cf.FixOutcome) -> None:
    if outcome.disposition == "fix-proposed":
        current_head = fetch_pr_head_sha(outcome.job.pr_number)
        if current_head != outcome.job.head_sha:
            raise PreconditionError(
                f"PR #{outcome.job.pr_number} advanced from {outcome.job.head_sha} "
                f"to {current_head}; discarding stale verified proposal"
            )
    marker = cf.outcome_marker(outcome.job)
    comments = _run_gh(
        [
            "api",
            "--paginate",
            f"repos/{cf.REPO}/issues/{outcome.job.pr_number}/comments?per_page=100",
        ]
    )
    comment_file = Path(f".ci-fixer-comment-{outcome.job.run_id}.md")
    comment_file.write_text(outcome.comment_body, encoding="utf-8")
    try:
        if marker not in comments:
            _run_gh(["pr", "comment", str(outcome.job.pr_number), "-F", str(comment_file)])
        if outcome.disposition == "fix-proposed":
            _run_gh(
                ["pr", "edit", str(outcome.job.pr_number), "--add-label", cf.CI_FIX_LABEL]
            )
    finally:
        comment_file.unlink(missing_ok=True)


# ----- orchestration ---------------------------------------------------------------------


def poll(dry_run: bool, jobs_json: Path | None, model: str = DEFAULT_MODEL) -> list[cf.FixOutcome]:
    # Read cf.LEDGER_PATH / cf.KPI_EVENTS_PATH at CALL time, not as bound defaults:
    # a Python default argument is evaluated once at function-definition time, so
    # tests overriding the module attribute would otherwise silently write past the
    # override straight to the real ledger.
    outcomes: list[cf.FixOutcome] = []

    if jobs_json is not None:
        raw = json.loads(jobs_json.read_text(encoding="utf-8"))
        candidates = [cf.FailedJob(**item) for item in raw]
    else:
        candidates = []
        for pr in list_open_prs():
            candidates.extend(
                fetch_failed_jobs_for_pr(pr["number"], pr["headRefName"], pr["headRefOid"])
            )

    for job in candidates:
        claim = cf.claim_job(job)
        if claim == "seen":
            continue
        if claim == "capacity-reached":
            break
        try:
            outcome = handle_job(job, dry_run=dry_run, model=model)
            if not dry_run:
                post_outcome(outcome)
            cf.append_kpi_event(outcome)
            cf.finalize_outcome(outcome)
            outcomes.append(outcome)
        except BaseException:
            cf.release_claim(job)
            raise

    return outcomes


def _print_outcome(outcome: cf.FixOutcome) -> None:
    token = {"fix-proposed": "[FIX]", "diagnosis-only": "[DIAG]", "known-unfixable": "[DECLINE]"}[
        outcome.disposition
    ]
    print(f"{token} run {outcome.job.run_id} (PR #{outcome.job.pr_number}): {outcome.reason}")


def _print_kpi_report() -> None:
    report = cf.build_kpi_report(cf.load_kpi_events(cf.KPI_EVENTS_PATH))
    print(f"[INFO] total examined: {report.total_events}")
    print(f"[INFO] fixes proposed: {report.fixes_proposed}")
    print(f"[INFO] diagnosis only: {report.diagnosis_only}")
    print(f"[INFO] known-unfixable (environment/history class): {report.known_unfixable}")
    share = report.runner_environment_share
    print(
        "[INFO] runner/environment share: "
        + (f"{share:.0%}" if share is not None else "undefined (no events yet)")
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--poll", action="store_true", help="scan open PRs for new failures")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="detect/classify/render only; never spawns a worker, never posts to gh",
    )
    parser.add_argument(
        "--jobs-json", type=Path, help="read candidate failed jobs from a file instead of gh"
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--kpi-report", action="store_true", help="print derived KPIs and exit")
    args = parser.parse_args(argv)

    if args.kpi_report:
        _print_kpi_report()
        return 0

    if not args.poll and not args.jobs_json:
        parser.error("pass --poll (live) or --jobs-json (offline)")

    try:
        outcomes = poll(dry_run=args.dry_run, jobs_json=args.jobs_json, model=args.model)
    except PreconditionError as exc:
        print(f"[ERROR] precondition: {exc}")
        return 10

    for outcome in outcomes:
        _print_outcome(outcome)
    if not outcomes:
        print("[OK] no new self-hosted failures to examine")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
