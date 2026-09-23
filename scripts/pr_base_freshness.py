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
* [if] the captured `gh pr list` payload (tests/fixtures/pr-base-freshness,
  verified against its MANIFEST.json) is fed through ``--rows-json`` [then] the
  same parser, git lookups, measurement and exit code run as on the live call.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path


@dataclass(frozen=True)
class Measurement:
    """The observable current-main freshness metrics."""

    pr_count: int
    newest_base_oid: str | None
    #: Exact age of the newest stale base; the threshold compares THIS, never
    #: the whole-hour figure below, which is for display and floors 12 h 59 min
    #: to 12.
    newest_base_age: timedelta | None
    newest_base_age_hours: int | None
    #: The PR whose stale base is the NEWEST (the alert's subject) and ITS gap.
    affected_pr: int | None
    affected_pr_commits_ahead: int
    #: The largest gap in the stale set and the PR that carries it: a
    #: different PR from affected_pr whenever an older base is further behind.
    max_main_commits_ahead: int
    max_gap_pr: int | None


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
        return Measurement(
            pr_count=0,
            newest_base_oid=None,
            newest_base_age=None,
            newest_base_age_hours=None,
            affected_pr=None,
            affected_pr_commits_ahead=0,
            max_main_commits_ahead=0,
            max_gap_pr=None,
        )

    newest, pr_number, oid, commits_ahead = max(stale, key=lambda row: row[0])
    age = now.astimezone(UTC) - newest.astimezone(UTC)
    age_hours = int(age.total_seconds() // 3600)
    # Kept as a pair so the printed gap is never attributed to the wrong PR:
    # the newest stale base and the widest gap are usually different PRs.
    _, max_gap_pr, _, max_gap = max(stale, key=lambda row: row[3])
    return Measurement(
        pr_count=len(stale),
        newest_base_oid=oid,
        newest_base_age=age,
        newest_base_age_hours=age_hours,
        affected_pr=pr_number,
        affected_pr_commits_ahead=commits_ahead,
        max_main_commits_ahead=max_gap,
        max_gap_pr=max_gap_pr,
    )


def exceeds_threshold(result: Measurement, *, max_age_hours: int) -> bool:
    """True when the newest stale base is older than the threshold, compared on
    the exact age: 12 h 01 min exceeds a 12 h threshold although it prints as 12."""
    if result.pr_count == 0:
        return False
    if result.newest_base_age is None:
        raise RuntimeError("stale PR count was non-zero without an age")
    return result.newest_base_age > timedelta(hours=max_age_hours)


def _run(*command: str) -> str:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(command)}\n{result.stderr.strip()}"
        )
    return result.stdout


def parse_rows(text: str) -> list[Mapping[str, object]]:
    """The `gh pr list` JSON payload as validated rows: the one parser, which a
    captured payload (``--rows-json``) goes through exactly as the live call does."""
    rows = json.loads(text)
    if not isinstance(rows, list):
        raise TypeError("gh pr list did not return a JSON list")
    for row in rows:
        if not isinstance(row, dict):
            raise TypeError(f"gh pr list returned a non-object row: {row!r}")
        if not isinstance(row.get("baseRefOid"), str):
            raise TypeError(f"PR row has no baseRefOid: {row!r}")
    return rows


def collect_commits(
    rows: Sequence[Mapping[str, object]], *, main_ref: str
) -> dict[str, tuple[str, int]]:
    """Each base's commit time and how many commits ``main_ref`` is ahead of it."""
    commits: dict[str, tuple[str, int]] = {}
    for row in rows:
        oid = str(row["baseRefOid"])
        committed_at = _run("git", "show", "-s", "--format=%cI", oid).strip()
        ahead_text = _run("git", "rev-list", "--count", f"{oid}..{main_ref}").strip()
        commits[oid] = (committed_at, int(ahead_text))
    return commits


def _live_rows(
    *, rows_json: Path | None, main_ref: str
) -> tuple[list[Mapping[str, object]], dict[str, tuple[str, int]]]:
    if rows_json is not None:
        text = rows_json.read_text(encoding="utf-8")
    else:
        text = _run(
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
    rows = parse_rows(text)
    return rows, collect_commits(rows, main_ref=main_ref)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-age-hours", type=int, required=True)
    parser.add_argument(
        "--rows-json",
        type=Path,
        default=None,
        help="a captured `gh pr list` payload to measure in place of the live call",
    )
    parser.add_argument("--main-ref", default="main", help="the ref each base is measured against")
    args = parser.parse_args(argv)
    if args.max_age_hours < 0:
        raise ValueError("--max-age-hours must be non-negative")
    prs, commits = _live_rows(rows_json=args.rows_json, main_ref=args.main_ref)
    result = measure(prs, commits, now=datetime.now(UTC))
    if result.pr_count == 0:
        print("PR_BASE_FRESHNESS OK stale_main_bases=0")
        return 0
    print(
        "PR_BASE_FRESHNESS "
        f"stale_main_bases={result.pr_count} "
        f"newest_base={result.newest_base_oid} "
        f"newest_base_age_hours={result.newest_base_age_hours} "
        f"affected_pr={result.affected_pr} "
        f"affected_pr_commits_ahead={result.affected_pr_commits_ahead} "
        f"max_main_commits_ahead={result.max_main_commits_ahead} "
        f"max_gap_pr={result.max_gap_pr}"
    )
    if exceeds_threshold(result, max_age_hours=args.max_age_hours):
        print(
            f"::error::newest open PR base is {result.newest_base_age_hours}h behind main "
            f"(threshold {args.max_age_hours}h)"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
