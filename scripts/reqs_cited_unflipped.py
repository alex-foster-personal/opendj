"""List v1 requirement ids that merged pull requests cite but that still read pending.

Step 3 of issue #1605, in its smallest form: a periodic REPORT, not a per-PR
gate. Merged PRs that name a requirement id are the strongest signal that the
requirement shipped; a cited id that is still ``pending`` in ``reqs.json`` is
either shipped-but-unflipped (REQHYG-02 covers the behaviour delta, nothing
enforces the status flip) or a docs PR that merely recorded the id. This tool
finds the candidates; a person judges them against the acceptance lines.
Matching is whole-id only: a range such as ``OPS-12..17`` or ``OPS-21/22`` does
not cite ``OPS-15``, by design, because ranges are how docs PRs record ids.

    python -m scripts.reqs_cited_unflipped                # last 7 days
    python -m scripts.reqs_cited_unflipped --days 30
    python -m scripts.reqs_cited_unflipped --strict       # exit 1 when any found

The periodic tier calls it with the window it actually recorded rather than a
rolling week:

    python -m scripts.reqs_cited_unflipped \
        --since 2026-09-03T05:41:12Z --until 2026-09-10T19:25:00Z

``--reqs-path`` overrides which ``reqs.json`` payload supplies the pending
ids, defaulting to this repo's own file. The periodic-checks workflow step
reads it from ``$REQS_JSON_PATH`` when that env var is set (unset in
production, so the default is unchanged); a test that must stay independent
of live v1 burn-down status points it at a pinned snapshot instead:

    python -m scripts.reqs_cited_unflipped \
        --reqs-path tests/fixtures/github/reqs_cited_unflipped_reqs_snapshot.json

Honest instrument: when the PR source cannot be read it prints UNKNOWN and
exits 2. It never renders a failed read as "0 cited", because that is the
exact silent-zero the periodic-checks table forbids. A read that SUCCEEDS and
finds nothing inside the recorded interval is a real zero and renders as one.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REQS_JSON = REPO_ROOT / "reqs.json"
DEFAULT_REPO = "maintainer/music-dj-tools"
DEFAULT_DAYS = 7
# A merged PR whose title starts like this recorded the id rather than shipped it.
DOCS_TITLE = re.compile(r"^docs\b")


@dataclass(frozen=True)
class Citation:
    req_id: str
    pr_number: int
    merged_at: str
    title: str
    docs_only_title: bool


def pending_v1_ids(payload: dict) -> list[str]:
    return [
        r["id"]
        for section in payload["v1"].values()
        for r in section["requirements"]
        if r["status"] != "shipped"
    ]


def citations(pending: Iterable[str], prs: Iterable[dict]) -> list[Citation]:
    prs = list(prs)
    out: list[Citation] = []
    for req_id in pending:
        pat = re.compile(r"\b" + re.escape(req_id) + r"\b")
        for pr in prs:
            text = (pr.get("title") or "") + "\n" + (pr.get("body") or "")
            if pat.search(text):
                out.append(
                    Citation(
                        req_id=req_id,
                        pr_number=int(pr["number"]),
                        merged_at=str(pr["mergedAt"]),
                        title=str(pr.get("title") or ""),
                        docs_only_title=bool(DOCS_TITLE.match(pr.get("title") or "")),
                    )
                )
    return out


def _instant(value: str) -> datetime:
    """One ISO-8601 instant in UTC.

    Both spellings occur in the data this reads: GitHub's `created_at` on the
    ledger comment ends in `Z`, while `git log --format=%cI` ends in `+00:00`.
    Comparing the two as strings would be wrong, so both are parsed here.
    """
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


@dataclass(frozen=True)
class Window:
    """The interval a periodic window covers, and how to ask gh for it.

    `search` is the `merged:` term handed to gh; `covers` is the exact bound
    applied to what comes back. They differ because gh's `merged:` range is
    DAY-granular: `2026-09-03..2026-09-10` is the whole of both days, which is
    a superset of the recorded interval and must be narrowed.
    """

    since: datetime
    until: datetime
    search: str
    label: str

    def covers(self, merged_at: str) -> bool:
        """Is this PR's merge inside the recorded interval?"""
        return self.since <= _instant(merged_at) <= self.until


def window_bounds(days: int | None, since: str | None, until: str | None) -> Window:
    """The interval to measure and the `merged:` term that asks GitHub for it.

    A periodic window is recorded as a ledger-comment timestamp and a head SHA,
    and the report exists to describe exactly THAT interval. A rolling `--days`
    slice does not: windows close on 50 merges or 7 elapsed days, whichever
    comes first, so a fixed week can miss the first day of an 8-day window or
    re-report PRs an earlier window already covered. `--since`/`--until` take
    the interval the window job actually recorded.

    `--days` is the ad-hoc slice and keeps gh's own day-granular definition;
    only an explicit interval is narrowed to its exact instants (see `covers`).
    """
    if since:
        start = _instant(since)
        end = _instant(until) if until else datetime.now(UTC)
        return Window(
            since=start,
            until=end,
            search=f"{start:%Y-%m-%d}..{end:%Y-%m-%d}",
            label=f"{start:%Y-%m-%dT%H:%M:%SZ}..{end:%Y-%m-%dT%H:%M:%SZ}",
        )
    if days is None:
        raise ValueError("one of --days or --since is required")
    end = datetime.now(UTC)
    start = end - timedelta(days=days)
    return Window(since=start, until=end, search=f">={start:%Y-%m-%d}", label=f"{days}d")


def merged_prs(search: str, repo: str = DEFAULT_REPO) -> list[dict]:
    """Merged PRs from gh. Raises on any failure; the caller renders UNKNOWN."""
    proc = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "merged",
            "--limit",
            "500",
            "--search",
            f"merged:{search}",
            "--json",
            "number,title,body,mergedAt",
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gh pr list failed rc={proc.returncode}: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def render(found: list[Citation], label: str, scanned: int) -> str:
    cited = len({c.req_id for c in found})
    lines = [
        f"[reqs-cited-unflipped] scanned={scanned} merged PRs in {label}, cited pending ids={cited}"
    ]
    for c in sorted(found, key=lambda c: (c.req_id, c.merged_at)):
        tag = "docs-title" if c.docs_only_title else "feature"
        lines.append(f"  {c.req_id:14s} #{c.pr_number} {c.merged_at[:10]} [{tag}] {c.title[:70]}")
    if not found:
        lines.append("  (none)")
    return "\n".join(lines)


def main(
    argv: list[str] | None = None,
    *,
    fetch: Callable[[str], list[dict]] = merged_prs,
    reqs_path: Path = REQS_JSON,
) -> int:
    parser = argparse.ArgumentParser(prog="reqs_cited_unflipped")
    parser.add_argument("--days", type=int, default=None)
    parser.add_argument(
        "--since", default=None, help="window start, ISO instant; takes precedence over --days"
    )
    parser.add_argument("--until", default=None, help="window end, ISO instant (inclusive)")
    parser.add_argument(
        "--strict", action="store_true", help="exit 1 when any cited pending id is found"
    )
    parser.add_argument(
        "--reqs-path",
        type=Path,
        default=None,
        help="path to the reqs.json payload to read pending ids from (default: this repo's own)",
    )
    args = parser.parse_args(argv)
    days = args.days if (args.days is not None or args.since) else DEFAULT_DAYS
    effective_reqs_path = args.reqs_path if args.reqs_path is not None else reqs_path

    try:
        window = window_bounds(days, args.since, args.until)
    except ValueError as exc:
        print(f"[reqs-cited-unflipped] UNKNOWN: {exc}", file=sys.stderr)
        return 2

    payload = json.loads(effective_reqs_path.read_text())
    try:
        prs = fetch(window.search)
    except Exception as exc:
        print(f"[reqs-cited-unflipped] UNKNOWN: could not read merged PRs ({exc})", file=sys.stderr)
        return 2
    if not prs:
        print(
            f"[reqs-cited-unflipped] UNKNOWN: gh returned no merged PRs in {window.label}; "
            "a zero here is a measurement failure, not a clean result",
            file=sys.stderr,
        )
        return 2
    # gh's `merged:` range is day-granular, so an explicit recorded interval is
    # narrowed to its exact ends here: without this it would admit the tail of
    # the previous window and any PR merged after the recorded head. The guard
    # above has already run on the RAW read, so an empty result below is a real
    # "nothing merged in this window", not an unreadable source rendered as zero.
    if args.since:
        prs = [pr for pr in prs if window.covers(pr["mergedAt"])]
    found = citations(pending_v1_ids(payload), prs)
    print(render(found, window.label, len(prs)))
    return 1 if (args.strict and found) else 0


if __name__ == "__main__":
    raise SystemExit(main())
