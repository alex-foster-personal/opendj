"""Read-only census of queued and in-progress GitHub Actions runs (SMARTEST-CI F6).

Pages ``repos/{repo}/actions/runs`` for ``status=queued`` and ``status=in_progress``,
filters by ``created_at`` inside a sliding window (drops Sep-Oct ghost queued runs),
groups by workflow name and event, and splits bookkeeping vs verdict by workflow name.

Stdlib only. Transport: ``scripts.gh_api.gh_api``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, TextIO

from scripts.gh_api import gh_api

PAGE_SIZE = 100
MAX_PAGES = 10
INFLIGHT = frozenset({"queued", "in_progress"})
BOOKKEEPING_MARKERS = ("cost guard", "budget watch", "stable evidence", "error sink", "label")
DEFAULT_REPO = "private_owner/music-dj-tools"
DEFAULT_WINDOW_HOURS = 168


class CensusError(Exception):
    """API, JSON, or schema failure; never emit zero counts on this path."""


def parse_time(value: str) -> datetime:
    if not value.endswith("Z"):
        raise CensusError(f"created_at must end with Z: {value!r}")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def format_time(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_bookkeeping(name: str) -> bool:
    lower = name.lower()
    return any(marker in lower for marker in BOOKKEEPING_MARKERS)


def in_window(created_at: datetime, start: datetime, end: datetime) -> bool:
    return start <= created_at < end


def select_runs(
    page_runs: list[dict[str, Any]],
    *,
    start: datetime,
    end: datetime,
) -> tuple[list[dict[str, Any]], int]:
    """Keep inflight runs whose created_at is in the window; count pre-window inflight ghosts."""
    kept: list[dict[str, Any]] = []
    excluded_before_window = 0
    for run in page_runs:
        status = run.get("status")
        if status not in INFLIGHT:
            continue
        raw_created = run.get("created_at")
        if not isinstance(raw_created, str):
            raise CensusError("workflow run missing string created_at")
        created_at = parse_time(raw_created)
        if created_at < start:
            excluded_before_window += 1
            continue
        if not in_window(created_at, start, end):
            continue
        kept.append(run)
    return kept, excluded_before_window


def group_counts(runs: list[dict[str, Any]]) -> dict[tuple[str, str], int]:
    counter: Counter[tuple[str, str]] = Counter()
    for run in runs:
        name = run.get("name")
        event = run.get("event")
        if not isinstance(name, str) or not isinstance(event, str):
            raise CensusError("workflow run missing name or event")
        counter[(name, event)] += 1
    return dict(counter)


def fetch_status_pages(
    repo: str,
    status: str,
    get_json: Callable[[str], dict[str, Any]],
) -> list[dict[str, Any]]:
    all_runs: list[dict[str, Any]] = []
    for page in range(1, MAX_PAGES + 1):
        path = f"repos/{repo}/actions/runs?status={status}&per_page={PAGE_SIZE}&page={page}"
        body = get_json(path)
        if not isinstance(body, dict):
            raise CensusError(f"expected object from {path}")
        runs = body.get("workflow_runs")
        if not isinstance(runs, list):
            raise CensusError(f"expected workflow_runs list from {path}")
        all_runs.extend(runs)
        if len(runs) < PAGE_SIZE:
            break
    else:
        raise CensusError(
            f"actions runs listing for status={status} reached {MAX_PAGES} full pages; refuse to truncate"
        )
    return all_runs


def census(
    repo: str,
    *,
    start: datetime,
    end: datetime,
    get_json: Callable[[str], dict[str, Any]],
) -> dict[str, Any]:
    seen_ids: set[int] = set()
    raw: list[dict[str, Any]] = []
    for status in ("queued", "in_progress"):
        for run in fetch_status_pages(repo, status, get_json):
            run_id = run.get("id")
            if not isinstance(run_id, int):
                raise CensusError("workflow run missing integer id")
            if run_id in seen_ids:
                continue
            seen_ids.add(run_id)
            raw.append(run)

    kept, excluded_before_window = select_runs(raw, start=start, end=end)
    counts = group_counts(kept)

    by_workflow_event: list[dict[str, Any]] = []
    bookkeeping = 0
    verdict = 0
    for (name, event), count in sorted(counts.items()):
        kind = "bookkeeping" if is_bookkeeping(name) else "verdict"
        if kind == "bookkeeping":
            bookkeeping += count
        else:
            verdict += count
        by_workflow_event.append(
            {"name": name, "event": event, "count": count, "kind": kind}
        )

    total = bookkeeping + verdict
    return {
        "window_start": format_time(start),
        "window_end": format_time(end),
        "total": total,
        "bookkeeping": bookkeeping,
        "verdict": verdict,
        "excluded_before_window": excluded_before_window,
        "by_workflow_event": by_workflow_event,
    }


def format_report(result: dict[str, Any]) -> str:
    total = result["total"]
    bookkeeping = result["bookkeeping"]
    share = (bookkeeping / total) if total > 0 else 0.0
    lines = [
        f"window_start={result['window_start']} window_end={result['window_end']}",
        (
            f"total={total} bookkeeping={bookkeeping} verdict={result['verdict']} "
            f"bookkeeping_share={share:.3f} excluded_before_window={result['excluded_before_window']}"
        ),
        "name\tevent\tkind\tcount",
    ]
    lines.extend(
        f"{row['name']}\t{row['event']}\t{row['kind']}\t{row['count']}"
        for row in result["by_workflow_event"]
    )
    return "\n".join(lines) + "\n"


def _default_get_json() -> Callable[[str], dict[str, Any]]:
    def get_json(path: str) -> dict[str, Any]:
        raw = gh_api(path, error_cls=CensusError)
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CensusError(f"invalid JSON from gh api {path}: {exc}") from exc
        if not isinstance(parsed, dict):
            raise CensusError(f"expected JSON object from gh api {path}")
        return parsed

    return get_json


def main(
    argv: list[str] | None = None,
    *,
    get_json: Callable[[str], dict[str, Any]] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr

    parser = argparse.ArgumentParser(description="Census queued/in-progress Actions workflow fan-out.")
    parser.add_argument("--repository", default=DEFAULT_REPO, help="owner/repo")
    parser.add_argument("--window-hours", type=int, default=DEFAULT_WINDOW_HOURS)
    parser.add_argument(
        "--now",
        default=None,
        help="ISO-8601 UTC timestamp (Z) for window end; default: current UTC time",
    )
    args = parser.parse_args(argv)

    if args.window_hours <= 0:
        err.write("error: --window-hours must be positive\n")
        return 1

    end = parse_time(args.now) if args.now else datetime.now(UTC)
    start = end - timedelta(hours=args.window_hours)

    fetch = get_json if get_json is not None else _default_get_json()
    try:
        result = census(args.repository, start=start, end=end, get_json=fetch)
    except CensusError as exc:
        err.write(f"error: {exc}\n")
        return 1

    out.write(format_report(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
