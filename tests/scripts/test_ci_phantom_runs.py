"""A phantom run (GitHub reports `in_progress` forever) never wedges a bookkeeping pass.

[if] a phantom run is in flight [then] no pass waits or fails on it; a live run holds, [else stop].

Fleet-af issue #138. On Thu 1 Oct 2026 runs 36802069871 and 36803336485 read
`in_progress` for 17h+ while cancel and force-cancel both returned HTTP 409. Two passes
could be pinned by such a run:

  - the cost guard's and stable evidence's census (`scripts/ci_run_batch.py census`)
    held the mark for every watched run in flight since before the lookback, so the
    pass failed on every cadence;
  - the trunk-tip-only closed-PR sweep POSTed a cancel GitHub answers with 409, raised,
    and failed on every cadence, leaving every closed-PR run listed after it running.

Regression lines:
  - if a phantom still holds the census, or is not named in its output, then broken
  - if a recent in_progress run older than the lookback no longer holds, then broken
    (the overshoot control: an age rule that skips everything also "fixes" the wedge)
  - if a queued run of any age is called a phantom, then broken
  - if a run that sat queued for 14 h and started a minute ago is called a phantom, then
    broken (Sol P1 on #4858: age is measured from updated_at, never created_at)
  - if an in_progress run idle for 17 h with a job still queued is called a phantom, then
    broken (a saturated pool bounds no job)
  - if a run with no job waiting is read as live because the jobs read was skipped, or a
    jobs listing longer than one page is trusted, then broken
  - if the closed-PR sweep POSTs a cancel for a phantom, or stops at one, then broken
  - if the closed-PR sweep stops cancelling a recent in_progress closed-PR run, then broken
  - if PHANTOM_AFTER_HOURS falls to or below the longest timeout chain of any workflow,
    the longest census lookback, or the 6.0 h measured run span, then broken
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, Self

import pytest
import yaml

import scripts.ci_run_batch as batch
import scripts.ci_trunk_tip_only as tip
from scripts.ci_phantom_runs import PHANTOM_AFTER_HOURS, is_phantom, split_phantoms

pytestmark = pytest.mark.requirement("OPS-46")

REPO = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO / ".github" / "workflows"
NOW = datetime(2026, 10, 1, 19, 0, 0, tzinfo=UTC)
LOOKBACK = timedelta(hours=3)
WATCHED = {"CI", "E2E"}
# The longest creation-to-completion span measured on a live run, cited in
# ci-cost-guard.yml's census comment (one CI run, Mon 28 Sep 2026).
MEASURED_LONGEST_RUN_HOURS = 6.0


def _stamp(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run(
    run_id: int,
    *,
    hours_old: float,
    status: str = "in_progress",
    name: str = "CI",
    attempt: int = 1,
    created_hours_ago: float | None = None,
) -> dict[str, Any]:
    """`hours_old` is the time since the run last did anything (updated_at), the phantom
    rule's age. created_at defaults to far older, so a rule that read it would misfire."""
    return {
        "id": run_id,
        "name": name,
        "status": status,
        "run_attempt": attempt,
        "created_at": _stamp(created_hours_ago if created_hours_ago is not None else hours_old + 20),
        "updated_at": _stamp(hours_old),
    }


def _jobs_of(*statuses: str) -> Callable[[int], list[dict[str, str]]]:
    """A jobs reader answering the same job statuses for every run."""
    return lambda _run_id: [{"status": status} for status in statuses]


# The two lost runs' signatures (Thu 1 Oct 2026): every job completed, or jobs stuck in_progress.
ORPHAN_JOBS = _jobs_of("completed", "completed", "in_progress")


def _no_job_read(run_id: int) -> list[dict[str, str]]:
    raise AssertionError(f"run {run_id}: jobs were read for a run that cannot be a phantom")


# ----- the rule -----


def test_an_idle_in_progress_run_with_no_waiting_job_is_a_phantom() -> None:
    assert is_phantom(_run(36802069871, hours_old=17), NOW, jobs_of=ORPHAN_JOBS)
    assert is_phantom(_run(36802069871, hours_old=17), NOW, jobs_of=_jobs_of("completed"))


def test_a_recently_active_in_progress_run_is_not_a_phantom_and_reads_no_jobs() -> None:
    assert not is_phantom(_run(1, hours_old=4), NOW, jobs_of=_no_job_read)
    assert not is_phantom(_run(2, hours_old=PHANTOM_AFTER_HOURS), NOW, jobs_of=_no_job_read), (
        "the bound is strict"
    )


@pytest.mark.parametrize("status", ["queued", "waiting", "pending", "requested"])
def test_a_queued_run_of_any_age_is_never_a_phantom(status: str) -> None:
    """A job queued on a saturated pool is bounded by no timeout (Codex P1 on #3844)."""
    assert not is_phantom(_run(3, hours_old=200, status=status), NOW, jobs_of=_no_job_read)


def test_a_run_queued_for_fourteen_hours_that_just_started_is_not_a_phantom() -> None:
    """Sol P1 on #4858. created_at includes the queue wait, so a rule that aged the run from
    it would call this run a phantom the minute it began executing and skip a live run."""
    run = _run(5, hours_old=0.02, created_hours_ago=14)
    assert not is_phantom(run, NOW, jobs_of=_no_job_read)


@pytest.mark.parametrize("waiting", ["queued", "waiting", "pending", "requested"])
def test_an_idle_run_with_a_job_still_waiting_for_a_runner_is_not_a_phantom(waiting: str) -> None:
    """The run is alive but idle behind a saturated pool, which no timeout bounds. The
    positive control is the same run with that job completed: it IS a phantom."""
    run = _run(6, hours_old=17)
    assert not is_phantom(run, NOW, jobs_of=_jobs_of("completed", waiting))
    assert is_phantom(run, NOW, jobs_of=_jobs_of("completed", "completed"))


def test_a_run_with_no_updated_at_is_refused_not_guessed() -> None:
    with pytest.raises(ValueError, match="no updated_at"):
        is_phantom({"id": 4, "status": "in_progress"}, NOW, jobs_of=_no_job_read)


def test_split_keeps_order_and_loses_nothing() -> None:
    runs = [_run(1, hours_old=20), _run(2, hours_old=1), _run(3, hours_old=30)]
    live, phantom = split_phantoms(runs, NOW, jobs_of=ORPHAN_JOBS)
    assert [r["id"] for r in live] == [2]
    assert [r["id"] for r in phantom] == [1, 3]


# ----- the census (cost guard, stable evidence) -----


def test_census_skips_a_phantom_and_names_it() -> None:
    inflight = [_run(36802069871, hours_old=17), _run(36803336485, hours_old=17.5)]
    held, named = batch.split_held_back(inflight, WATCHED, NOW, LOOKBACK, jobs_of=ORPHAN_JOBS)
    assert held == []
    assert [r["id"] for r in named] == [36802069871, 36803336485]


def test_census_still_holds_a_recent_in_progress_run() -> None:
    """The overshoot control. 4 h is past the 3 h lookback and well under the phantom bound:
    the run is alive, the next pass cannot list it, so the mark must stay."""
    inflight = [_run(10, hours_old=4), _run(11, hours_old=17)]
    held, phantoms = batch.split_held_back(inflight, WATCHED, NOW, LOOKBACK, jobs_of=ORPHAN_JOBS)
    assert [r["id"] for r in held] == [10]
    assert [r["id"] for r in phantoms] == [11]


def test_census_still_holds_a_run_that_started_after_a_long_queue_wait() -> None:
    """Sol P1 on #4858, through the census: created 14 h ago, executing for a minute. It is
    past the lookback and live, so it holds the mark."""
    inflight = [_run(15, hours_old=0.02, created_hours_ago=14)]
    held, phantoms = batch.split_held_back(inflight, WATCHED, NOW, LOOKBACK, jobs_of=_no_job_read)
    assert [r["id"] for r in held] == [15]
    assert phantoms == []


def test_census_still_holds_an_idle_run_with_a_job_queued_behind_a_busy_pool() -> None:
    inflight = [_run(16, hours_old=17)]
    held, phantoms = batch.split_held_back(
        inflight, WATCHED, NOW, LOOKBACK, jobs_of=_jobs_of("completed", "queued")
    )
    assert [r["id"] for r in held] == [16]
    assert phantoms == []


def test_census_still_holds_an_old_queued_run() -> None:
    inflight = [_run(12, hours_old=30, status="queued")]
    held, phantoms = batch.split_held_back(inflight, WATCHED, NOW, LOOKBACK, jobs_of=_no_job_read)
    assert [r["id"] for r in held] == [12]
    assert phantoms == []


def test_census_phantom_rule_leaves_unwatched_and_rerun_filters_alone() -> None:
    inflight = [_run(13, hours_old=20, name="Docs"), _run(14, hours_old=20, attempt=2)]
    assert batch.split_held_back(inflight, WATCHED, NOW, LOOKBACK, jobs_of=_no_job_read) == ([], [])


def test_census_cli_reports_phantoms_by_id_and_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """End to end through `main`: held=0 is what the workflow's last step reads, the ids
    land in the log, the step output and the job summary."""
    inflight = [_run(36802069871, hours_old=17), _run(20, hours_old=1, created_hours_ago=1)]
    monkeypatch.setattr(batch, "fetch_inflight_runs", lambda *a, **k: inflight)
    monkeypatch.setattr(batch, "fetch_run_jobs", lambda repository, run_id, *a, **k: ORPHAN_JOBS(run_id))
    monkeypatch.setattr(batch, "datetime", _FrozenDatetime)
    output, summary = tmp_path / "out", tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_TOKEN", "unused")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

    code = batch.main(["census", "--repository", "o/r", "--watched", "CI,E2E", "--lookback-hours", "3"])

    assert code == 0
    out = capsys.readouterr().out
    assert "[census] in_flight=2 held=0 phantom=1 phantom_ids=36802069871" in out
    assert "::warning::phantom-run-skipped caller=ci_run_batch census run_id=36802069871" in out
    assert "held=0\n" in output.read_text(encoding="utf-8")
    assert "phantom_ids=36802069871\n" in output.read_text(encoding="utf-8")
    assert "run_id=36802069871" in summary.read_text(encoding="utf-8")


def test_census_cli_still_holds_a_live_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    inflight = [_run(21, hours_old=4)]
    monkeypatch.setattr(batch, "fetch_inflight_runs", lambda *a, **k: inflight)
    monkeypatch.setattr(batch, "fetch_run_jobs", lambda repository, run_id, *a, **k: _no_job_read(run_id))
    monkeypatch.setattr(batch, "datetime", _FrozenDatetime)
    output = tmp_path / "out"
    monkeypatch.setenv("GITHUB_TOKEN", "unused")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    batch.main(["census", "--repository", "o/r", "--watched", "CI", "--lookback-hours", "3"])

    assert "held=1\n" in output.read_text(encoding="utf-8")
    assert "phantom=0 phantom_ids=none" in capsys.readouterr().out


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz: tzinfo | None = None) -> Self:
        return cls.fromtimestamp(NOW.timestamp(), tz=UTC)


# ----- the trunk-tip-only closed-PR sweep -----


def _pr_run(run_id: int, *, hours_old: float, status: str = "in_progress") -> tip.QueuedRun:
    """`hours_old` is the time since the run last did anything; created_at is far older."""
    return tip.QueuedRun(
        run_id=run_id,
        name="CI",
        event="pull_request",
        head_branch=f"af--closed-{run_id}",
        head_repo_owner="maintainer",
        head_sha="b" * 40,
        created_at=NOW - timedelta(hours=hours_old + 20),
        status=status,
        updated_at=NOW - timedelta(hours=hours_old),
    )


def _record(seen: list[int], run_id: int) -> bool:
    seen.append(run_id)
    return True


def _recording_cancel(posts: list[int]):
    def cancel(run_id: int) -> tip.CancelOutcome:
        posts.append(run_id)
        return tip.CancelOutcome.CANCELLED

    return cancel


def test_closed_pr_sweep_skips_a_phantom_and_cancels_the_rest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The phantom comes FIRST, so a sweep that raised on it would never reach run 31."""
    posts: list[int] = []
    rechecked: list[int] = []
    monkeypatch.setattr(tip, "_cancel_run", _recording_cancel(posts))
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

    counts = tip.execute_closed_pr_sweep(
        (_pr_run(36803336485, hours_old=17), _pr_run(31, hours_old=1)),
        dry_run=False,
        still_closed=lambda run: _record(rechecked, run.run_id),
        now=NOW,
        jobs_of=ORPHAN_JOBS,
    )

    assert posts == [31], "no cancel is POSTed for the phantom"
    assert rechecked == [31], "the phantom costs no recheck call either"
    assert (counts.planned, counts.cancelled, counts.phantom_skipped) == (1, 1, (36803336485,))
    out = capsys.readouterr().out
    assert "::warning::phantom-run-skipped caller=closed-PR sweep run_id=36803336485" in out
    assert "run_id=36803336485" in summary.read_text(encoding="utf-8")


def test_closed_pr_sweep_still_cancels_a_recent_in_progress_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The overshoot control: an in_progress run of a closed PR is the sweep's main case."""
    posts: list[int] = []
    monkeypatch.setattr(tip, "_cancel_run", _recording_cancel(posts))
    counts = tip.execute_closed_pr_sweep(
        (_pr_run(32, hours_old=5), _pr_run(33, hours_old=40, status="queued")),
        dry_run=False,
        still_closed=lambda _run: True,
        now=NOW,
        jobs_of=_no_job_read,
    )
    assert posts == [32, 33]
    assert counts.phantom_skipped == ()


def test_closed_pr_sweep_still_cancels_a_run_that_started_after_a_long_queue_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sol P1 on #4858, through the sweep: created 34 h ago, active a minute ago."""
    posts: list[int] = []
    monkeypatch.setattr(tip, "_cancel_run", _recording_cancel(posts))
    live = _pr_run(35, hours_old=0.02)
    counts = tip.execute_closed_pr_sweep(
        (live,), dry_run=False, still_closed=lambda _run: True, now=NOW, jobs_of=_no_job_read
    )
    assert posts == [35]
    assert counts.phantom_skipped == ()


def test_the_listing_carries_status_into_the_sweep() -> None:
    """Without status on the parsed run, no listed run could ever read as a phantom."""
    run = tip._parse_queued_run(
        {
            "id": 34,
            "name": "CI",
            "event": "pull_request",
            "status": "in_progress",
            "head_branch": "af--x",
            "head_repository": {"owner": {"login": "maintainer"}},
            "head_sha": "c" * 40,
            "created_at": "2026-09-30T01:00:00Z",
            "updated_at": "2026-09-30T02:00:00Z",
        }
    )
    assert run.status == "in_progress"
    assert is_phantom(
        {"id": run.run_id, "status": run.status, "updated_at": run.updated_at}, NOW, jobs_of=ORPHAN_JOBS
    )


def test_the_sweeps_jobs_read_refuses_a_listing_longer_than_one_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """A waiting job on page 2 would read as none, and a live run would be skipped."""
    monkeypatch.setattr(tip, "_gh_api_json", lambda path: {"total_count": 150, "jobs": [{"status": "completed"}] * 100})
    with pytest.raises(tip.PreconditionError, match="150 jobs"):
        tip._run_jobs(40)


def test_the_censuss_jobs_read_refuses_a_listing_longer_than_one_page() -> None:
    page = {"total_count": 150, "jobs": [{"status": "completed"}] * 100}
    with pytest.raises(RuntimeError, match="150 jobs"):
        batch.fetch_run_jobs("o/r", 41, "unused", "test", get_json=lambda url: page)


def test_the_censuss_jobs_read_asks_for_the_latest_attempt_and_returns_the_jobs() -> None:
    urls: list[str] = []

    def get_json(url: str) -> dict[str, Any]:
        urls.append(url)
        return {"total_count": 1, "jobs": [{"status": "queued"}]}

    assert batch.fetch_run_jobs("o/r", 42, "unused", "test", get_json=get_json) == [{"status": "queued"}]
    assert urls == ["https://api.github.com/repos/o/r/actions/runs/42/jobs?filter=latest&per_page=100"]


# ----- the threshold is derived from this repository, not remembered -----


def _max_minutes(value: object) -> int:
    """A literal, or the largest literal an expression can choose between."""
    if isinstance(value, int):
        return value
    numbers = [int(n) for n in re.findall(r"\b\d+\b", str(value))]
    # An expression naming no number is a timeout this test cannot measure: refuse.
    assert numbers, f"cannot measure timeout-minutes {value!r}"
    return max(numbers)


def _longest_chain_minutes(path: Path, seen: tuple[Path, ...] = ()) -> int:
    assert path not in seen, f"reusable workflow cycle at {path.name}"
    jobs = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("jobs") or {}
    own: dict[str, int] = {}
    for name, spec in jobs.items():
        uses = str(spec.get("uses", ""))
        if uses.startswith("./.github/workflows/"):
            own[name] = _longest_chain_minutes(REPO / uses[2:], (*seen, path))
        else:
            # GitHub's default when a job sets no timeout-minutes.
            own[name] = _max_minutes(spec.get("timeout-minutes", 360))

    memo: dict[str, int] = {}

    def chain(name: str) -> int:
        if name not in memo:
            needs = jobs[name].get("needs") or []
            needs = [needs] if isinstance(needs, str) else needs
            memo[name] = own[name] + max((chain(n) for n in needs), default=0)
        return memo[name]

    return max((chain(n) for n in jobs), default=0)


def test_phantom_bound_exceeds_every_workflows_longest_timeout_chain() -> None:
    files = sorted(WORKFLOWS.glob("*.yml"))
    assert len(files) >= 20, "the workflow directory was not found or not read"
    longest = max(_longest_chain_minutes(path) for path in files)
    # Positive control, derived rather than remembered: the walk must see past the longest
    # single literal timeout, which proves it sums `needs` chains instead of reading one job.
    single = max(int(m) for path in files for m in re.findall(r"timeout-minutes:\s*(\d+)", path.read_text("utf-8")))
    assert longest > single, f"positive control: no needs chain summed past the longest single job ({single})"
    assert longest < PHANTOM_AFTER_HOURS * 60, (
        f"a {longest}-minute job chain can legitimately run past the {PHANTOM_AFTER_HOURS} h phantom bound"
    )


def test_phantom_bound_exceeds_every_census_lookback_and_the_measured_run_span() -> None:
    # Every workflow that runs the census, found rather than listed: a retired caller must
    # not break this test, and a new one must not escape it.
    callers = [p for p in sorted(WORKFLOWS.glob("*.yml")) if "scripts.ci_run_batch census" in p.read_text("utf-8")]
    assert any(p.name == "stable-evidence.yml" for p in callers), "positive control: stable-evidence runs the census"
    lookbacks: list[int] = []
    for path in callers:
        found = re.findall(r'LOOKBACK_HOURS: "(\d+)"', path.read_text(encoding="utf-8"))
        assert found, f"{path.name} runs the census but sets no LOOKBACK_HOURS"
        lookbacks.extend(int(h) for h in found)
    assert max(lookbacks) < PHANTOM_AFTER_HOURS, (
        "at or below the lookback, every run the census would hold reads as a phantom"
    )
    assert PHANTOM_AFTER_HOURS > MEASURED_LONGEST_RUN_HOURS
