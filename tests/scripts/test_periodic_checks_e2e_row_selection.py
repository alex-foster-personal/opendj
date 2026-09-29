"""Regression guard for the periodic-checks e2e-row selection in
``.github/workflows/periodic-checks.yml`` (DEVOPS-05).

Runs the workflow's OWN `e2e_line` selection script (extracted from the
file, not a copy) against a stubbed `gh` on PATH, so a future edit that
drifts from what actually executes cannot pass silently. See review finding
r3974766506: without `--status completed`, `--limit 1` can select the
04:17 scheduled run while it is still queued or running, whose null
conclusion rendered as "in progress" -- and the NEXT window's `--limit 1`
then moves on to a newer scheduled run and never revisits it. A run that
later fails after being reported "in progress" was never seen again by any
later window, exactly the silent-failure shape this row exists to catch.

Also r3975092357: a fixed 48h `--created` floor assumed windows always run
within 48h of each other, but the `window` job's own weekly-floor OR-arm
(PR #1494) lets windows go up to 7 days apart, so a fixed 48h floor left
every night before the last 2 uncovered whenever a quiet week widened that
gap. The floor now anchors to the previous window's own timestamp
(`BASE_AT`), falling back to 48h only on a genuine bootstrap window with no
prior report to anchor to.

Also r3975509887: `gh run list` only ever exposes the WHOLE RUN's
conclusion, but e2e.yml's `extended` job runs `needs: gate` /
`if: !cancelled()` -- ordered after `gate`, not gated on its success, so it
can PASS a night `gate` FAILS (or vice versa). The row must report the
`extended` job's OWN conclusion, fetched via a separate `gh api
.../actions/runs/<id>/jobs` call per candidate run, not the run-level
conclusion `gh run list` returns.

The stub `gh` does not re-implement GitHub's own server-side filtering
(that is GitHub's contract, not this repo's); it logs its argv so a test can
assert `--status completed` was actually requested, then pipes a
test-supplied JSON payload -- standing in for whatever GitHub already
filtered server-side -- through the REAL `--jq` filter text extracted from
the workflow, so the downstream formatting is exercised for real rather
than asserted against a hand-typed expected string.

The snippet runs on a Linux runner and calls GNU `date -d`, which BSD `date`
(macOS) rejects. Each run therefore gets a `date` on PATH that execs a
resolved GNU date (`date` itself on Linux, Homebrew `gdate` on macOS); a host
with neither SKIPS the whole module as UNAVAILABLE rather than letting the
snippet's floor silently come out empty.

The bootstrap 48h floor reads the wall clock, so no value computed outside
the snippet can equal it exactly: a second boundary between the two reads
makes them differ by 1s (seen on PR #4221). Those tests bracket the snippet
between two real clock reads instead of recomputing the floor a second time.

- [if] the bootstrap floor is not 48h before the run [then] broken, [else stop].
- [if] a second boundary between the test's clock read and the snippet's fails a correct floor [then] broken, [else stop].
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "periodic-checks.yml"

EXTENDED_JOB = "e2e extended (nightly)"

BOOTSTRAP_FLOOR = timedelta(hours=48)


def _resolve_gnu_date() -> str | None:
    """Path to a GNU coreutils `date`, or None when this host has none."""
    for name in ("date", "gdate"):
        path = shutil.which(name)
        if path is None:
            continue
        version = subprocess.run([path, "--version"], capture_output=True, text=True, check=False)
        if "GNU coreutils" in version.stdout:
            return path
    return None


GNU_DATE = _resolve_gnu_date()

pytestmark = pytest.mark.skipif(
    GNU_DATE is None,
    reason=(
        "UNAVAILABLE: the periodic-checks e2e snippet needs GNU `date -d` (it runs on a "
        "Linux runner); this host has neither GNU date nor gdate (brew install coreutils)"
    ),
)

# Execs GNU date unchanged, so the snippet sees the Linux runner's `date`.
_DATE_SHIM = """#!/usr/bin/env bash
exec "{gnu_date}" "$@"
"""

# Sleeps to the next whole second first, so a clock read taken by the test
# BEFORE the run always lands in an earlier second than the snippet's read.
_DATE_SHIM_CROSSING_A_SECOND = """#!/usr/bin/env bash
ns="$("{gnu_date}" +%N)"
left=$((1000000000 - 10#$ns))
sleep "$((left / 1000000000)).$(printf '%09d' $((left % 1000000000)))"
exec "{gnu_date}" "$@"
"""


def _extract_e2e_line_script() -> str:
    """Pull the `e2e_created_floor=...` through the `e2e_line=` selection
    block out of the `report` job's `run:` script. Delimited by the blank
    line before `window_desc=` rather than by the block's own internal
    `if`/`while` shape, so the extraction survives that shape changing."""
    text = WORKFLOW.read_text(encoding="utf-8")
    match = re.search(
        r'( *e2e_created_floor=.*?)(?=\n *window_desc=)',
        text,
        re.DOTALL,
    )
    assert match, "could not locate the e2e_line selection block in periodic-checks.yml"
    return match.group(1)


_STUB_GH = """#!/usr/bin/env bash
echo "$@" >> "$STUB_LOG"
if [ "$1" = "run" ]; then
  if [ -n "${STUB_FAIL:-}" ]; then
    echo "$STUB_FAIL" >&2
    exit 1
  fi
  printf '%s' "$STUB_JSON"
elif [ "$1" = "api" ]; then
  run_id="$(printf '%s' "$2" | grep -oE '[0-9]+' | head -1)"
  if [ "${STUB_JOBS_FAIL_ID:-}" = "$run_id" ]; then
    echo "job query failed for run $run_id" >&2
    exit 1
  fi
  jq_filter=""
  prev=""
  for a in "$@"; do
    if [ "$prev" = "--jq" ]; then jq_filter="$a"; fi
    prev="$a"
  done
  var="STUB_JOBS_$run_id"
  payload="${!var}"
  [ -n "$payload" ] || payload='{"jobs":[]}'
  printf '%s' "$payload" | jq -r "$jq_filter"
else
  echo "unexpected gh invocation: $*" >&2
  exit 1
fi
"""


def _run_with_stub_gh(
    tmp_path: Path,
    *,
    runs: list[dict] | None = None,
    fail: str | None = None,
    jobs_fail_id: str | None = None,
    base_at: str | None = None,
    date_shim: str = _DATE_SHIM,
) -> tuple[str, str]:
    """Run the extracted script against the stub `gh` above and return
    ``(e2e_line, argv_log)``.

    ``runs`` is a list of dicts shaped like what a candidate scheduled run
    contributes: ``databaseId``, ``createdAt``, ``url``, and either
    ``job_conclusion`` (the `extended` job's own conclusion for that run) or
    ``job_missing=True`` (the jobs listing has no `extended` job at all).
    """
    stub = tmp_path / "gh"
    stub.write_text(_STUB_GH, encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    date = tmp_path / "date"
    date.write_text(date_shim.format(gnu_date=GNU_DATE), encoding="utf-8")
    date.chmod(date.stat().st_mode | stat.S_IEXEC)
    script = f'{_extract_e2e_line_script()}\nprintf "%s" "$e2e_line"\n'
    env = dict(os.environ)
    env["PATH"] = f"{tmp_path}:{env['PATH']}"
    env["STUB_LOG"] = str(tmp_path / "gh.argv")

    runs = runs if runs is not None else []
    run_list_payload = [
        {"databaseId": r["databaseId"], "createdAt": r["createdAt"], "url": r["url"]} for r in runs
    ]
    env["STUB_JSON"] = json.dumps(run_list_payload)
    for r in runs:
        jobs_payload: dict = {"jobs": []}
        if not r.get("job_missing"):
            jobs_payload = {"jobs": [{"name": EXTENDED_JOB, "conclusion": r["job_conclusion"]}]}
        env[f"STUB_JOBS_{r['databaseId']}"] = json.dumps(jobs_payload)

    if fail is not None:
        env["STUB_FAIL"] = fail
    if jobs_fail_id is not None:
        env["STUB_JOBS_FAIL_ID"] = jobs_fail_id
    if base_at is not None:
        env["BASE_AT"] = base_at
    else:
        env.pop("BASE_AT", None)
    proc = subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    argv_path = tmp_path / "gh.argv"
    argv_log = argv_path.read_text(encoding="utf-8") if argv_path.exists() else ""
    return proc.stdout, argv_log


def test_the_query_restricts_to_completed_runs(tmp_path: Path) -> None:
    """The stub `gh` must be invoked with `--status completed`: without it,
    an in-flight 04:17 run can be selected and then abandoned once a later
    window's `--limit 1` moves on to a newer scheduled run."""
    _, argv = _run_with_stub_gh(tmp_path, runs=[])
    assert "--status completed" in argv, argv


def test_the_query_fetches_more_than_the_single_newest_run(tmp_path: Path) -> None:
    """r3974912528: the stub `gh` does not re-implement GitHub's own
    `--limit` filtering (see module docstring), so the newest-failure-wins
    behavior alone cannot catch a regression back to `--limit 1` -- a
    payload-based test would still pass no matter which value is on the
    command line, since the stub returns whatever payload the test hands it
    regardless of `--limit`. Asserting the argv directly is what actually
    guards it: `--limit 1` can only ever see ONE completed run per window,
    so an older failure sitting behind a newer pass would never even be
    fetched, let alone compared. 30, not 10 (r3975092357): once the floor
    itself can reach back a full 7-day weekly-floor gap instead of a fixed
    48h, `--limit 10` is no longer generous enough to fetch every nightly
    run the wider floor can now match."""
    _, argv = _run_with_stub_gh(tmp_path, runs=[])
    assert "--limit 30" in argv, argv


def test_the_floor_uses_the_previous_windows_own_timestamp_when_available(
    tmp_path: Path,
) -> None:
    """r3975092357: a fixed 48h floor assumed windows are always <= 48h
    apart, but this workflow's own cadence can go up to 7 DAYS between
    windows (the weekly floor OR-arm in the `window` job, PR #1494) -- a
    nightly failure more than 48h before a window that only fires after a
    quiet week, followed by a later pass, aged out unseen by every window
    that ever looked at it. The floor must anchor to the PREVIOUS window's
    own timestamp (`BASE_AT`), covering the exact gap since the last report
    with no assumption about how wide that gap can get."""
    _, argv = _run_with_stub_gh(tmp_path, runs=[], base_at="2026-08-30T05:40:00Z")
    assert "--created >=2026-08-30T05:40:00Z" in argv, argv


def _floor_second(now: datetime) -> datetime:
    """The 48h floor for a clock read, truncated to whole seconds as `+%FT%TZ` is."""
    return (now - BOOTSTRAP_FLOOR).replace(microsecond=0)


def _bootstrap_floor_bracket(tmp_path: Path, date_shim: str) -> tuple[datetime, datetime, datetime]:
    """Run a bootstrap window and return ``(earliest, floor, latest)``: the
    floor the snippet passed to `gh run list`, and the 48h floors of two real
    clock reads taken just before and just after the run."""
    before = datetime.now(UTC)
    _, argv = _run_with_stub_gh(tmp_path, runs=[], base_at=None, date_shim=date_shim)
    after = datetime.now(UTC)
    match = re.search(r"--created >=(\S+)", argv)
    assert match, f"no --created floor in the gh argv: {argv!r}"
    floor = datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    return _floor_second(before), floor, _floor_second(after)


def test_a_bootstrap_window_with_no_prior_report_falls_back_to_a_48h_floor(
    tmp_path: Path,
) -> None:
    """OPPOSITE DIRECTION: `BASE_AT` is only ever empty together with `BASE`
    on a genuine bootstrap window (no prior report exists to anchor to), and
    a 48h floor is a reasonable first measurement with nothing before it to
    miss. Anchoring on an empty string instead of falling back is not caught
    by a bare "some --created value is present" check: GNU `date -d ""`
    does not error, it silently parses empty as TODAY at midnight, a much
    narrower and wrong floor.

    The floor reads the wall clock, so it is pinned between the 48h floors of
    two real clock reads that bracket the run, not compared with a floor
    recomputed at a different instant (that raced a second boundary on PR
    #4221). The bracket spans the run's own duration, so a 24h or 72h floor,
    or today's midnight, still falls outside it.

    [if] the bootstrap floor is not 48h before the run [then] broken, [else stop].
    """
    earliest, floor, latest = _bootstrap_floor_bracket(tmp_path, _DATE_SHIM)
    assert earliest <= floor <= latest, (earliest, floor, latest)


def test_the_bootstrap_floor_survives_a_second_boundary_during_the_run(
    tmp_path: Path,
) -> None:
    """The flake on PR #4221: the old test recomputed the 48h floor with its
    own `date` call before the run, and a second boundary between that call
    and the snippet's own read failed a correct floor by 1s. The date shim
    here sleeps to the next whole second before the snippet reads the clock,
    so that boundary is crossed on every run, not by chance.

    [if] a second boundary between the test's clock read and the snippet's fails a correct floor [then] broken, [else stop].
    """
    earliest, floor, latest = _bootstrap_floor_bracket(tmp_path, _DATE_SHIM_CROSSING_A_SECOND)
    assert floor > earliest, (
        "the shim did not move the snippet's clock read past the test's, so this "
        f"test did not exercise a second boundary: {earliest} vs {floor}"
    )
    assert earliest <= floor <= latest, (earliest, floor, latest)


def test_a_completed_runs_conclusion_is_reported_verbatim(tmp_path: Path) -> None:
    """The ordinary case: GitHub's own `--status completed` filtering has
    already dropped a still-running newer run, leaving the last COMPLETED
    one -- its real `extended`-job conclusion, not a stale "in progress"
    placeholder, ends up in `e2e_line`."""
    e2e_line, _ = _run_with_stub_gh(
        tmp_path,
        runs=[
            {
                "databaseId": 1,
                "createdAt": "2026-09-08T04:17:00Z",
                "url": "https://example/runs/1",
                "job_conclusion": "failure",
            }
        ],
    )
    assert e2e_line == "failure (2026-09-08T04:17:00Z) https://example/runs/1"


def test_the_extended_jobs_own_conclusion_wins_over_the_run_level_conclusion(
    tmp_path: Path,
) -> None:
    """r3975509887: e2e.yml's `extended` job runs `needs: gate` /
    `if: !cancelled()`, so it can complete and PASS on a night the overall
    run (and `gate`) FAILED. The row exists to watch the extended suite, not
    `gate`, so it must report the extended job's own conclusion here:
    success, even though `gh run list` would have reported this run's
    OVERALL conclusion as failure."""
    e2e_line, argv = _run_with_stub_gh(
        tmp_path,
        runs=[
            {
                "databaseId": 42,
                "createdAt": "2026-09-08T04:17:00Z",
                "url": "https://example/runs/42",
                "job_conclusion": "success",
            }
        ],
    )
    assert e2e_line == "success (2026-09-08T04:17:00Z) https://example/runs/42"
    assert "actions/runs/42/jobs" in argv, argv


def test_a_failing_extended_job_is_reported_even_when_the_run_overall_passed(
    tmp_path: Path,
) -> None:
    """OPPOSITE DIRECTION of the above: a run can complete with `gate`
    passing (and thus the WHOLE RUN reported as a pass by `gh run list`)
    while `extended` itself failed. The row must surface that failure, not
    the run-level pass, or a genuinely broken nightly suite reads as green
    forever."""
    e2e_line, _ = _run_with_stub_gh(
        tmp_path,
        runs=[
            {
                "databaseId": 7,
                "createdAt": "2026-09-08T04:17:00Z",
                "url": "https://example/runs/7",
                "job_conclusion": "failure",
            }
        ],
    )
    assert e2e_line == "failure (2026-09-08T04:17:00Z) https://example/runs/7"


def test_a_run_with_no_extended_job_at_all_is_not_read_as_a_pass(tmp_path: Path) -> None:
    """A candidate run whose jobs listing has no `extended` job (e.g. a run
    predating that job's addition) must not silently render as a pass by
    virtue of an empty/missing conclusion -- absence is reported as
    "MISSING", which the same fails-filter below treats as a failure, not a
    success."""
    e2e_line, _ = _run_with_stub_gh(
        tmp_path,
        runs=[
            {
                "databaseId": 9,
                "createdAt": "2026-09-08T04:17:00Z",
                "url": "https://example/runs/9",
                "job_missing": True,
            }
        ],
    )
    assert e2e_line == "MISSING (2026-09-08T04:17:00Z) https://example/runs/9"


def test_an_older_failure_is_not_masked_by_a_newer_pass(tmp_path: Path) -> None:
    """r3974912528: two nightly runs can both complete between two windows
    (night N was still running at the previous window's start, so it was
    skipped; night N+1 finishes before the next window runs). A newest-only
    selection would report N+1's PASS and never surface N's FAILURE. The
    newest FAILURE within the floor must win over a newer PASS, or a real
    failure silently ages out of the ledger the moment anything later
    passes."""
    e2e_line, _ = _run_with_stub_gh(
        tmp_path,
        runs=[
            {
                "databaseId": 1,
                "createdAt": "2026-09-08T04:17:00Z",
                "url": "https://example/runs/1",
                "job_conclusion": "failure",
            },
            {
                "databaseId": 2,
                "createdAt": "2026-09-09T04:17:00Z",
                "url": "https://example/runs/2",
                "job_conclusion": "success",
            },
        ],
    )
    assert e2e_line == "failure (2026-09-08T04:17:00Z) https://example/runs/1"


def test_multiple_passes_report_the_newest_one(tmp_path: Path) -> None:
    """OPPOSITE DIRECTION: with no failure anywhere in the floor, the row
    must still report the MOST RECENT completed run, not the oldest --
    preferring a failure over a later pass must not accidentally flip the
    ordering when every run passed."""
    e2e_line, _ = _run_with_stub_gh(
        tmp_path,
        runs=[
            {
                "databaseId": 1,
                "createdAt": "2026-09-08T04:17:00Z",
                "url": "https://example/runs/1",
                "job_conclusion": "success",
            },
            {
                "databaseId": 2,
                "createdAt": "2026-09-09T04:17:00Z",
                "url": "https://example/runs/2",
                "job_conclusion": "success",
            },
        ],
    )
    assert e2e_line == "success (2026-09-09T04:17:00Z) https://example/runs/2"


def test_no_completed_runs_is_reported_as_such_not_as_a_pass(tmp_path: Path) -> None:
    """OPPOSITE DIRECTION: zero completed runs within the floor (every
    scheduled run in the window is still in flight, or none exist) must
    render as an explicit "not measured yet" string, never as an empty
    e2e_line that a careless reader could mistake for a blank pass."""
    e2e_line, _ = _run_with_stub_gh(tmp_path, runs=[])
    assert "NO COMPLETED SCHEDULED RUNS FOUND" in e2e_line


def test_a_failed_run_list_query_is_reported_as_a_failed_measurement(tmp_path: Path) -> None:
    """OPPOSITE DIRECTION again: `gh run list` itself failing (rate limit,
    auth) must not silently read as "no runs" -- it is a FAILED
    measurement, not a finding."""
    e2e_line, _ = _run_with_stub_gh(tmp_path, fail="boom")
    assert "QUERY FAILED" in e2e_line


def test_a_failed_jobs_query_is_reported_as_a_failed_measurement(tmp_path: Path) -> None:
    """A candidate run's own `gh api .../jobs` call failing (rate limit,
    auth, a deleted run) must ALSO be a FAILED measurement, not silently
    treated as "no extended job" (which would misreport as MISSING/failure)
    nor silently skipped (which would misreport by falling through to
    whatever other candidate happens to remain)."""
    e2e_line, _ = _run_with_stub_gh(
        tmp_path,
        runs=[
            {
                "databaseId": 5,
                "createdAt": "2026-09-08T04:17:00Z",
                "url": "https://example/runs/5",
                "job_conclusion": "success",
            }
        ],
        jobs_fail_id="5",
    )
    assert "QUERY FAILED" in e2e_line
