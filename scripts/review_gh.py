"""Thin `gh` CLI plumbing shared by scripts/review_coverage.py.

Split out (issue #1016, Thu 3 Sep 2026) so the review-domain module can add
the head-race and pagination fixes below without crossing this repo's
600-line file-size ratchet -- a real refactor along an existing seam rather
than a shrink to fit the gate: this module has no idea what a "reviewer" is,
it only knows how to ask `gh` for JSON and fail loudly when it cannot.
"""

from __future__ import annotations

import json
import subprocess


class TriageError(RuntimeError):
    """Measurement failed. Never rendered as a verdict."""


def _gh(args: list[str]) -> str:
    proc = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise TriageError(
            f"gh {' '.join(args)} failed ({proc.returncode}): "
            f"{proc.stderr.strip() or '<no stderr>'}"
        )
    return proc.stdout


def _checks(pr: str) -> list[dict[str, str]]:
    raw = _gh(["pr", "checks", pr, "--json", "name,bucket,state,description"])
    if not raw.strip():
        raise TriageError(f"gh returned an empty check list for PR {pr}")
    return json.loads(raw)


def _head_sha(pr: str) -> str:
    raw = _gh(["pr", "view", pr, "--json", "headRefOid", "-q", ".headRefOid"]).strip()
    if not raw:
        raise TriageError(f"gh returned no headRefOid for PR {pr}")
    return raw


def _flatten_pages(pages: list[list[dict]]) -> list[dict]:
    """Flatten `gh api --paginate --slurp`'s list-of-pages into one list.

    `--paginate` alone is not enough: each page is a separate JSON array
    printed back to back, not valid JSON to `json.loads` past page 1.
    `--slurp` wraps pages into one outer array instead, so even a single page
    arrives as `[[...]]`. Kept pure (issue #1016 P1 BLOCKING, thread
    r3927877691) so it is directly testable on literal data -- this repo's
    AGENTS.md bars mocking `_gh` itself, so the live `--paginate --slurp`
    wiring is proven instead by a real CLI integration test in
    tests/scripts/test_review_coverage_pagination.py.
    """
    return [item for page in pages for item in page]


def _paginated_json_list(endpoint: str) -> list[dict]:
    """Fetch every page of a REST list endpoint (issue #1016 P2 BLOCKING,
    thread r3927558605): a plain `gh api <endpoint>` call only fetches page
    1, silently dropping evidence that scrolled past it on a long-lived PR.
    """
    return _flatten_pages(json.loads(_gh(["api", endpoint, "--paginate", "--slurp"])))
