"""Shared helpers for the review-thread gate tests.

FIXTURES ARE REAL CAPTURES. `tests/fixtures/review_threads/*.json` are verbatim
GraphQL responses from this repo's own PRs, captured Mon 31 Aug 2026 and
consumed through the production `build_thread` path:

    pr-576.json   Codex with explicit BLOCKING / NON-BLOCKING verdicts
    pr-515.json   both reviewers at once: Codex findings alongside Devin
                  coloured bugs and analysis notes
    pr-492.json   the long-lived mix: resolved, replied-unresolved, silent

Thread objects inside them are byte-identical to what the API returned; the
only edit is SELECTING a subset of threads (and dropping the pageInfo cursor)
to keep the files small. Every read is checksum-verified against MANIFEST.json
BEFORE parsing, so an accidental edit fails loudly rather than silently
becoming the authoritative capture. Regenerate by re-capturing, never by hand.

The synthetic builder is kept ONLY for shapes the corpus does not contain (P0
and P3 have never been emitted here, and no bot has yet replied to its own
thread). It is input to a pure parser, not fabricated application state
standing in for a real path.
"""

from __future__ import annotations

import hashlib
import json
import pathlib

from scripts.review_thread_parse import build_thread

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "review_threads"
MANIFEST = json.loads((FIXTURES / "MANIFEST.json").read_text())


def _verified_bytes(name: str) -> bytes:
    """Read a capture only after its manifest checksum matches.

    Without this, an accidental edit to a payload would silently become the
    authoritative capture and the suite would pass against corrupted evidence.
    Checksum first, parse second.
    """
    entry = MANIFEST["files"].get(name)
    assert entry is not None, f"{name} has no manifest entry; add one or delete the file"
    raw = (FIXTURES / name).read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    assert actual == entry["sha256"], (
        f"{name} does not match its pinned capture.\n"
        f"  expected {entry['sha256']}\n  actual   {actual}\n"
        "Captures are immutable. Re-capture from the API and regenerate the "
        "manifest; never hand-edit a payload to make a test pass."
    )
    assert len(raw) == entry["bytes"], f"{name} size drifted from the manifest"
    return raw


def _captured_nodes(name: str) -> list[dict]:
    """Every raw GraphQL thread node in a verified capture, for a caller that
    needs to pass its OWN ledger into `build_thread` rather than the default
    empty one -- see `_captured` below, which is the common case that doesn't.
    """
    payload = json.loads(_verified_bytes(name))
    return payload["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]


def _captured(pr: int) -> list:
    """Every bot thread in a verified capture, through the production path."""
    nodes = _captured_nodes(f"pr-{pr}.json")
    return [t for t in (build_thread(n) for n in nodes) if t is not None]


def _captured_pr_header(name: str) -> dict:
    """The real `number`/`title`/`state`/`merged`/`url` for a verified capture's
    PR, read from the same GraphQL response `_captured_nodes` draws threads
    from. `headRefOid` is not part of this query shape and so is never in the
    capture; callers building a `PullRequest` still supply `head_sha`
    explicitly, same as `fetch_pull_request` reads it from a separate ref
    query rather than the thread page (`review_thread_triage.py:202`).
    """
    payload = json.loads(_verified_bytes(name))
    pr = payload["data"]["repository"]["pullRequest"]
    return {
        "number": pr["number"],
        "title": pr["title"],
        "state": pr["state"],
        "merged": pr["merged"],
        "url": pr["url"],
    }


# A real Codex finding from PR #492, trimmed to the shape that matters.
CODEX_BODY = (
    "**<sub><sub>![P1 Badge](https://img.shields.io/badge/P1-orange?style=flat)"
    "</sub></sub>  Surface terminal AudioWorklet failures**\n\n"
    "If an exception escapes the processor's constructor, the browser can "
    "terminate the processor and leave its node producing silence.\n\n"
    "Useful? React with a thumbs up or down."
)


def _thread_node(
    *,
    author: str = "chatgpt-codex-connector",
    body: str = CODEX_BODY,
    resolved: bool = False,
    replies: tuple[str, ...] = (),
    reply_body: str = "Fixed in abc1234.",
    reply_bodies: tuple[str, ...] = (),
) -> dict:
    """Build a GraphQL reviewThread node with the given first author and replies."""
    comments = [
        {
            "author": {"login": author},
            "body": body,
            "url": "https://github.com/o/r/pull/1#discussion_r1",
            "createdAt": "2026-08-22T01:05:32Z",
        }
    ]
    bodies = reply_bodies or tuple(reply_body for _ in replies)
    comments += [
        {
            "author": {"login": login},
            "body": bodies[i],
            "url": "https://github.com/o/r/pull/1#discussion_r2",
            "createdAt": "2026-08-28T14:36:03Z",
        }
        for i, login in enumerate(replies)
    ]
    return {
        "id": "PRRT_test",
        "isResolved": resolved,
        "isOutdated": False,
        "path": "apps/webui/frontend/src/lib/rb/audio-engine.svelte.ts",
        "line": 1267,
        "comments": {"nodes": comments},
    }
