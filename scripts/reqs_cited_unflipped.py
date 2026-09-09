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

Honest instrument: when the PR source cannot be read it prints UNKNOWN and
exits 2. It never renders a failed read as "0 cited", because that is the
exact silent-zero the periodic-checks table forbids.
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


def merged_prs_since(days: int, repo: str = DEFAULT_REPO) -> list[dict]:
    """Merged PRs from gh. Raises on any failure; the caller renders UNKNOWN."""
    since = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")
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
            f"merged:>={since}",
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


def render(found: list[Citation], days: int, scanned: int) -> str:
    cited = len({c.req_id for c in found})
    lines = [
        f"[reqs-cited-unflipped] scanned={scanned} merged PRs in {days}d, cited pending ids={cited}"
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
    fetch: Callable[[int], list[dict]] = merged_prs_since,
    reqs_path: Path = REQS_JSON,
) -> int:
    parser = argparse.ArgumentParser(prog="reqs_cited_unflipped")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument(
        "--strict", action="store_true", help="exit 1 when any cited pending id is found"
    )
    args = parser.parse_args(argv)

    payload = json.loads(reqs_path.read_text())
    try:
        prs = fetch(args.days)
    except Exception as exc:
        print(f"[reqs-cited-unflipped] UNKNOWN: could not read merged PRs ({exc})", file=sys.stderr)
        return 2
    if not prs:
        print(
            f"[reqs-cited-unflipped] UNKNOWN: gh returned no merged PRs in {args.days}d; "
            "a zero here is a measurement failure, not a clean result",
            file=sys.stderr,
        )
        return 2
    found = citations(pending_v1_ids(payload), prs)
    print(render(found, args.days, len(prs)))
    return 1 if (args.strict and found) else 0


if __name__ == "__main__":
    raise SystemExit(main())
