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
    python3 -m scripts.ci_run_batch mark ... --display-title "X reconcile" \
        --search-hours 72 --overlap-hours 48
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
            pending.extend(halves(name, start, stop))
    return list(seen.values())


# GitHub re-runs a run, or any of its jobs, "up to 30 days after its initial run"
# (docs.github.com, Re-running workflows and jobs). A re-run keeps the run's id and
# created_at, so past this age a run can no longer complete again. A reconcile pass
# lists a horizon no wider than this; its workflow sets how much narrower.
RERUN_HORIZON = timedelta(days=30)
# GitHub serves at most 1,000 results from a filtered runs listing.
LISTING_CAP = 1000


def fetch_runs_created_between(
    repository: str,
    created_since: str,
    created_before: str,
    token: str,
    agent: str,
    *,
    workflow_names: Iterable[str],
    listing_cap: int = LISTING_CAP,
    get_json: Callable[[str], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Every run of the named workflows created in a CLOSED window, any status.

    This is the reconcile listing: a re-run keeps its run's created_at, so a run
    re-run long after creation is below every pass's creation floor (Codex P1 on
    #3844; 25 of 5,123 watched runs in the 7 days to Tue 29 Sep 2026 started their
    latest attempt more than 3 h after creation, the oldest 143.6 h). With no
    status filter and a window wholly in the past, the listed set cannot grow or
    shrink and its order is by id, which a re-run does not change, so offset
    pagination is safe here, unlike the open, status-filtered pass listing. A
    window over the 1,000-result cap is bisected, and the runs read must add up to
    the window's total_count or the pass fails.
    """
    require_closed_window(created_before, datetime.now(UTC))
    fetch = get_json or (lambda url: _get_json(url, token, agent))
    base = f"https://api.github.com/repos/{repository}/actions/workflows"
    seen: dict[str, dict[str, Any]] = {}
    for name, workflow_id in sorted(_workflow_ids(base, set(workflow_names), fetch).items()):
        pending = [(created_since, created_before)]
        while pending:
            start, stop = pending.pop()
            listing = f"{base}/{workflow_id}/runs?per_page={PAGE_SIZE}&created={start}..{stop}"
            first = fetch(f"{listing}&page=1")
            total = int(first["total_count"])
            if total > listing_cap:
                pending.extend(halves(name, start, stop))
                continue
            runs = list(first.get("workflow_runs") or [])
            for page in range(2, -(-total // PAGE_SIZE) + 1):
                runs.extend(fetch(f"{listing}&page={page}").get("workflow_runs") or [])
            require_served_total(len(runs), total, f"{name} {start}..{stop}")
            seen.update((str(run["id"]), run) for run in runs)
    return list(seen.values())


def reconcile_listing(
    repository: str,
    reconcile_since: str,
    created_before: str,
    token: str,
    *,
    workflow_names: Iterable[str],
    horizon: timedelta,
    listing_cap: int = LISTING_CAP,
    get_json: Callable[[str], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """What a reconcile pass adds to the plain listing: the RE-RUNS among the runs
    created from `horizon` before the reconcile mark up to the plain listing's
    floor. Each page of a runs listing is about 1.7 MB and 4.5 s, so the horizon is
    what a pass costs: 30 days outran a 10-minute job, and a re-run starting more
    than `horizon` after its run was created is not recorded. A first attempt that completes past the floor is the census's case (the
    plain pass holds its mark until that run completes); a re-run is the case no
    plain pass can see, so re-runs are all a reconcile pass adds, about 20 a day,
    and its overlap can be days wide without re-pricing thousands of runs. Empty
    when the pass is not a reconcile pass (no mark)."""
    if not reconcile_since:
        return []
    if not timedelta(0) < horizon <= RERUN_HORIZON:
        raise ValueError(f"a reconcile horizon of {horizon} is outside GitHub's re-run limit")
    listed = fetch_runs_created_between(
        repository,
        iso(parse_time(reconcile_since) - horizon),
        created_before,
        token,
        "ci-run-batch-reconcile",
        workflow_names=workflow_names,
        listing_cap=listing_cap,
        get_json=get_json,
    )
    return [run for run in listed if int(run.get("run_attempt") or 1) > 1]


def require_closed_window(created_before: str, now: datetime) -> None:
    """Offset pagination is safe only over a window no run can still be created in."""
    if parse_time(created_before) >= now:
        raise ValueError(f"the reconcile window must be closed; {created_before} is not past")


def require_served_total(served: int, total: int, what: str) -> None:
    """A closed window serves exactly its total_count, or it changed while read."""
    if served != total:
        raise RuntimeError(
            f"{what} reports {total} runs but served {served}; the listing changed while "
            "it was read"
        )


def require_under_one_page(count: int, what: str) -> None:
    """A one-page listing that fills its page may have more behind it."""
    if count >= PAGE_SIZE:
        raise RuntimeError(f"{what}: {count} runs fill a page; the census would need a second page")


def halves(name: str, start: str, stop: str) -> list[tuple[str, str]]:
    """The two halves of a creation window that holds too many runs to list whole."""
    width = parse_time(stop) - parse_time(start)
    if width <= timedelta(seconds=1):
        raise RuntimeError(
            f"{name}: creation window {start}..{stop} holds too many runs and cannot be "
            "narrowed below one second"
        )
    middle = iso(parse_time(start) + timedelta(seconds=width.total_seconds() // 2))
    return [(middle, stop), (start, middle)]


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
    display_title: str | None = None,
    not_before: str | None = None,
    token: str = "",
    agent: str = "ci-run-batch",
    get_json: Callable[[str], dict[str, Any]] | None = None,
) -> str:
    """The start of the follower's last successful pass, however far back it is.

    With `display_title`, only passes with that title count: a reconcile pass
    (`run-name` set by its workflow) keeps its own mark, while the plain pass mark
    counts every pass, reconcile ones included, since a reconcile pass is a plain
    pass plus its reconcile listing. With `not_before`, the search stops at passes
    created before it and returns "" if it found no success, so the caller falls back
    to `not_before` itself: before the first reconcile succeeds, an unbounded search
    read all 11,493 passes of stable-evidence.yml and outran the job's timeout, and a
    reconcile that never succeeds never writes the mark it searches for (live run
    36562208010 on #3844).

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
            if not_before is not None and str(run["created_at"]) < not_before:
                return ""
            if int(run["id"]) == this_run or run.get("status") != "completed":
                continue
            if display_title is not None and run.get("display_title") != display_title:
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
            require_under_one_page(
                len(batch), f"{status} runs of {name!r} created before {created_before}"
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
    neither (Codex and Sol P1s on #3844). Only a FIRST attempt holds: a re-run keeps
    its old created_at whether or not a pass waits for it, so re-runs are the daily
    reconcile listing's case (`reconcile_listing`), and holding for them would only
    fail plain passes.
    """
    cutoff = now - lookback
    return sorted(
        (
            run
            for run in inflight
            if run.get("name") in watched
            and int(run.get("run_attempt") or 1) == 1
            and parse_time(str(run["created_at"])) < cutoff
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
    mark.add_argument("--display-title", default=None, help="count only passes with this title")
    mark.add_argument(
        "--search-hours",
        type=int,
        default=None,
        help="search only passes created this recently; with no success, the mark is the bound",
    )
    mark.add_argument(
        "--overlap-hours",
        type=int,
        default=None,
        help="print the mark floored at now minus this many hours (batch_since), never empty",
    )
    census = commands.add_parser(
        "census", help="count watched runs in flight since before the lookback (held=N)"
    )
    census.add_argument("--repository", required=True)
    census.add_argument("--watched", required=True, help="comma-separated workflow names")
    census.add_argument("--lookback-hours", type=int, required=True)
    args = parser.parse_args(argv)
    token = os.environ["GITHUB_TOKEN"]
    if args.command == "mark":
        now = datetime.now(UTC)
        not_before = (
            iso(now - timedelta(hours=args.search_hours)) if args.search_hours is not None else None
        )
        start = last_successful_pass_start(
            args.repository,
            args.workflow_file,
            args.this_run,
            display_title=args.display_title,
            not_before=not_before,
            token=token,
        )
        if args.overlap_hours is not None:
            start = batch_since(start or not_before, now, timedelta(hours=args.overlap_hours))
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
