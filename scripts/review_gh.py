"""Thin `gh` CLI plumbing shared by scripts/review_coverage.py.

Split out (issue #1016, Thu 3 Sep 2026) so the review-domain module can add
the head-race and pagination fixes below without crossing this repo's
600-line file-size ratchet -- a real refactor along an existing seam rather
than a shrink to fit the gate: this module has no idea what a "reviewer" is,
it only knows how to ask `gh` for JSON and fail loudly when it cannot.

`_body_is_at_head` joined it Sun 6 Sep 2026 for the same reason (#T8): it is
generic comment-body parsing (does THIS line carry the head SHA and a
completed marker), with no idea what a "reviewer" is either, and moving it
here is what kept scripts/review_coverage.py's own module-resolution guard
(added the same day) from pushing the file over the 600-line ceiling.
"""

from __future__ import annotations

import json
import os
import re
import subprocess

from scripts.gh_version_guard import require_gh_min_version

#: A commit SHA GitHub renders in backtick-quoted code, e.g. the summary
#: table's `` `7cbe749` `` Commit column or a review body's `` **Reviewed
#: commit:** `7cbe7496d2` ``. GitHub abbreviates to 7+ hex chars, never fewer,
#: so the floor here matches GitHub's own minimum rather than inventing one.
_SHA_IN_BACKTICKS = re.compile(r"`([0-9a-f]{7,40})`", re.IGNORECASE)

#: The summary table's Status cell reads e.g. `` ✅ **Completed** ``,
#: `` ⏳ **Queued** ``, `` 🔄 **In progress** `` or `` ❌ **Failed** ``. Only
#: the first of those is a claim that Codex finished looking at this head.
_STATUS_COMPLETED = re.compile(r"completed", re.IGNORECASE)


def _body_is_at_head(body: str, head_sha: str) -> bool:
    """Does this comment's OWN embedded commit reference match the PR head,
    AND does that same line say the review of it is done?

    Issue comments (as opposed to submitted reviews or inline review
    comments) carry no `commit_id` field at all -- GitHub does not tie them to
    any specific push -- so a bot's summary comment is otherwise untethered
    from any particular head. Codex's summary comment is edited in place each
    round and embeds the SHA it reviewed, so require that embedded prefix to
    match the CURRENT head. That alone is not enough (issue #1016 P1
    BLOCKING, thread r3927558602): the summary's Commit column takes on the
    new head's SHA the moment a round STARTS, before Codex has looked at
    anything, so a row read as `Queued`/`In progress`/`Failed` for the current
    head is a promise, not a review. Require both the SHA and a `completed`
    marker on the SAME table row -- table rows are one line each in Codex's
    rendered markdown, so line-scoping ties the status to the SHA it actually
    describes rather than to any other row of a multi-row table. A body with
    no such row is treated as NOT evidence for this push, the same
    fail-closed direction as zero artifacts.
    """
    for line in body.splitlines():
        shas = _SHA_IN_BACKTICKS.findall(line)
        if not shas:
            continue
        if not any(head_sha.lower().startswith(sha.lower()) for sha in shas):
            continue
        if _STATUS_COMPLETED.search(line):
            return True
    return False


#: The bot login each expected reviewer posts under. A reviewer's STATUS is not
#: evidence it reviewed; #682 carried CodeRabbit "Review completed" with zero
#: submitted reviews and zero inline comments (Tue 1 Sep 2026). Only an ARTIFACT
#: -- a submitted review, an inline comment, or a review summary comment -- shows
#: that something actually looked at the diff.
REVIEWER_LOGINS: dict[str, tuple[str, ...]] = {
    "Codex": ("chatgpt-codex-connector",),
}


def _matches(login: str, name: str) -> bool:
    """Exact match on the normalized login, never a substring test.

    issue #1016 P1 BLOCKING, thread r3929765931 (PR #1053, Thu 3 Sep 2026): a
    substring check accepted `chatgpt-codex-connector-attacker` as Codex,
    because `"chatgpt-codex-connector" in login.lower()` is true for any
    login merely CONTAINING the trusted stem. On a public repo any commenter
    could post a current-head `Completed` line under that name and pass
    coverage without a real Codex review. Normalize the `[bot]` suffix and
    require full equality, the same rule `review_thread_parse._is_bot` already
    applies to its own bot-identity check.
    """
    return login.removesuffix("[bot]").lower() in REVIEWER_LOGINS.get(name, ())


class TriageError(RuntimeError):
    """Measurement failed. Never rendered as a verdict."""


def _gh(args: list[str], payload: dict | None = None, *, as_human: bool = False) -> str:
    """Run `gh` and return stdout, raising on any nonzero exit.

    `payload` is written to the child's stdin as JSON, for the one call shape
    that needs a request body too large and too nested for `-f` flags: the
    `pulls/<n>/reviews` POST that `scripts/sol_review.py` submits, whose
    `comments` array is a list of objects. Kept here rather than shelling out
    separately so every `gh` call in the review tooling fails the same way.

    `as_human=True` strips `GH_APP` from the child env and sets
    `GH_ALLOW_HUMAN=1` (issue #3951). Sol/Claude reviews count toward coverage
    only when posted as login ``maintainer`` plus a marker comment; with
    `GH_APP` set the fleet `gh` shim posts as the GitHub App bot instead, and
    the coverage gate silently treats that as a MISS.

    `require_gh_min_version()` runs first so a too-old `gh` reports its own
    version and the minimum needed, instead of the opaque `unknown flag:
    --slurp` this module's `--paginate --slurp` calls would otherwise raise
    (scripts/gh_version_guard.py; agentbox-15 job 103871571683, Fri 12 Sep
    2026 onward). `scripts/ci_wait.py` imports this `_gh`, so one check here
    covers both call sites.
    """
    require_gh_min_version()
    if as_human:
        env = dict(os.environ)
        env.pop("GH_APP", None)
        env["GH_ALLOW_HUMAN"] = "1"
        proc = subprocess.run(
            ["gh", *args],
            input=json.dumps(payload) if payload is not None else None,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
    else:
        proc = subprocess.run(
            ["gh", *args],
            input=json.dumps(payload) if payload is not None else None,
            capture_output=True,
            text=True,
            check=False,
        )
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


def _paginated_json_pages(endpoint: str) -> list:
    """Every page of an endpoint whose pages are OBJECTS (`compare`, or the single object
    `pulls/{n}`), unflattened: `--slurp` hands back one outer array with a page per item."""
    return json.loads(_gh(["api", endpoint, "--paginate", "--slurp"]))
