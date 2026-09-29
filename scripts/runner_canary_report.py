"""Runner-canary report: each vendor's pytest shards against self-hosted, on the same SHAs.

For every canary run (`.github/workflows/runner-canary.yml`) in each vendor's own
`target_repo` (the source repository or the canary org's mirror), each vendor shard is
paired with the self-hosted shard of the same SHA from the source repository's `ci.yml`
push run on `main`. A vendor's rows are read from its target repository only. Per vendor
it reports:

- median push-to-verdict ratio: median(vendor verdict latency) / median(self-hosted), where
  a run's verdict latency is max(shard completed_at) - run created_at;
- p95 start latency: nearest-rank p95 of started_at - created_at over EVERY vendor shard
  job (attempt 1 of every run), paired or not;
- infra failures per 100 shard jobs, over every finished vendor shard job, paired or not: a
  failure before or without the pytest step, a pytest step that failed on its own
  wall-budget TIMEOUT (read from its check-run annotation), a `timed_out`, and, once the
  budget gate has handed the job to the vendor, ANY cancel, skip or job that never started.
  A never-started job also counts as an infinite start latency, and so does every shard a
  gate-passed run owed (its gate's planned-vendor annotation, times `shard_count`) that
  GitHub returned no job for; a gate-passed run whose planned vendors cannot be read is
  UNKNOWN. Only a run whose gate never
  passed (superseded in the concurrency queue, or refused) is identifiably ours and left
  out; a job still in flight makes the verdict UNKNOWN;
- outcome agreement: the share of shard pairs with a test verdict on both sides that agree,
  over paired commits only, as is the push-to-verdict ratio.

Verdicts, per vendor (decision record: docs/decisions/ADR-NEW-runner-canary.md):
- FAIL when a known-red SHA comes back green on the vendor (the canary cannot go red), or
  when at least `min_paired_commits` pairs exist, a known-red SHA came back red on the
  vendor, and a bar in ci/runner-canary.json is missed;
- UNKNOWN when fewer than `min_paired_commits` (20) commits are paired, when no known-red
  SHA is configured, or when a known-red SHA was not run on the vendor or is not red on
  self-hosted, even if a bar is missed: a canary not yet shown to go red has not measured
  anything, a miss included (Sol P2 on 27840126b). UNKNOWN is never a pass;
- PASS otherwise.

A paired commit has all shards on BOTH sides with a non-dropped outcome, from attempt 1 of
the earliest run for that SHA on each side: push-to-verdict is the FIRST verdict.

Usage:
    python -m scripts.runner_canary_report --config ci/runner-canary.json \\
        [--vendor avrea] [--known-red-sha <sha> ...] [--json out.json]

Reads through `gh api`, so it uses gh's own authentication, which needs read access to
every target repository and the source repository. Exit 0 all PASS, 1 any FAIL, 3 any
UNKNOWN (including a GitHub read that failed).

Requirements (mini-PRD)
- [if] fewer than 20 commits are paired [then] UNKNOWN, never PASS, [else stop] ✔︎ ✅ 🎯
- [if] no known-red SHA came back red on the vendor [then] UNKNOWN even when a bar is
  missed, or FAIL when it came back green, [else stop] ✔︎ ✅ 🎯
- [if] 20+ pairs meet every bar with a red control [then] PASS, [else stop] ✔︎ ✅ 🎯
- [if] a gate-passed run lacks a job for a shard its gate planned [then] that shard is an
  infra failure with infinite start latency (pending while the run is still going), and a
  run whose planned vendors cannot be read is UNKNOWN, [else stop] ✔︎ ✅ 🎯
- [if] a GitHub read fails [then] exit 3 UNKNOWN, [else stop] ✔︎ ✅ 🎯
- [if] a vendor shard never started, or was cancelled after the gate passed [then] it is
  infra and a start-latency breach; [if] the run's gate never passed [then] it is excluded,
  [else stop] ✔︎ ✅ 🎯
- [if] a pytest step failed on its TIMEOUT annotation [then] infra, never a test red,
  [else stop] ✔︎ ✅ 🎯
- [if] an infra failure or slow start lands on a commit with no complete baseline [then]
  it still counts in the operational rates, and never in the ratio or agreement, [else
  stop] ✔︎ ✅ 🎯
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import subprocess
import sys
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from scripts.runner_canary_rows import (
    BASELINE_JOB,
    GATE_JOB,
    VENDOR_JOB,
    ReadFailed,
    ShardRow,
    shard_jobs,
)

EXIT_PASS, EXIT_FAIL, EXIT_UNKNOWN = 0, 1, 3
CANARY_WORKFLOW = ".github/workflows/runner-canary.yml"
GH_TIMEOUT_S = 120


def _first_run_by_sha(rows: Iterable[ShardRow]) -> dict[str, list[ShardRow]]:
    """Each SHA's rows from its earliest run only."""
    by_run: dict[tuple[str, int], list[ShardRow]] = defaultdict(list)
    for row in rows:
        by_run[(row.sha, row.run_id)].append(row)
    first: dict[str, list[ShardRow]] = {}
    for (sha, _), run_rows in sorted(by_run.items(), key=lambda kv: kv[1][0].run_created_at):
        first.setdefault(sha, run_rows)
    return first


def _complete(run_rows: list[ShardRow] | None, shard_count: int) -> bool:
    return (
        run_rows is not None
        and sorted(r.shard for r in run_rows) == list(range(1, shard_count + 1))
        and all(r.outcome in ("green", "red", "infra") and not r.missing for r in run_rows)
    )


def _verdict_latency_s(run_rows: list[ShardRow]) -> float:
    """Push to the last shard's verdict. Only a complete run is paired, and every shard of
    one has finished, so a missing completed_at is a broken pairing, never a zero."""
    completed = [r.completed_at for r in run_rows]
    finished = [c for c in completed if c is not None]
    if len(finished) != len(completed):
        raise ValueError(f"paired run {run_rows[0].run_id} has a shard with no completed_at")
    return (max(finished) - run_rows[0].run_created_at).total_seconds()


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


# ----- the verdict --------------------------------------------------------------------


@dataclass
class VendorReport:
    vendor: str
    verdict: Literal["PASS", "FAIL", "UNKNOWN"] = "UNKNOWN"
    paired_commits: int = 0
    verdict_latency_ratio: float | None = None
    p95_queue_wait_s: float | None = None
    infra_failures_per_100: float | None = None
    outcome_agreement: float | None = None
    shard_jobs: int = 0
    reasons: list[str] = field(default_factory=list)


def _control_state(sha: str, vendor_runs: dict, baseline_runs: dict) -> tuple[str, str]:
    """(state, reason) for one known-red SHA: ok / green / not-run / invalid."""
    baseline = baseline_runs.get(sha)
    if not baseline or not any(r.outcome == "red" for r in baseline):
        return "invalid", f"known-red {sha[:12]} is not red on self-hosted, so it is no control"
    vendor = vendor_runs.get(sha)
    if not vendor:
        return "not-run", f"known-red {sha[:12]} was not run on this vendor"
    if any(r.outcome == "red" for r in vendor):
        return "ok", ""
    if all(r.outcome == "green" for r in vendor) and _complete(vendor, len(baseline)):
        return "green", f"known-red {sha[:12]} came back GREEN: this canary cannot go red"
    return "not-run", f"known-red {sha[:12]} has no test verdict on this vendor (infra or dropped)"


#: (metric, bar key, label): the bar is missed when the metric exceeds its ceiling.
CEILING_BARS = (
    ("verdict_latency_ratio", "max_verdict_latency_ratio", "push-to-verdict ratio"),
    ("p95_queue_wait_s", "max_p95_queue_wait_seconds", "p95 start latency (s)"),
    ("infra_failures_per_100", "max_infra_failures_per_100_jobs", "infra failures per 100 jobs"),
)


def _start_latency_s(row: ShardRow) -> float:
    """Seconds from queue to start; a job that never started waits forever (a breach)."""
    if row.never_started or row.started_at is None:
        return math.inf
    return (row.started_at - row.created_at).total_seconds()


def _measure_operations(report: VendorReport, rows: list[ShardRow]) -> None:
    """Start latency and infra rate over EVERY finished vendor shard job, paired or not.

    A job whose commit has no complete self-hosted run is still a job the runner started
    late or lost, so these rates never shrink to the paired subset. A lost job (never
    started, or cancelled short of the cap after the gate) is already `infra` and waits
    forever. Only `ours` (the gate never passed) and `pending` rows are left out.
    """
    finished = [r for r in rows if r.outcome in ("green", "red", "infra")]
    report.p95_queue_wait_s = _p95([_start_latency_s(r) for r in finished])
    report.shard_jobs = len(finished)
    if finished:
        report.infra_failures_per_100 = (
            100 * sum(r.outcome == "infra" for r in finished) / len(finished)
        )


def _measure_against_baseline(
    report: VendorReport, vendor_runs: dict, baseline_runs: dict, paired: list[str]
) -> None:
    """Push-to-verdict ratio and outcome agreement, which need the self-hosted pair."""
    vendor_median = statistics.median(_verdict_latency_s(vendor_runs[s]) for s in paired)
    baseline_median = statistics.median(_verdict_latency_s(baseline_runs[s]) for s in paired)
    report.verdict_latency_ratio = vendor_median / baseline_median
    verdict_pairs = [
        (v.outcome, b.outcome)
        for s in paired
        for v, b in zip(
            sorted(vendor_runs[s], key=lambda r: r.shard),
            sorted(baseline_runs[s], key=lambda r: r.shard),
            strict=True,
        )
        if v.outcome in ("green", "red") and b.outcome in ("green", "red")
    ]
    if verdict_pairs:
        report.outcome_agreement = sum(v == b for v, b in verdict_pairs) / len(verdict_pairs)


def _bar_findings(report: VendorReport, bars: dict[str, float]) -> tuple[list[str], list[str]]:
    """(missed bars, unmeasured reasons) for the measured metrics."""
    failed: list[str] = []
    unknown: list[str] = []
    if report.paired_commits < bars["min_paired_commits"]:
        unknown.append(
            f"{report.paired_commits} paired commits, "
            f"fewer than the {bars['min_paired_commits']:g} required"
        )
    if report.outcome_agreement is None:
        unknown.append("no shard pair has a test verdict on both sides, so agreement is unmeasured")
    elif report.outcome_agreement < bars["min_outcome_agreement"]:
        failed.append(
            f"outcome agreement {report.outcome_agreement:.3f} < {bars['min_outcome_agreement']:g}"
        )
    for metric, bar, label in CEILING_BARS:
        value = getattr(report, metric)
        if value is not None and value > bars[bar]:
            failed.append(f"{label} {value:.3f} > {bars[bar]:g}")
    return failed, unknown


def _control_findings(controls: list[tuple[str, str]]) -> tuple[list[str], list[str]]:
    """(controls that came back green, unmeasured reasons) for the known-red SHAs."""
    green = [reason for state, reason in controls if state == "green"]
    if not controls:
        return green, [
            "no known-red SHA configured: a canary never shown to go red is not measuring"
        ]
    if any(state == "ok" for state, _ in controls):
        return green, []
    return green, [reason for state, reason in controls if state in ("not-run", "invalid")]


def evaluate_vendor(
    vendor: str,
    vendor_rows: list[ShardRow],
    baseline_rows: list[ShardRow],
    *,
    bars: dict[str, float],
    known_red_shas: Iterable[str],
    shard_count: int,
) -> VendorReport:
    report = VendorReport(vendor=vendor)
    own_rows = [r for r in vendor_rows if r.vendor == vendor and r.outcome != "ours"]
    _measure_operations(report, own_rows)
    in_flight = sum(r.outcome == "pending" for r in own_rows)
    vendor_runs = _first_run_by_sha(own_rows)
    baseline_runs = _first_run_by_sha(baseline_rows)
    paired = sorted(
        sha
        for sha in vendor_runs
        if _complete(vendor_runs[sha], shard_count)
        and _complete(baseline_runs.get(sha), shard_count)
    )
    report.paired_commits = len(paired)
    if paired:
        _measure_against_baseline(report, vendor_runs, baseline_runs, paired)
    failed, unknown = _bar_findings(report, bars)
    control_green, control_unknown = _control_findings(
        [_control_state(sha, vendor_runs, baseline_runs) for sha in known_red_shas]
    )
    unknown += control_unknown
    if in_flight:
        unknown.append(f"{in_flight} vendor shard jobs are still in flight; re-run when done")

    if control_green:
        report.verdict, report.reasons = "FAIL", control_green + failed + unknown
    elif control_unknown:
        # No valid red control: a missed bar is reported, but it is not yet a measurement.
        report.verdict, report.reasons = "UNKNOWN", unknown + failed
    elif failed and report.paired_commits >= bars["min_paired_commits"]:
        report.verdict, report.reasons = "FAIL", failed + unknown
    elif unknown:
        report.verdict, report.reasons = "UNKNOWN", unknown + failed
    else:
        report.verdict, report.reasons = "PASS", []
    return report


# ----- GitHub reads (imperative shell) --------------------------------------------


def _gh_lines(path: str, jq: str) -> list[dict[str, Any]]:
    try:
        result = subprocess.run(
            ["gh", "api", "--paginate", path, "--jq", jq],
            check=False,
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT_S,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise ReadFailed(f"gh api {path}: {exc}") from exc
    if result.returncode != 0:
        raise ReadFailed(f"gh api {path} exit {result.returncode}: {result.stderr.strip()[:300]}")
    try:
        return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    except json.JSONDecodeError as exc:
        raise ReadFailed(f"gh api {path} returned non-JSON: {exc}") from exc


#: One GitHub read: (request path, jq selector) -> the selected records.
Reader = Callable[[str, str], list[dict[str, Any]]]


def _with_jobs(
    repo: str, runs: list[dict[str, Any]], read: Reader = _gh_lines
) -> list[dict[str, Any]]:
    """Each run's jobs, each FAILED shard job's check-run annotations (the only mark that
    separates a wall-budget TIMEOUT from a test red), and each PASSED gate's annotations
    (the vendors whose shards the run owes). `read` is `gh api`; the tests replay captured
    responses through it by request path."""
    for run in runs:
        run["jobs"] = read(
            f"repos/{repo}/actions/runs/{run['id']}/jobs?filter=all&per_page=100", ".jobs[]"
        )
        for job in run["jobs"]:
            is_shard = VENDOR_JOB.fullmatch(job.get("name", "")) or BASELINE_JOB.fullmatch(
                job.get("name", "")
            )
            # A passing gate's annotation names the vendor shards the run owes.
            is_passed_gate = job.get("name") == GATE_JOB and job.get("conclusion") == "success"
            if (is_shard and job.get("conclusion") == "failure") or is_passed_gate:
                job["annotations"] = read(
                    f"repos/{repo}/check-runs/{job['id']}/annotations?per_page=100", ".[]"
                )
    return runs


def fetch_canary_runs(repo: str) -> list[dict[str, Any]]:
    workflow = Path(CANARY_WORKFLOW).name
    runs = _gh_lines(
        f"repos/{repo}/actions/workflows/{workflow}/runs?per_page=100", ".workflow_runs[]"
    )
    return _with_jobs(repo, runs)


def vendor_rows_from_targets(
    runs_by_repo: dict[str, list[dict[str, Any]]], config: dict[str, Any]
) -> list[ShardRow]:
    """Each vendor's shard rows from its OWN target repository, and from nowhere else."""
    rows = []
    for repo, runs in runs_by_repo.items():
        rows += [
            row
            for row in shard_jobs(
                runs,
                side="vendor",
                pytest_step_name=config["pytest_step_name"],
                timeout_minutes=config["shard_timeout_minutes"],
                shard_count=config["shard_count"],
            )
            if row.vendor in config["vendors"]
            and config["vendors"][row.vendor]["target_repo"] == repo
        ]
    return rows


def fetch_baseline_runs(
    source: str, baseline: dict[str, str], shas: Iterable[str]
) -> list[dict[str, Any]]:
    workflow = Path(baseline["workflow_path"]).name
    runs = []
    for sha in sorted(set(shas)):
        found = _gh_lines(
            f"repos/{source}/actions/workflows/{workflow}/runs?head_sha={sha}"
            f"&event={baseline['event']}&branch={baseline['branch']}&per_page=100",
            ".workflow_runs[]",
        )
        runs.extend(found)
    return _with_jobs(source, runs)


# ----- CLI ------------------------------------------------------------------------


def _format(report: VendorReport) -> str:
    def num(value: float | None, fmt: str) -> str:
        return "unmeasured" if value is None else format(value, fmt)

    lines = [
        f"### {report.vendor}: {report.verdict}",
        "",
        "| metric | value |",
        "|:--|--:|",
        f"| paired commits | {report.paired_commits} |",
        f"| vendor shard jobs (every finished one) | {report.shard_jobs} |",
        "| push-to-verdict ratio (vendor / self-hosted medians) | "
        f"{num(report.verdict_latency_ratio, '.3f')} |",
        f"| p95 start latency (s) | {num(report.p95_queue_wait_s, 'g')} |",
        f"| infra failures per 100 jobs | {num(report.infra_failures_per_100, '.2f')} |",
        f"| outcome agreement | {num(report.outcome_agreement, '.3f')} |",
        "",
    ]
    lines.extend(f"- {reason}" for reason in report.reasons)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--vendor", action="append", default=[], help="limit to a vendor (repeatable)"
    )
    parser.add_argument("--known-red-sha", action="append", default=[], help="add a known-red SHA")
    parser.add_argument("--json", type=Path, help="also write the reports as JSON here")
    args = parser.parse_args(argv)
    config = json.loads(args.config.read_text())
    vendors = args.vendor or list(config["vendors"])
    unknown_vendors = sorted(set(vendors) - set(config["vendors"]))
    if unknown_vendors:
        parser.error(f"unknown vendor(s) {unknown_vendors}")
    known_red = [*config["known_red_shas"], *args.known_red_sha]
    targets = sorted({config["vendors"][v]["target_repo"] for v in vendors})
    try:
        runs_by_repo = {repo: fetch_canary_runs(repo) for repo in targets}
        baseline_runs = fetch_baseline_runs(
            config["source_repository"],
            config["baseline"],
            (r["head_sha"] for runs in runs_by_repo.values() for r in runs),
        )
        vendor_rows = vendor_rows_from_targets(runs_by_repo, config)
        # ci.yml's shard cap. A baseline cancelled at it is infra, as on the vendor side.
        baseline_rows = shard_jobs(
            baseline_runs,
            side="baseline",
            pytest_step_name=config["pytest_step_name"],
            timeout_minutes=60,
            shard_count=config["shard_count"],
        )
    except ReadFailed as exc:
        print(f"UNKNOWN: the GitHub read failed, so nothing was measured: {exc}")
        return EXIT_UNKNOWN
    reports = [
        evaluate_vendor(
            vendor,
            vendor_rows,
            baseline_rows,
            bars=config["success_bars"],
            known_red_shas=known_red,
            shard_count=config["shard_count"],
        )
        for vendor in vendors
    ]
    print("\n\n".join(_format(r) for r in reports))
    if args.json:
        args.json.write_text(json.dumps([asdict(r) for r in reports], indent=2, default=str))
    verdicts = {r.verdict for r in reports}
    if "FAIL" in verdicts:
        return EXIT_FAIL
    if "UNKNOWN" in verdicts:
        return EXIT_UNKNOWN
    return EXIT_PASS


if __name__ == "__main__":
    sys.exit(main())
