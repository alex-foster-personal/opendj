"""Claude reviewer identity: what counts as a Claude-lane review artifact.

A configured alternative reviewer uses the same exact-head coverage contract.

This module is the SHAPE of the reviewer's mark, and nothing else. It sits
apart from `claude_review.py` (the lane that runs the model and posts) for the
same reason `review_sol.py` sits apart from `sol_review.py`: two readers need
the SAME definition of "a Claude review artifact" and they are on opposite
sides of the wire. `claude_review.py` WRITES the marker; `review_coverage.py`
and `review_thread_parse.py` READ it. A marker whose writer and reader can
drift is a gate that silently stops counting, so the string is defined once,
here, and neither side is allowed its own copy.

IDENTITY TAKES TWO SIGNALS, AND NEITHER IS SUFFICIENT ALONE. Codex is
identified by its bot login, because only that bot can post under it. The
Claude lane, like Sol, has no bot account: it posts through `gh` as the maintainer, so:

  - Login alone would mean any review the maintainer writes by hand, or any ordinary
    comment he leaves, satisfies reviewer coverage. That is a human
    self-review clearing the reviewer gate.
  - Marker alone would mean ANY account that can comment here passes coverage
    without a review existing. The repository is private, so that is not the
    whole internet, but it is every collaborator and every one of the many
    agent tokens that comment on these PRs daily. Same hole issue #1016
    thread r3929765931 found when `chatgpt-codex-connector-attacker` matched
    Codex by substring.

So an artifact counts only when it carries BOTH: a login on the short trusted
list AND a well-formed marker naming the head it reviewed. The marker also
carries the SHA, which is what ties an ISSUE comment (GitHub attaches no
`commit_id` to those) to a specific push.

The Claude marker records model and source head; the configured lane has no
seat field. Marker readers and writers share this schema.

Requirements (mini-PRD):
  / A lane-written artifact at the current head is recognized.
    [if a real Claude review cannot satisfy coverage then broken] -- without
    this row an always-reject matcher passes every other row here
  / A marker pasted by anyone else is not recognized.
    [if a non-trusted login carrying a valid marker counts then broken]
  / A trusted login WITHOUT a marker is not recognized.
    [if the maintainer's own plain review comment counts as a Claude review then broken]
  / A marker naming an earlier push does not certify the current one.
    [if a stale-SHA marker counts at the current head then broken]
  / A Claude thread is governed by the three-state rule at ANY head.
    [if a P0 Claude finding can sit unanswered through a merge then broken]
"""

from __future__ import annotations

import re

#: The reviewer name the Claude lane reports under in the coverage board.
CLAUDE = "Claude"

#: Logins the lane is allowed to post under. The lane has no bot account, so
#: this is the human account its `gh` token authenticates as. Kept as a set
#: rather than a single string because a future service account posting the
#: same marker should be added here, NOT matched by loosening the marker.
CLAUDE_LOGINS: frozenset[str] = frozenset({"maintainer"})

#: The machine-readable marker every lane-written artifact carries, matched
#: with an ANCHORED key order so a hand-typed near-miss fails loudly rather
#: than half-counting. `v1` is a format version: a later shape gets `v2` and
#: this reader learns it explicitly.
CLAUDE_MARKER = re.compile(
    r"<!--\s*claude-review\s+v1\s+sha=([0-9a-f]{7,40})\s+model=(\S+)"
    r"(?:\s+skipped=([\w.,/-]+))?\s*-->",
    re.IGNORECASE,
)


def marker(sha: str, model: str, skipped: frozenset[str] = frozenset()) -> str:
    """The marker the lane writes into every artifact it posts."""
    tail = (
        f" skipped={','.join(sorted(skipped))}" if skipped else ""
    )
    return f"<!-- claude-review v1 sha={sha} model={model}{tail} -->"


def skipped_paths_from_marker(body: str) -> frozenset[str]:
    match = CLAUDE_MARKER.search(body or "")
    if not match or not match.group(3):
        return frozenset()
    return frozenset(match.group(3).split(","))


def marker_skipped_mismatch(body: str, diff: str) -> str | None:
    from scripts.review_lane import skipped_paths_match_diff

    return skipped_paths_match_diff(skipped_paths_from_marker(body), diff)


def _normalize_login(login: str) -> str:
    return login.removesuffix("[bot]").lower()


def is_claude_artifact(login: str, body: str, head_sha: str) -> bool:
    """Was this artifact written by the Claude lane, against THIS push?

    All three conditions are load-bearing; see the module docstring for what
    each one alone lets through. The SHA comparison is a prefix test in the
    same direction `review_gh._body_is_at_head` uses, because GitHub
    abbreviates SHAs and the marker may legitimately carry a short one.
    """
    if _normalize_login(login) not in CLAUDE_LOGINS:
        return False
    match = CLAUDE_MARKER.search(body or "")
    if not match:
        return False
    return head_sha.lower().startswith(match.group(1).lower())


def is_claude_thread(login: str, body: str) -> bool:
    """Is this thread one the Claude lane opened, at ANY head?

    Used by thread triage, which asks "is this governed by the three-state
    rule". That is a question about AUTHORSHIP, not about which push is
    certified, so the head is deliberately not consulted: an outdated Claude
    finding still owes a disposition.

    The trusted login is required here for the same reason it is required in
    `is_claude_artifact`: marker-only was a real hole on the Sol side (P1,
    Fri 5 Sep 2026), where anyone who could comment could paste a marker into
    a P0-shaped comment and force it into the mandatory disposition gate,
    blocking a merge with a thread no reviewer wrote.
    """
    return _normalize_login(login) in CLAUDE_LOGINS and bool(CLAUDE_MARKER.search(body or ""))
