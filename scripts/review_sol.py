"""Sol reviewer identity, and the Codex-or-Sol substitution policy.

Sol is GPT-5.6 driven through the Codex CLI on a ChatGPT subscription seat
(`scripts/sol_review.py` is the lane that runs it). It exists because the
GitHub Codex app -- `chatgpt-codex-connector[bot]`, the sole entry in
`review_coverage.EXPECTED_REVIEWERS` since issue #1016 -- ran out of quota at
about 18:30Z on Thu 4 Sep 2026 and has since posted a usage-limit notice in
place of every review (issue #1211). Coverage correctly reported MISS on every
PR from that moment, and PRs merged un-reviewed anyway, which is the failure
the gate was built to make visible rather than to survive.

WHY THIS IS A SEPARATE MODULE, and not three more constants in
`review_coverage.py`: two readers need the SAME definition of "a Sol review
artifact" and they sit on opposite sides of the wire. `sol_review.py` WRITES
the marker; `review_coverage.py` and `review_thread_parse.py` READ it. A
marker whose writer and reader can drift is a gate that silently stops
counting, so the string is defined once, here, and neither side is allowed its
own copy. (It also keeps `review_coverage.py` under this repo's 600-line
ceiling, but that is a consequence, not the reason.)

IDENTITY TAKES TWO SIGNALS, AND NEITHER IS SUFFICIENT ALONE. Codex is
identified by its bot login, because only that bot can post under it. Sol has
no bot account: the lane posts through `gh` as the maintainer, so:

  - Login alone would mean any review the maintainer writes by hand, or any ordinary
    comment he leaves, satisfies reviewer coverage. That is a human
    self-review clearing the reviewer gate.
  - Marker alone would mean ANY account that can comment here passes
    coverage without a review existing. The repo is private, so that is not
    the whole internet, but it is every collaborator and every one of the
    many agent tokens that comment on these PRs daily. Same hole issue #1016
    thread r3929765931 found when `chatgpt-codex-connector-attacker` matched
    Codex by substring.

So an artifact counts only when it carries BOTH: a login on the short trusted
list AND a well-formed marker naming the head it reviewed. The marker also
carries the SHA, which is what ties an ISSUE comment (GitHub attaches no
`commit_id` to those) to a specific push.

Requirements (mini-PRD):
  / A lane-written artifact at the current head is recognized.
    [if a real Sol review cannot satisfy coverage then broken] -- without
    this row an always-reject matcher passes every other row here
  / A marker pasted by anyone else is not recognized.
    [if a non-trusted login carrying a valid marker counts then broken]
  / A trusted login WITHOUT a marker is not recognized.
    [if the maintainer's own plain review comment counts as a Sol review then broken]
  / A marker naming an earlier push does not certify the current one.
    [if a stale-SHA marker counts at the current head then broken]
  / Either Codex or Sol reviewing covers the pair; neither reviewing does not.
    [if a PR nobody reviewed reports covered then broken]
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import cycle guard, typing only
    from scripts.review_coverage import ReviewerVerdict

#: The reviewer name Sol reports under in the coverage board.
SOL = "Sol"

#: The name Sol stands in for. Both are named in `review_coverage`'s
#: EXPECTED_REVIEWERS so the board says which one actually ran.
CODEX = "Codex"

#: Logins the lane is allowed to post under. Sol has no bot account, so this
#: is the human account the lane's `gh` token authenticates as. Kept as a set
#: rather than a single string because a future service account posting the
#: same marker should be added here, NOT matched by loosening the marker.
SOL_LOGINS: frozenset[str] = frozenset({"maintainer"})

#: The machine-readable marker every lane-written artifact carries, matched
#: with an ANCHORED key order so a hand-typed near-miss fails loudly rather
#: than half-counting. `v1` is a format version: a later shape gets `v2` and
#: this reader learns it explicitly.
SOL_MARKER = re.compile(
    r"<!--\s*sol-review\s+v1\s+sha=([0-9a-f]{7,40})\s+seat=(\S+)\s+model=(\S+)"
    r"(?:\s+skipped=([\w.,/-]+))?\s*-->",
    re.IGNORECASE,
)


def marker(sha: str, seat: str, model: str, skipped: frozenset[str] = frozenset()) -> str:
    """The marker the lane writes into every artifact it posts."""
    tail = (
        f" skipped={','.join(sorted(skipped))}" if skipped else ""
    )
    return f"<!-- sol-review v1 sha={sha} seat={seat} model={model}{tail} -->"


def skipped_paths_from_marker(body: str) -> frozenset[str]:
    match = SOL_MARKER.search(body or "")
    if not match or not match.group(4):
        return frozenset()
    return frozenset(match.group(4).split(","))


def marker_skipped_mismatch(body: str, diff: str) -> str | None:
    from scripts.review_lane import skipped_paths_match_diff

    return skipped_paths_match_diff(skipped_paths_from_marker(body), diff)


def _normalize_login(login: str) -> str:
    return login.removesuffix("[bot]").lower()


def is_sol_artifact(login: str, body: str, head_sha: str) -> bool:
    """Was this artifact written by the Sol lane, against THIS push?

    All three conditions are load-bearing; see the module docstring for what
    each one alone lets through. The SHA comparison is a prefix test in the
    same direction `review_coverage._body_is_at_head` uses, because GitHub
    abbreviates SHAs and the marker may legitimately carry a short one.
    """
    if _normalize_login(login) not in SOL_LOGINS:
        return False
    match = SOL_MARKER.search(body or "")
    if not match:
        return False
    return head_sha.lower().startswith(match.group(1).lower())


def is_sol_thread(login: str, body: str) -> bool:
    """Is this thread one the Sol lane opened, at ANY head?

    Used by thread triage, which asks "is this governed by the three-state
    rule". That is a question about AUTHORSHIP, not about which push is
    certified, so the head is deliberately not consulted: an outdated Sol
    finding still owes a disposition.

    The trusted login is required here for the same reason it is required in
    `is_sol_artifact`, and the marker-only version was a real hole: anyone who
    can comment could paste a marker into a P0-shaped comment and force it
    into the mandatory disposition gate, blocking a merge with a thread no
    reviewer wrote. Sol's own review of this module found it (P1, Fri 5 Sep
    2026); the coverage side already required both signals, and triage
    quietly did not.
    """
    return _normalize_login(login) in SOL_LOGINS and bool(SOL_MARKER.search(body or ""))


def substitute_alternatives(
    verdicts: list[ReviewerVerdict], alternatives: tuple[str, ...]
) -> list[ReviewerVerdict]:
    """Let a real review by ANY of `alternatives` stand in for the others.

    Codex, Sol and Claude are three routes to the same thing -- a frontier
    model reading the diff -- so requiring ALL of them would mean the gate can
    only go green while three independent quotas hold at once, and a gate that
    cannot go green blocks all work (the reasoning
    `KNOWN_UNAVAILABLE_REVIEWERS` was built on). Requiring ANY keeps the gate
    honest in the direction that matters: a PR nobody reviewed still reports
    MISS on every row and fails.

    `alternatives` is passed in rather than read from a constant here so that
    the set lives beside `EXPECTED_REVIEWERS`, which is what it must always
    equal; a private copy in this module would be a second list to forget to
    update the next time a reviewer is added or removed. It started as a
    hard-coded Codex/Sol pair and became a parameter when the Claude lane
    landed (Sun 6 Sep 2026).

    The stand-in is recorded in `substituted_by` rather than being folded into
    the reason string, so the board can print `sub` instead of `ok` and never
    claim Codex reviewed when Codex did not. Returns a new list; the inputs
    are frozen dataclasses and are not mutated.
    """
    reviewed = {v.name for v in verdicts if v.reviewed}
    out = []
    for verdict in verdicts:
        others = [n for n in alternatives if n != verdict.name and n in reviewed]
        stand_in = others[0] if others else ""
        if verdict.reviewed or verdict.name not in alternatives or not stand_in:
            out.append(verdict)
            continue
        out.append(
            type(verdict)(
                name=verdict.name,
                reviewed=True,
                reason=f"{stand_in} reviewed this head, so this one is not required",
                in_progress=verdict.in_progress,
                outage=verdict.outage,
                substituted_by=stand_in,
                carried_from=verdict.carried_from,
                carried_paths=verdict.carried_paths,
                carry_proof=verdict.carry_proof,
            )
        )
    return out
