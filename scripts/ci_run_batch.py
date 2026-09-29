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
    python3 -m scripts.ci_run_batch census --repository o/r --watched "CI,E2E" --lookback-hours 6
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable, Iterable
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


PAGE_SIZE = 100


def fetch_completed_runs(
    repository: str,
    created_since: str,
    token: str,
    agent: str,
    *,
    workflow_names: Iterable[str],
    now: datetime | None = None,
    slice_width: timedelta = timedelta(hours=1),
    get_json: Callable[[str], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Completed runs of the named workflows created at or after `created_since`.

    Every listing is ONE page. Offset pagination over the completed set is not a
    snapshot: a run re-run while later pages are read leaves the set, shifts a later
    run into the page already read, and that run is skipped with nothing to say so
    (Codex P1 on #3844). So each workflow is listed one creation slice at a time, and
    a slice that fills a page is bisected and listed again rather than paged, down to
    one second; only a one-second slice holding a full page fails the pass. Listing
    per watched workflow instead of repo-wide reads about a tenth of the runs.
    Boundary seconds belong to both neighbours; runs are deduped by id.
    """
    fetch = get_json or (lambda url: _get_json(url, token, agent))
    base = f"https://api.github.com/repos/{repository}/actions/workflows"
    seen: dict[str, dict[str, Any]] = {}
    for name, workflow_id in sorted(_workflow_ids(base, set(workflow_names), fetch).items()):
        pending = list(
            reversed(created_slices(created_since, now or datetime.now(UTC), slice_width))
        )
        while pending:
            start, stop = pending.pop()
            batch = (
                fetch(
                    f"{base}/{workflow_id}/runs?status=completed&per_page={PAGE_SIZE}"
                    f"&created={start}..{stop}"
                ).get("workflow_runs")
                or []
            )
            if len(batch) < PAGE_SIZE:
                seen.update((str(run["id"]), run) for run in batch)
                continue
            width = parse_time(stop) - parse_time(start)
            if width <= timedelta(seconds=1):
                raise RuntimeError(
                    f"{name}: creation slice {start}..{stop} fills a page and cannot be "
                    "narrowed below one second; the listing would need a second page"
                )
            middle = iso(parse_time(start) + timedelta(seconds=width.total_seconds() // 2))
            pending.extend([(middle, stop), (start, middle)])
    return list(seen.values())


def _workflow_ids(
    base: str, names: set[str], fetch: Callable[[str], dict[str, Any]]
) -> dict[str, int]:
    """Each watched name's workflow id. A name with no workflow, or with two, fails the
    pass: an unmatched name would read nothing and look clean."""
    found: dict[str, list[int]] = {}
    page = 1
    while True:
        workflows = fetch(f"{base}?per_page={PAGE_SIZE}&page={page}").get("workflows") or []
        for workflow in workflows:
            found.setdefault(str(workflow["name"]), []).append(int(workflow["id"]))
        if len(workflows) < PAGE_SIZE:
            break
        page += 1
    unmatched = sorted(name for name in names if len(found.get(name, [])) != 1)
    if unmatched:
        raise RuntimeError(f"watched workflow names without exactly one workflow: {unmatched}")
    return {name: found[name][0] for name in names}


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
        runs = (
            fetch(
                f"https://api.github.com/repos/{repository}/actions/workflows/{workflow_file}"
                f"/runs?per_page=100&page={page}"
            ).get("workflow_runs")
            or []
        )
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
    workflow_names: Iterable[str],
    created_before: str,
    get_json: Callable[[str], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Every run of the named workflows not yet completed and created before
    `created_before`.

    One page per workflow and status, for the same reason the completed listing is one
    page per slice: paging a set that changes under the reader can skip a member. Only
    watched runs older than the cutoff matter to the census, and those are few; a page
    that fills fails the pass rather than reading a second one. Listing per watched
    workflow keeps old runs of unwatched workflows from filling that page (Sol P1 on
    #3844: a repo-wide page full of them would wedge every pass).
    """
    fetch = get_json or (lambda url: _get_json(url, token, agent))
    base = f"https://api.github.com/repos/{repository}/actions/workflows"
    seen: dict[str, dict[str, Any]] = {}
    for name, workflow_id in sorted(_workflow_ids(base, set(workflow_names), fetch).items()):
        for status in INFLIGHT_STATUSES:
            batch = (
                fetch(
                    f"{base}/{workflow_id}/runs"
                    f"?status={status}&per_page={PAGE_SIZE}&created=<{created_before}"
                ).get("workflow_runs")
                or []
            )
            if len(batch) >= PAGE_SIZE:
                raise RuntimeError(
                    f"{PAGE_SIZE} or more {status} runs of {name!r} were created before "
                    f"{created_before}; the census would need a second page"
                )
            seen.update((str(run["id"]), run) for run in batch)
    return list(seen.values())


def runs_held_back(
    inflight: list[dict[str, Any]], watched: set[str], now: datetime, lookback: timedelta
) -> list[dict[str, Any]]:
    """Watched runs still in flight that were created before `now - lookback`.

    The listing filters on created_at, so the next pass cannot see a run created before
    its floor; a pass that succeeded now would move the mark past that run's completion
    for good (Codex P1 on #3844: a job queued on a saturated pool is bounded by no
    timeout). A pass that finds one fails instead, the mark stays put, and the window
    keeps the run in view until it completes. The census is taken BEFORE the completed
    listing: a run that completes between the two is then in one or the other, never in
    neither (Codex and Sol P1s on #3844).
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
    census = commands.add_parser(
        "census", help="count watched runs in flight since before the lookback (held=N)"
    )
    census.add_argument("--repository", required=True)
    census.add_argument("--watched", required=True, help="comma-separated workflow names")
    census.add_argument("--lookback-hours", type=int, required=True)
    args = parser.parse_args(argv)
    token = os.environ["GITHUB_TOKEN"]
    if args.command == "mark":
        start = last_successful_pass_start(
            args.repository, args.workflow_file, args.this_run, token=token
        )
        print(start)
        return 0
    if args.command == "census":
        watched = {name.strip() for name in args.watched.split(",") if name.strip()}
        now = datetime.now(UTC)
        lookback = timedelta(hours=args.lookback_hours)
        inflight = fetch_inflight_runs(
            args.repository,
            token,
            "ci-run-batch",
            workflow_names=watched,
            created_before=iso(now - lookback),
        )
        held = runs_held_back(inflight, watched, now, lookback)
        for run in held:
            print(
                f"::error::run {run['id']} ({run['name']}, {run['status']}) was created "
                f"{run['created_at']}, before the {args.lookback_hours}h lookback; this pass "
                "will fail so the mark stays and the next pass still lists it"
            )
        print(f"[census] in_flight={len(inflight)} held={len(held)}")
        output_file = os.environ.get("GITHUB_OUTPUT", "")
        if output_file:
            with open(output_file, "a", encoding="utf-8") as handle:
                handle.write(f"held={len(held)}\n")
        return 0
    raise AssertionError(f"unhandled command {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
