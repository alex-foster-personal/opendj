#!/usr/bin/env python3
"""Alarm a main push e2e gate that has sat queued with no runner, and re-run it ONCE.

Issue #5831 (W46). Wed 7 Oct 2026, main 2411a8f23190's job "e2e gate (webkit artifact +
hermetic chromium)" (run 37620689059) sat QUEUED from 12:24:31Z for about 1h45m while all
4 `e2e` runners took newer PR jobs, so the trunk checkpoint judged `pending=e2e
verdict:missing` and nothing told anyone. e2e.yml routes a main push's gate to the
opt-in CI_RUNS_ON_E2E_TRUNK lane (E2ELANE-01); this watchdog is the backstop for when
that lane is off, offline, or the dispatcher orphans the job anyway.

Run by .github/workflows/e2e-main-starvation.yml every 10 minutes:

    python -m scripts.ci_e2e_main_starvation --repo owner/name            # act
    python -m scripts.ci_e2e_main_starvation --repo owner/name --dry-run  # report only

For every E2E run on a main push that is still queued or in progress, it reads the run's
jobs and finds the gate job queued with no runner assigned. Past the threshold:

    the run is main's tip, attempt 1    -> alarm, cancel, re-run once (RERUN)
    the run is main's tip, attempt >= 2 -> alarm only; the one auto-rerun is spent (ALARM)
    main has moved on                   -> alarm, cancel, no re-run (SUPERSEDED): the newer
                                           push run waits behind it in e2e.yml's `push-tip`
                                           concurrency group, and cancelling lets it start

The sweep acts first, then alarms with what actually happened. The alarm is one open
issue labelled `ci:e2e-main-starved` (opened, or commented on), a
`::error` annotation, and exit 1 so the watchdog run itself reads red. A job already
reported on that issue is not reported again, so a starved attempt-2 job does not comment
every tick.

Exit codes: 0 measured and nothing starved, 1 at least one starved gate (alarm fired),
2 UNKNOWN (a listing failed or was malformed; never read as "nothing starved").

MINI-PRD
    R1 Starvation detection .......................................... done + ran + regression
       [if] a main push E2E run's gate job is queued, has no runner, and was created more
            than 30 min ago [then] it is starved [else stop]
       [if] the gate job has a runner, is running, or is younger than the threshold
            [then] it is not starved [else stop]
       [if] the run is a PR, a schedule, or a non-main push [then] it is never looked at
            [else stop]
    R2 One auto-rerun ................................................ done + ran + regression
       [if] the starved run is main's tip on attempt 1 [then] cancel, wait for it to
            complete, and re-run it [else stop]
       [if] it is on attempt 2 or later [then] alarm only, never a second re-run [else stop]
       [if] main has moved past it [then] cancel without a re-run [else stop]
       [if] on re-reading the gate just before the cancel a runner has taken it [then] it is
            not cancelled; [if] main moved since the sweep [then] it is cancelled, not re-run;
            [if] main moves during the cancel wait [then] it is not re-run [else stop]
       [if] the cancel, the tip re-read after it, or the re-run (retried 3 times) fails
            [then] the alarm names THAT step and its own manual step (a failed cancel never
            says "re-run it": the run may still be live), and the sweep exits UNKNOWN; the
            alarm is written after acting, so it never claims a re-run that did not happen
            [else stop]
       [if] writing the alarm issue still fails after 3 tries [then] the whole alarm is printed
            as an `::error` annotation and the sweep exits UNKNOWN, so the red watchdog run
            carries it; the cancelled run has left the listing and no later sweep would
            [else stop]
    R3 Honest measurement ............................................ done + ran + regression
       [if] a listing fails, is not JSON, or is shorter than its total_count
            [then] UNKNOWN, exit 2, no action taken [else stop]
       [if] a job's created_at cannot be parsed [then] UNKNOWN, never "young" [else stop]
"""

from __future__ import annotations

import argparse
import functools
import json
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

DEFAULT_REPO = "private_owner/music-dj-tools"
WORKFLOW_FILE = "e2e.yml"
GATE_JOB_PREFIX = "e2e gate"
THRESHOLD = timedelta(minutes=30)
ALARM_LABEL = "ci:e2e-main-starved"
MARKER = "<!-- e2e-main-starved job={job_id} -->"
CANCEL_WAIT_S = 300
CANCEL_POLL_S = 10
#: Sleeps before each attempt of a post-cancel write (the re-run POST, the alarm): three
#: tries over about 30 s. Once a run is cancelled it leaves the unfinished-run listing, so
#: no later sweep sees it again; a transient failure here is this sweep's only chance.
RETRY_DELAYS_S = (0, 5, 20)
EXIT_OK, EXIT_ALARM, EXIT_UNKNOWN = 0, 1, 2

RERUN, ALARM, SUPERSEDED = "RERUN", "ALARM", "SUPERSEDED"
#: The step of act() that failed, named in the alarm (Codex r4208698122).
CANCEL_STEP, TIP_STEP, RERUN_STEP = "cancel", "tip re-read", "re-run"


class MeasurementError(RuntimeError):
    """A listing could not be read completely; starvation is unmeasured."""


@dataclass(frozen=True)
class Failure:
    """Which step of act() failed, and its error."""

    step: str
    error: str


@dataclass(frozen=True)
class Starved:
    run_id: int
    run_attempt: int
    run_url: str
    head_sha: str
    job_id: int
    job_name: str
    job_url: str
    queued_for: timedelta
    action: str


# ----------------------------------------------------------------------------- pure
def parse_time(value: object) -> datetime:
    """A GitHub ISO 8601 stamp as an aware UTC datetime, or MeasurementError."""
    if not isinstance(value, str) or not value:
        raise MeasurementError(f"unparseable timestamp {value!r}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise MeasurementError(f"unparseable timestamp {value!r}") from None
    if parsed.tzinfo is None:
        raise MeasurementError(f"timestamp without a zone {value!r}")
    return parsed.astimezone(UTC)


def is_main_push(run: dict, default_branch: str) -> bool:
    return run.get("event") == "push" and run.get("head_branch") == default_branch


def starved_gate(job: dict, now: datetime, threshold: timedelta = THRESHOLD) -> timedelta | None:
    """How long the gate job has waited for a runner, if past the threshold, else None."""
    if not str(job.get("name", "")).startswith(GATE_JOB_PREFIX):
        return None
    if job.get("status") != "queued" or job.get("runner_id"):
        return None
    waited = now - parse_time(job.get("created_at"))
    return waited if waited > threshold else None


def action_for(run: dict, tip_sha: str) -> str:
    """RERUN once on main's tip, ALARM only after that, SUPERSEDED once main moved on."""
    if run["head_sha"] != tip_sha:
        return SUPERSEDED
    return RERUN if int(run["run_attempt"]) == 1 else ALARM


def find_starved(
    runs: list[dict],
    jobs_by_run: dict[int, list[dict]],
    tip_sha: str,
    default_branch: str,
    now: datetime,
    threshold: timedelta = THRESHOLD,
) -> list[Starved]:
    found: list[Starved] = []
    for run in runs:
        if not is_main_push(run, default_branch) or run.get("status") == "completed":
            continue
        for job in jobs_by_run.get(int(run["id"]), []):
            waited = starved_gate(job, now, threshold)
            if waited is None:
                continue
            found.append(
                Starved(
                    run_id=int(run["id"]),
                    run_attempt=int(run["run_attempt"]),
                    run_url=str(run["html_url"]),
                    head_sha=str(run["head_sha"]),
                    job_id=int(job["id"]),
                    job_name=str(job["name"]),
                    job_url=str(job.get("html_url", "")),
                    queued_for=waited,
                    action=action_for(run, tip_sha),
                )
            )
    return found


def recheck(item: Starved, job: dict, tip_sha: str, now: datetime) -> Starved | None:
    """Re-judge a starved gate on state re-read just before acting (Codex r4208593734).

    None when the gate is no longer starved (a runner took it, it started, or it finished):
    nothing is cancelled. Otherwise the item, SUPERSEDED if main moved since the sweep read it.
    """
    if starved_gate(job, now) is None:
        return None
    return item if tip_sha == item.head_sha else replace(item, action=SUPERSEDED)


def rerun_or_supersede(item: Starved, tip_sha: str) -> Starved:
    """After the cancel, re-run only if main still points at the run's commit."""
    return item if tip_sha == item.head_sha else replace(item, action=SUPERSEDED)


def items_from_pages(pages: list[dict], key: str, path: str) -> list[dict]:
    """Flatten slurped pages, refusing a listing shorter than its own total_count."""
    if not pages:
        raise MeasurementError(f"gh api {path} returned no pages")
    try:
        items = [item for page in pages for item in page[key]]
        total = pages[0]["total_count"]
    except (KeyError, TypeError) as error:
        raise MeasurementError(f"gh api {path} is missing {error}") from None
    if len(items) != total:
        raise MeasurementError(f"gh api {path}: read {len(items)} of total_count {total}")
    return items


def outcome_text(item: Starved, failure: Failure | None) -> str:
    """What the sweep actually did, written AFTER acting, so the alarm never claims a step
    that failed. Each failed step gets its own text and manual step (Codex r4208698122):
    a failed cancel never tells the operator to re-run a run that may still be live."""
    if item.action == ALARM:
        return (
            f"Alarm only: this is attempt {item.run_attempt}, so the one automatic re-run is "
            "already spent. A human needs to look at the e2e runners. The run was NOT cancelled: it "
            "stays queued until a runner takes it or someone cancels it "
            f"(`gh run cancel {item.run_id}`), and newer main pushes wait behind it in the "
            "`push-tip` concurrency group."
        )
    if failure is not None and failure.step == CANCEL_STEP:
        then = (
            f"; once it completes, re-run it (`gh run rerun {item.run_id}`) only if "
            f"`{item.head_sha[:9]}` is still main's tip"
            if item.action == RERUN
            else ""
        )
        return (
            f"**The automatic cancel FAILED** ({failure.error}). Nothing was re-run, and the run "
            "may still be queued or running. Check it, then cancel it by hand "
            f"(`gh run cancel {item.run_id}`){then}."
        )
    if failure is not None and failure.step == TIP_STEP:
        return (
            f"**Cancelled, but re-reading main's tip FAILED** ({failure.error}), so it was NOT "
            f"re-run. Re-run it by hand (`gh run rerun {item.run_id}`) only if "
            f"`{item.head_sha[:9]}` is still main's tip."
        )
    if failure is not None:
        return (
            f"**The automatic re-run FAILED** ({failure.error}). The run is cancelled with no "
            "re-run queued, and no later sweep will retry it: re-run it by hand "
            f"(`gh run rerun {item.run_id}`)."
        )
    if item.action == RERUN:
        return "Cancelled and re-run once automatically (attempt 1 was starved)."
    return (
        "Cancelled without a re-run: main has moved past this commit, and the newer push run "
        "was waiting behind this one in the `push-tip` concurrency group."
    )


def lost_alarm_annotation(item: Starved, now: datetime, failure: Failure | None, error: str) -> str:
    """The alarm as a workflow `::error` annotation, for when the issue write itself failed
    after its retries (Codex r4208801831). The annotation and the red watchdog run are then
    the durable record: the cancelled run has left the listing, so no later sweep can
    write the issue. Newlines are %0A-escaped as the workflow command syntax requires."""
    body = report_body(item, now, failure).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return (
        f"::error title=Main e2e gate starved; alarm issue NOT written::The `{ALARM_LABEL}` issue "
        f"could not be written ({error}). The alarm it would have carried:%0A{body}"
    )


def report_body(item: Starved, now: datetime, failure: Failure | None = None) -> str:
    minutes = int(item.queued_for.total_seconds() // 60)
    outcome = outcome_text(item, failure)
    return "\n".join(
        [
            MARKER.format(job_id=item.job_id),
            "# Main e2e gate starved for a runner",
            "",
            f"- Job: [{item.job_name}]({item.job_url}) (id {item.job_id})",
            f"- Run: [{item.run_id}]({item.run_url}), attempt {item.run_attempt}",
            f"- Commit: `{item.head_sha}`",
            f"- Queued with no runner for {minutes} min (threshold "
            f"{int(THRESHOLD.total_seconds() // 60)} min), measured "
            f"{now.strftime('%a %d %b %Y %H:%M:%S %Z')}",
            f"- Action: **{item.action}**{f' (FAILED at {failure.step})' if failure else ''}. {outcome}",
            "",
            "Issue #5831. Reserve check: `python -m scripts.ci_merge_queue_pool --variable CI_RUNS_ON_E2E_TRUNK`.",
        ]
    )


# ----------------------------------------------------------------------------- I/O
@functools.cache
def _gh_path() -> str:
    """gh by resolved path, looked up once; absent is unmeasured (UNKNOWN), never the alarm."""
    gh = shutil.which("gh")
    if gh is None:
        raise MeasurementError("gh is not on PATH; the runner image must provide it")
    return gh


def _gh(args: list[str]) -> str:
    try:
        result = subprocess.run([_gh_path(), *args], capture_output=True, text=True, check=False)
    except OSError as error:
        # No gh, or not executable: unmeasured, and never the alarm's exit 1.
        raise MeasurementError(f"gh could not start: {error}") from None
    if result.returncode != 0:
        raise MeasurementError(f"gh {' '.join(args)} failed: {result.stderr.strip()[:300]}")
    return result.stdout


def _gh_json(args: list[str]) -> object:
    out = _gh(args)
    try:
        return json.loads(out)
    except json.JSONDecodeError as error:
        raise MeasurementError(f"gh {' '.join(args)} returned non-JSON ({error})") from None


def _gh_list(path: str, key: str) -> list[dict]:
    pages = _gh_json(["api", "--paginate", "--slurp", path])
    if not isinstance(pages, list):
        raise MeasurementError(f"gh api {path} did not return a page list")
    return items_from_pages(pages, key, path)


def measure(repo: str) -> tuple[list[dict], dict[int, list[dict]], str, str]:
    meta = _gh_json(["api", f"repos/{repo}"])
    if not isinstance(meta, dict) or not meta.get("default_branch"):
        raise MeasurementError(f"repos/{repo} has no default_branch")
    branch = str(meta["default_branch"])
    tip = _gh_json(["api", f"repos/{repo}/commits/{branch}"])
    if not isinstance(tip, dict) or not tip.get("sha"):
        raise MeasurementError(f"repos/{repo}/commits/{branch} has no sha")
    runs: list[dict] = []
    for status in ("queued", "in_progress"):
        runs += _gh_list(
            f"repos/{repo}/actions/workflows/{WORKFLOW_FILE}/runs"
            f"?event=push&branch={branch}&status={status}&per_page=100",
            "workflow_runs",
        )
    jobs_by_run = {
        int(run["id"]): _gh_list(f"repos/{repo}/actions/runs/{run['id']}/jobs?filter=latest&per_page=100", "jobs")
        for run in runs
    }
    return runs, jobs_by_run, str(tip["sha"]), branch


def _already_reported(repo: str, issue: int, job_id: int) -> bool:
    comments = _gh_json(["api", "--paginate", "--slurp", f"repos/{repo}/issues/{issue}/comments?per_page=100"])
    body = _gh_json(["api", f"repos/{repo}/issues/{issue}"])
    marker = MARKER.format(job_id=job_id)
    texts = [str(body.get("body") or "")] if isinstance(body, dict) else []
    if isinstance(comments, list):
        texts += [str(c.get("body") or "") for page in comments for c in page]
    return any(marker in text for text in texts)


def _alarm(repo: str, item: Starved, now: datetime, failure: Failure | None) -> None:
    _gh(
        [
            "label",
            "create",
            ALARM_LABEL,
            "--repo",
            repo,
            "--color",
            "B60205",
            "--description",
            "main's e2e gate sat queued with no runner past 30 min (#5831)",
            "--force",
        ]
    )
    open_issues = _gh_json(
        [
            "issue",
            "list",
            "--repo",
            repo,
            "--label",
            ALARM_LABEL,
            "--state",
            "open",
            "--limit",
            "1",
            "--json",
            "number",
        ]
    )
    body = report_body(item, now, failure)
    if isinstance(open_issues, list) and open_issues:
        number = int(open_issues[0]["number"])
        if _already_reported(repo, number, item.job_id):
            print(f"    job {item.job_id} already reported on #{number}; not commenting again")
            return
        _gh(["issue", "comment", str(number), "--repo", repo, "--body", body])
    else:
        title = f"Main e2e gate starved for a runner at {item.head_sha[:8]}"
        _gh(
            [
                "issue",
                "create",
                "--repo",
                repo,
                "--title",
                title,
                "--label",
                ALARM_LABEL,
                "--body",
                body,
            ]
        )


def _cancel_and_wait(repo: str, run_id: int) -> None:
    _gh(["api", "-X", "POST", f"repos/{repo}/actions/runs/{run_id}/cancel"])
    deadline = time.monotonic() + CANCEL_WAIT_S
    while time.monotonic() < deadline:
        run = _gh_json(["api", f"repos/{repo}/actions/runs/{run_id}"])
        if isinstance(run, dict) and run.get("status") == "completed":
            return
        time.sleep(CANCEL_POLL_S)
    raise MeasurementError(f"run {run_id} did not complete within {CANCEL_WAIT_S}s of cancel")


def _retried(call: Callable[[], None]) -> None:
    """Run a post-cancel write, retried over RETRY_DELAYS_S; raise its last error."""
    last: MeasurementError | None = None
    for delay in RETRY_DELAYS_S:
        time.sleep(delay)
        try:
            call()
        except MeasurementError as error:
            last = error
        else:
            return
    assert last is not None
    raise last


def _rerun(repo: str, run_id: int) -> None:
    def _post() -> None:
        _gh(["api", "-X", "POST", f"repos/{repo}/actions/runs/{run_id}/rerun"])

    _retried(_post)


def _tip(repo: str) -> str:
    meta = _gh_json(["api", f"repos/{repo}"])
    branch = str(meta.get("default_branch") or "") if isinstance(meta, dict) else ""
    tip = _gh_json(["api", f"repos/{repo}/commits/{branch}"]) if branch else None
    if not isinstance(tip, dict) or not tip.get("sha"):
        raise MeasurementError(f"repos/{repo}: could not re-read the default branch tip")
    return str(tip["sha"])


def _job(repo: str, job_id: int) -> dict:
    job = _gh_json(["api", f"repos/{repo}/actions/jobs/{job_id}"])
    if not isinstance(job, dict):
        raise MeasurementError(f"actions/jobs/{job_id} is not an object")
    return job


def act(repo: str, item: Starved) -> tuple[Starved | None, Failure | None]:
    """Re-read state, cancel and re-run as it then says, THEN alarm with what really happened.

    The gate and main's tip are re-read right before the cancel, and the tip again before the
    re-run, so a gate a runner just took is left alone and a commit main moved past is not
    re-run (Codex r4208593734). A window of one API round trip remains between the re-read
    and the cancel; GitHub has no compare-and-cancel. Returns (the item as acted on, or None
    when it was no longer starved; the failed step's error, or None). A failed cancel or
    re-run still alarms, naming the failure and the manual step, so the promised re-run is
    never silently lost (Codex r4208593749).
    """
    fresh = recheck(item, _job(repo, item.job_id), _tip(repo), datetime.now(UTC))
    if fresh is None:
        print(f"    job {item.job_id} is no longer starved on re-read; nothing cancelled")
        return None, None
    failure: Failure | None = None
    if fresh.action != ALARM:
        step = CANCEL_STEP
        try:
            _cancel_and_wait(repo, fresh.run_id)
            if fresh.action == RERUN:
                step = TIP_STEP
                fresh = rerun_or_supersede(fresh, _tip(repo))
                if fresh.action == RERUN:
                    step = RERUN_STEP
                    _rerun(repo, fresh.run_id)
        except MeasurementError as error:
            failure = Failure(step, str(error))
    now = datetime.now(UTC)
    try:
        # Retried like the re-run, and safe to repeat: _alarm skips a job whose marker the
        # open issue already carries, so a write that landed but reported failure is not
        # posted twice.
        _retried(lambda: _alarm(repo, fresh, now, failure))
    except MeasurementError as error:
        print(lost_alarm_annotation(fresh, now, failure, str(error)))
        raise MeasurementError(f"alarm issue for job {fresh.job_id} not written: {error}") from None
    return fresh, failure


def verdict(
    runs: list[dict],
    jobs_by_run: dict[int, list[dict]],
    tip: str,
    branch: str,
    now: datetime,
) -> tuple[int, list[str], list[Starved]]:
    """Exit code, printed lines and starved gates for one measured sweep. Pure."""
    starved = find_starved(runs, jobs_by_run, tip, branch, now)
    if not starved:
        return (
            EXIT_OK,
            [
                f"[OK] {len(runs)} unfinished main push E2E run(s) at tip {tip[:9]}; no gate "
                f"queued without a runner past {int(THRESHOLD.total_seconds() // 60)} min"
            ],
            [],
        )
    lines = [
        f"::error title=Main e2e gate starved::{item.job_name} (job {item.job_id}, run "
        f"{item.run_id} attempt {item.run_attempt}, {item.head_sha[:9]}) queued with no "
        f"runner for {int(item.queued_for.total_seconds() // 60)} min; action {item.action}"
        for item in starved
    ]
    return EXIT_ALARM, lines, starved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=DEFAULT_REPO, help="owner/name to read")
    parser.add_argument("--dry-run", action="store_true", help="report only; no issue, no cancel")
    args = parser.parse_args(argv)
    now = datetime.now(UTC)
    try:
        exit_code, lines, starved = verdict(*measure(args.repo), now)
    except MeasurementError as error:
        print(f"[UNKNOWN] {error}")
        return EXIT_UNKNOWN
    print("\n".join(lines))
    if args.dry_run:
        return exit_code
    # One starved run's failure does not stop the sweep: each item is re-read and acted on
    # independently, so the rest still get their cancel, re-run or alarm; the sweep then exits
    # UNKNOWN for the failure (REVIEW-13, #6730).
    unknown = False
    for item in starved:
        try:
            acted, failure = act(args.repo, item)
        except MeasurementError as error:
            print(f"[UNKNOWN] acting on run {item.run_id}: {error}")
            unknown = True
            continue
        if acted is not None and failure is not None:
            print(
                f"[UNKNOWN] {acted.action} on run {acted.run_id}: the {failure.step} step failed "
                f"(alarm says so): {failure.error}"
            )
            unknown = True
    return EXIT_UNKNOWN if unknown else exit_code


if __name__ == "__main__":
    sys.exit(main())
