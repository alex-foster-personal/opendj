"""REVIEW-13's debt-only carry: a submitted review at an earlier head still counts.

Split out of review_control_plane so that module stays under the 600-line file-size ratchet.
The git rule is review_coverage_carry's own (`is_debt_only_since`); nothing is copied here.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from scripts.review_coverage_carry import (
    _fetch_candidates,
    carry_proof_lines,
    fetch_commits,
    is_debt_only_since,
    paths_between,
    sort_candidates_newest_first,
)

#: Pull-request review states that count as submitted (excludes PENDING and DISMISSED).
SUBMITTED_REVIEW_STATES: frozenset[str] = frozenset({"COMMENTED", "APPROVED", "CHANGES_REQUESTED"})


@dataclass(frozen=True)
class CarriedReview:
    """One reviewer's review at an earlier head, carried over debt-only commits."""

    reviewer: str
    carried_from: str  # full SHA of the reviewed ancestor head
    paths: frozenset[str]  # `git diff --name-only carried_from..head`, printed as proof

    @property
    def phrase(self) -> str:
        return f"{self.reviewer} (carried from {self.carried_from[:11]} (debt-only since))"


@dataclass(frozen=True)
class Carry:
    reviews: tuple[CarriedReview, ...] = ()
    unknown: tuple[str, ...] = ()  # candidates whose carry could not be measured


_NO_CARRY = Carry()


def debt_only_carry(
    pr: str,
    head_sha: str,
    repo_root: Path,
    reviews: Sequence[Mapping],
    reviewed_at: Callable[[str], Mapping[str, bool]],
    at_head: Mapping[str, bool],
) -> Carry:
    """Reviewers with a submitted review at an earlier head, debt-only equivalent to `head_sha`.

    The git rule is review_coverage_carry's own (`is_debt_only_since`); candidates are the heads
    of submitted reviews, newest first. A candidate that cannot be fetched is `unknown`, never a carry.
    """
    candidates = tuple(
        sorted({str(r["commit_id"]) for r in reviews if str(r["state"]).upper() in SUBMITTED_REVIEW_STATES} - {head_sha})
    )
    if not candidates:
        return _NO_CARRY
    fetch_commits(repo_root, head_sha)
    present, unknown = _fetch_candidates(repo_root, candidates)
    carried_by: dict[str, CarriedReview] = {}
    for sha in sort_candidates_newest_first(repo_root, head_sha, present):
        if not is_debt_only_since(repo_root, pr, sha, head_sha):
            continue
        paths = paths_between(repo_root, sha, head_sha)
        for name, reviewed in reviewed_at(sha).items():
            if reviewed and not at_head.get(name) and name not in carried_by:
                carried_by[name] = CarriedReview(name, sha, paths)
    return Carry(tuple(carried_by.values()), tuple(unknown))


def print_carry_proofs(carried: Sequence[CarriedReview], head_sha: str) -> None:
    """Both full SHAs and the local diff path list, once per carried head (as review_coverage does)."""
    seen: set[str] = set()
    for review in carried:
        if review.carried_from in seen:
            continue
        seen.add(review.carried_from)
        for line in carry_proof_lines(review.carried_from, head_sha, review.paths):
            print(line)
