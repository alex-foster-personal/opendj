#!/usr/bin/env python3
"""One scheduled pass over every workflow completion since a high-water mark.

The Actions queue is per JOB. A `workflow_run` follower that fires on every
completion holds one queued job per completion ahead of PR CI whenever the
pool is saturated (issue #2196). A follower that instead runs once per cadence
and lists the completions since the previous pass costs one job per cadence
whatever the completion rate. The pieces every such follower shares live here:
the mark, its floor, and the paginated listing.

The mark lives nowhere but GitHub's own record of the follower's runs
(`run_started_at` of its last SUCCESSFUL pass, `last_successful_pass_start`).
The floor makes two passes overlap even if a pass is late; every follower must
make re-applying an overlap harmless on its own terms.

    python3 -m scripts.ci_run_batch mark --repository o/r --workflow-file f.yml --this-run N
    python3 -m scripts.ci_run_batch hold --repository o/r --watched "CI,E2E" --lookback-hours 6
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.request import Request, urlopen


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def headers(token: str, agent: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": f"music-dj-tools-{agent}",
    }


def batch_since(previous_started: str | None, now: datetime, floor: timedelta) -> str:
    """The high-water mark: the previous pass's start, never later than `now - floor`."""
    floored = now - floor
    if previous_started is None:
        return iso(floored)
    return iso(min(parse_time(previous_started), floored))


def created_floor(since: str, lookback: timedelta) -> str:
    """The listing filters on created_at and the selection on updated_at; a run
    completes at most `lookback` after it was created."""
    return iso(parse_time(since) - lookback)


RESULT_CAP = 1000


def created_slices(created_since: str, now: datetime, width: timedelta) -> list[tuple[str, str]]:
    """Consecutive creation windows from `created_since` to `now`, each `width` wide."""
    slices: list[tuple[str, str]] = []
    start = parse_time(created_since)
    while start < now:
        stop = min(start + width, now)
        slices.append((iso(start), iso(stop)))
        start = stop
    return slices


def _get_json(url: str, token: str, agent: str) -> dict[str, Any]:
    request = Request(url, headers=headers(token, agent))
    with urlopen(request, timeout=30) as response:
        payload: dict[str, Any] = json.load(response)
    return payload


def fetch_completed_runs(
    repository: str,
    created_since: str,
    token: str,
    agent: str,
    *,
    now: datetime | None = None,
    slice_width: timedelta = timedelta(hours=1),
    get_json: Callable[[str], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Completed runs created at or after `created_since`, every page of every slice.

    GitHub caps a filtered runs listing at 1,000 results, the newest; one query
    over a window wider than that silently drops the oldest runs, and with them
    a long-running completion the mark will step past (Codex P1 on #3844). So
    the window is listed one creation slice at a time, and a slice that reaches
    the cap fails closed: a red pass re-reads the window, a lost record never
    comes back. The boundary second belongs to both slices; runs are deduped
    by id.
    """
    fetch = get_json or (lambda url: _get_json(url, token, agent))
    seen: dict[int, dict[str, Any]] = {}
    for start, stop in created_slices(created_since, now or datetime.now(UTC), slice_width):
        listed = 0
        page = 1
        while True:
            payload = fetch(
                f"https://api.github.com/repos/{repository}/actions/runs"
                f"?status=completed&per_page=100&page={page}&created={start}..{stop}"
            )
            batch = payload.get("workflow_runs") or []
            listed += len(batch)
            for run in batch:
                seen[int(run["id"])] = run
            if listed >= RESULT_CAP:
                raise RuntimeError(
                    f"creation slice {start}..{stop} reached GitHub's {RESULT_CAP}-result "
                    "cap; the listing is truncated, narrow the slice"
                )
            if len(batch) < 100:
                break
            page += 1
    return list(seen.values())


def last_successful_pass_start(
    repository: str,
    workflow_file: str,
    this_run: int,
    *,
    token: str = "",
    agent: str = "ci-run-batch",
    get_json: Callable[[str], dict[str, Any]] | None = None,
) -> str:
    """The start of the follower's last successful pass, however far back it is.

    A failed pass may have written none of its batch, so it never moves the mark, and the
    search has no fixed window: a run of failures longer than any window would otherwise
    skip what they missed (Codex P1s on #3844). The listing carries no `status` or
    `created` filter, because GitHub stops a filtered listing at 1,000 results; in-flight
    runs are skipped here instead. With no success in the whole history the mark is the
    oldest pass, which covers everything; with no passes at all it is empty and the caller
    uses the floor. The job's timeout is what bounds the search.
    """
    fetch = get_json or (lambda url: _get_json(url, token, agent))
    oldest = ""
    page = 1
    while True:
        runs = fetch(
            f"https://api.github.com/repos/{repository}/actions/workflows/{workflow_file}"
            f"/runs?per_page=100&page={page}"
        ).get("workflow_runs") or []
        for run in runs:
            if int(run["id"]) == this_run or run.get("status") != "completed":
                continue
            if run["conclusion"] == "success":
                return str(run["run_started_at"])
            oldest = str(run["run_started_at"])
        if len(runs) < 100:
            return oldest
        page += 1


INFLIGHT_STATUSES = ("requested", "waiting", "pending", "queued", "in_progress")


def fetch_inflight_runs(
    repository: str,
    token: str,
    agent: str,
    *,
    get_json: Callable[[str], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Every run not yet completed, each status listed in full; fails closed at the cap."""
    fetch = get_json or (lambda url: _get_json(url, token, agent))
    seen: dict[int, dict[str, Any]] = {}
    for status in INFLIGHT_STATUSES:
        listed = 0
        page = 1
        while True:
            batch = fetch(
                f"https://api.github.com/repos/{repository}/actions/runs"
                f"?status={status}&per_page=100&page={page}"
            ).get("workflow_runs") or []
            listed += len(batch)
            seen.update((int(run["id"]), run) for run in batch)
            if listed >= RESULT_CAP:
                raise RuntimeError(f"{status} runs reached GitHub's {RESULT_CAP}-result cap")
            if len(batch) < 100:
                break
            page += 1
    return list(seen.values())


def runs_held_back(
    inflight: list[dict[str, Any]], watched: set[str], now: datetime, lookback: timedelta
) -> list[dict[str, Any]]:
    """Watched runs still in flight that were created before `now - lookback`.

    The listing filters on created_at, so the next pass cannot see a run created before
    its floor; a pass that succeeded now would move the mark past that run's completion
    for good (Codex P1 on #3844: a job queued on a saturated pool is bounded by no
    timeout). A pass that finds one fails instead, the mark stays put, and the window
    keeps the run in view until it completes.
    """
    cutoff = now - lookback
    return sorted(
        (
            run
            for run in inflight
            if run.get("name") in watched and parse_time(str(run["created_at"])) < cutoff
        ),
        key=lambda run: int(run["id"]),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    mark = commands.add_parser("mark", help="print the last successful pass's start")
    mark.add_argument("--repository", required=True)
    mark.add_argument("--workflow-file", required=True)
    mark.add_argument("--this-run", type=int, required=True)
    hold = commands.add_parser("hold", help="fail while a watched run outlives the lookback")
    hold.add_argument("--repository", required=True)
    hold.add_argument("--watched", required=True, help="comma-separated workflow names")
    hold.add_argument("--lookback-hours", type=int, required=True)
    args = parser.parse_args(argv)
    token = os.environ["GITHUB_TOKEN"]
    if args.command == "mark":
        start = last_successful_pass_start(
            args.repository, args.workflow_file, args.this_run, token=token
        )
        print(start)
        return 0
    if args.command == "hold":
        watched = {name.strip() for name in args.watched.split(",") if name.strip()}
        inflight = fetch_inflight_runs(args.repository, token, "ci-run-batch")
        held = runs_held_back(
            inflight, watched, datetime.now(UTC), timedelta(hours=args.lookback_hours)
        )
        for run in held:
            print(
                f"::error::run {run['id']} ({run['name']}, {run['status']}) was created "
                f"{run['created_at']}, before the {args.lookback_hours}h lookback; this pass "
                "fails so the mark stays and the next pass still lists it"
            )
        print(f"[hold] in_flight={len(inflight)} held={len(held)}")
        return 1 if held else 0
    raise AssertionError(f"unhandled command {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
