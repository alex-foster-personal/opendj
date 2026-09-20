"""Measure and alert on open PRs whose ``main`` base is behind trunk.

Supersedes: no prior open-PR base freshness command existed; this command is
the sole measurement implementation used by ``main-control.yml``.

Mini-PRD:
* ✔︎ A six-hour control run measures only open PRs based on ``main``.
* ✔︎ The metric is the age of the newest base commit that is behind ``main``;
  the report also includes the largest commit gap and the affected PR.
* ✔︎ A base older than the explicit threshold exits non-zero so GitHub Actions
  can alert on it.

Acceptance tests:
* [if] a PR is based on a non-main branch [then] it is excluded.
* [if] a main-based PR is current [then] it does not affect the stale metric.
* [if] a main-based PR is behind main [then] the newest stale base is reported.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class Measurement:
    """The observable current-main freshness metrics."""

    pr_count: int
    newest_base_oid: str | None
    newest_base_age_hours: int | None
    max_main_commits_ahead: int
    affected_pr: int | None


def measure(
    prs: Sequence[Mapping[str, object]],
    commits: Mapping[str, tuple[str, int]],
    *,
    now: datetime,
) -> Measurement:
    """Measure stale main bases from API rows and commit metadata."""
    stale: list[tuple[datetime, int, str, int]] = []
    for pr in prs:
        if pr.get("baseRefName") != "main":
            continue
        oid = pr.get("baseRefOid")
        number = pr.get("number")
        if not isinstance(oid, str) or not isinstance(number, int):
            raise TypeError(f"PR row has invalid base identity: {pr!r}")
        if oid not in commits:
            raise ValueError(f"missing commit metadata for base {oid}")
        committed_at_text, commits_ahead = commits[oid]
        committed_at = datetime.fromisoformat(committed_at_text.replace("Z", "+00:00"))
        if committed_at.tzinfo is None:
            raise ValueError(f"base commit timestamp is not timezone-aware: {committed_at_text}")
        if commits_ahead > 0:
            stale.append((committed_at, number, oid, commits_ahead))

    if not stale:
        return Measurement(0, None, None, 0, None)

    newest, pr_number, oid, commits_ahead = max(stale, key=lambda row: row[0])
    age_hours = int(
        (now.astimezone(UTC) - newest.astimezone(UTC)).total_seconds() // 3600
    )
    return Measurement(len(stale), oid, age_hours, max(row[3] for row in stale), pr_number)


def _run(*command: str) -> str:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(command)}\n"
            f"{result.stderr.strip()}"
        )
    return result.stdout


def _live_rows() -> tuple[list[Mapping[str, object]], dict[str, tuple[str, int]]]:
    rows = json.loads(
        _run(
            "gh",
            "pr",
            "list",
            "--state",
            "open",
            "--base",
            "main",
            "--limit",
            "1000",
            "--json",
            "number,baseRefName,baseRefOid",
        )
    )
    if not isinstance(rows, list):
        raise TypeError("gh pr list did not return a JSON list")
    commits: dict[str, tuple[str, int]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise TypeError(f"gh pr list returned a non-object row: {row!r}")
        oid = row.get("baseRefOid")
        if not isinstance(oid, str):
            raise TypeError(f"PR row has no baseRefOid: {row!r}")
        committed_at = _run("git", "show", "-s", "--format=%cI", oid).strip()
        ahead_text = _run("git", "rev-list", "--count", f"{oid}..main").strip()
        commits[oid] = (committed_at, int(ahead_text))
    return rows, commits


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-age-hours", type=int, required=True)
    args = parser.parse_args()
    if args.max_age_hours < 0:
        raise ValueError("--max-age-hours must be non-negative")
    prs, commits = _live_rows()
    result = measure(prs, commits, now=datetime.now(UTC))
    if result.pr_count == 0:
        print("PR_BASE_FRESHNESS OK stale_main_bases=0")
        return 0
    print(
        "PR_BASE_FRESHNESS "
        f"stale_main_bases={result.pr_count} "
        f"newest_base={result.newest_base_oid} "
        f"newest_base_age_hours={result.newest_base_age_hours} "
        f"max_main_commits_ahead={result.max_main_commits_ahead} "
        f"affected_pr={result.affected_pr}"
    )
    if result.newest_base_age_hours is None:
        raise RuntimeError("stale PR count was non-zero without an age")
    if result.newest_base_age_hours > args.max_age_hours:
        print(
            f"::error::newest open PR base is {result.newest_base_age_hours}h behind main "
            f"(threshold {args.max_age_hours}h)"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
