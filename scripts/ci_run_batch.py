#!/usr/bin/env python3
"""One scheduled pass over every workflow completion since a high-water mark.

The Actions queue is per JOB. A `workflow_run` follower that fires on every
completion holds one queued job per completion ahead of PR CI whenever the
pool is saturated (issue #2196). A follower that instead runs once per cadence
and lists the completions since the previous pass costs one job per cadence
whatever the completion rate. The pieces every such follower shares live here:
the mark, its floor, and the paginated listing.

The mark lives nowhere but GitHub's own record of the follower's runs
(`run_started_at` of its last completed pass). The floor makes two passes
overlap even if a pass is late; every follower must make re-applying an
overlap harmless on its own terms.
"""

from __future__ import annotations

import json
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
