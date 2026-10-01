"""Carry reviewer coverage from an earlier reviewed head (one mechanism, two rules).

Rule 1, debt-only (issues #2907, #2871; ADR-0063): when the PR head advanced
only via `.planning/debt/<pr>.md` commits, accept a reviewer artifact from an
earlier ancestor head instead of forcing a paid re-review. Local
`git diff --name-only` is the source of truth; the PR files API is never
consulted. When carry applies, ``review_coverage.triage()`` prints both SHAs
and the diff path list (issue #2871).

Rule 2, base-merge (REVIEW-12): when the head advanced only by merging main,
main changed no path the PR touches, and the PR's net diff against its base is
byte-identical at both heads (the disjoint-paths rule). See
scripts/review_coverage_base_merge.py. A carry that cannot be measured is
reported as `carry UNKNOWN` on the MISS row, never as a carry.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from scripts.review_claude import CLAUDE, CLAUDE_LOGINS, CLAUDE_MARKER
from scripts.review_coverage_base_merge import (
    CarryUnknown,
    base_merge_carry,
    pin_base_tip,
    require_full_history,
)
from scripts.review_gh import _SHA_IN_BACKTICKS, _STATUS_COMPLETED, TriageError
from scripts.review_lane import diff_of, evidence_skipped_paths_ok
from scripts.review_sol import SOL, SOL_LOGINS, SOL_MARKER, _normalize_login


def debt_file_path(pr: str) -> str:
    return f".planning/debt/{pr}.md"


def _object_exists(root: Path, sha: str) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(root), "cat-file", "-e", f"{sha}^{{commit}}"],
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def fetch_commits(root: Path, *shas: str) -> None:
    """Fetch each SHA from origin without writing FETCH_HEAD (CLAUDE.md)."""
    for sha in shas:
        if _object_exists(root, sha):
            continue
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "fetch",
                "--quiet",
                "--no-write-fetch-head",
                "origin",
                sha,
            ],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            detail = proc.stderr.strip() or proc.stdout.strip() or "unknown error"
            raise TriageError(f"git fetch origin {sha} failed: {detail}")


def is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    proc = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "merge-base",
            "--is-ancestor",
            ancestor,
            descendant,
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    detail = proc.stderr.strip() or proc.stdout.strip() or "unknown error"
    raise TriageError(
        f"git merge-base --is-ancestor {ancestor} {descendant} failed: {detail}"
    )


def carry_proof_lines(
    carried_sha: str,
    head_sha: str,
    paths: frozenset[str],
) -> tuple[str, ...]:
    """Lines printed when coverage is carried (issue #2871)."""
    header = f"  debt-only carry: reviewed {carried_sha} -> head {head_sha}"
    diff_cmd = f"  git diff --name-only {carried_sha}..{head_sha}:"
    path_lines = tuple(f"    {p}" for p in sorted(paths))
    return (header, diff_cmd, *path_lines)


def print_carry_proofs(verdicts, head_sha: str, repo_root: Path) -> None:
    """Print carry proof blocks once per unique (carried, head) pair."""
    seen: set[tuple[str, str]] = set()
    for verdict in verdicts:
        if not verdict.carried_from:
            continue
        pair = (verdict.carried_from, head_sha)
        if pair in seen:
            continue
        seen.add(pair)
        if verdict.carry_proof:
            for line in verdict.carry_proof:
                print(line)
            continue
        paths = (
            verdict.carried_paths
            if verdict.carried_paths
            else paths_between(repo_root, verdict.carried_from, head_sha)
        )
        for line in carry_proof_lines(verdict.carried_from, head_sha, paths):
            print(line)


def paths_between(root: Path, older: str, newer: str) -> frozenset[str]:
    proc = subprocess.run(
        ["git", "-C", str(root), "diff", "--name-only", f"{older}..{newer}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return frozenset(line for line in proc.stdout.splitlines() if line)


def is_debt_only_since(
    root: Path,
    pr: str,
    carried_sha: str,
    head_sha: str,
    *,
    allowed_paths: frozenset[str] | None = None,
) -> bool:
    """True when only this PR's debt file changed on the path from carried to head.

    Empty diff with distinct SHAs fails closed: identical content across commits
    is not a carry path (head-tie handles H == head).
    """
    if carried_sha == head_sha:
        return False
    if not is_ancestor(root, carried_sha, head_sha):
        return False
    paths = paths_between(root, carried_sha, head_sha)
    if not paths:
        return False
    expected = allowed_paths if allowed_paths is not None else frozenset({debt_file_path(pr)})
    return paths == expected


def _sha_completed_in_body(body: str) -> str | None:
    for line in body.splitlines():
        shas = _SHA_IN_BACKTICKS.findall(line)
        if not shas:
            continue
        if _STATUS_COMPLETED.search(line):
            return shas[0]
    return None


def _marker_sha(body: str, marker: re.Pattern[str]) -> str | None:
    match = marker.search(body or "")
    if not match:
        return None
    return match.group(1)


def _authored_by_reviewer(name: str, login: str, body: str, matches_login: Callable[[str, str], bool]) -> bool:
    if name == SOL:
        return _normalize_login(login) in SOL_LOGINS and bool(SOL_MARKER.search(body or ""))
    if name == CLAUDE:
        return _normalize_login(login) in CLAUDE_LOGINS and bool(CLAUDE_MARKER.search(body or ""))
    return matches_login(login, name)


def reviewed_shas_for_reviewer(
    name: str,
    reviews: list[dict],
    inline: list[dict],
    issue_comments: list[dict],
    *,
    matches_login: Callable[[str, str], bool],
) -> tuple[str, ...]:
    shas: set[str] = set()
    marker = SOL_MARKER if name == SOL else CLAUDE_MARKER if name == CLAUDE else None

    for review in reviews:
        login = (review.get("user") or {}).get("login", "")
        body = review.get("body") or ""
        if not _authored_by_reviewer(name, login, body, matches_login):
            continue
        if commit_id := review.get("commit_id"):
            shas.add(commit_id)
        if marker is not None and (sha := _marker_sha(body, marker)):
            shas.add(sha)

    for comment in inline:
        login = (comment.get("user") or {}).get("login", "")
        body = comment.get("body") or ""
        if not _authored_by_reviewer(name, login, body, matches_login):
            continue
        if original := comment.get("original_commit_id"):
            shas.add(original)

    for comment in issue_comments:
        login = (comment.get("user") or {}).get("login", "")
        body = comment.get("body") or ""
        if not _authored_by_reviewer(name, login, body, matches_login):
            continue
        if marker is not None and (sha := _marker_sha(body, marker)):
            shas.add(sha)
        elif sha := _sha_completed_in_body(body):
            shas.add(sha)

    return tuple(shas)


def _commits_since(root: Path, base: str, head: str) -> int:
    proc = subprocess.run(
        ["git", "-C", str(root), "rev-list", "--count", f"{base}..{head}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(proc.stdout.strip())


def sort_candidates_newest_first(root: Path, head_sha: str, candidates: tuple[str, ...]) -> tuple[str, ...]:
    ancestors = [sha for sha in candidates if is_ancestor(root, sha, head_sha)]
    return tuple(sorted(ancestors, key=lambda sha: _commits_since(root, sha, head_sha)))


@dataclass(frozen=True)
class ReviewCarryInputs:
    checks: list[dict]
    evidence: dict
    reviews: list[dict]
    inline: list[dict]
    issue_comments: list[dict]
    repo_root: Path
    expected_reviewers: tuple[str, ...]
    classify_reviewer: Callable


def verdicts_with_carry(
    pr: str,
    head_sha: str,
    inputs: ReviewCarryInputs,
):
    """Classify each reviewer at head, then try to carry coverage on MISS rows."""
    from scripts.review_coverage import ReviewerVerdict

    pr_diff = diff_of(pr)
    verdicts = []
    for name in inputs.expected_reviewers:
        verdict = inputs.classify_reviewer(name, inputs.checks, inputs.evidence.get(name))
        row = inputs.evidence.get(name)
        if verdict.reviewed and row and (reason := evidence_skipped_paths_ok(name, row.bodies, pr_diff)):
            verdict = ReviewerVerdict(name, False, reason)
        if not verdict.reviewed:
            verdict = _apply_carry(verdict, pr, head_sha, inputs)
        verdicts.append(verdict)
    return verdicts


def _apply_carry(verdict, pr: str, head_sha: str, inputs: ReviewCarryInputs):
    attempt = carry_attempt(
        verdict.name, pr, head_sha, inputs.reviews, inputs.inline, inputs.issue_comments, inputs.repo_root
    )
    if attempt.verdict is not None:
        return attempt.verdict
    if attempt.unknown:
        notes = "; ".join(attempt.unknown)
        return replace(verdict, reason=f"{verdict.reason}; carry UNKNOWN ({notes}), so a review at head is required")
    return verdict


@dataclass(frozen=True)
class CarryAttempt:
    """`verdict` is a carried ReviewerVerdict or None; `unknown` names every
    candidate whose carry could not be measured (fail closed, never a carry)."""

    verdict: object | None
    unknown: tuple[str, ...] = ()


def _fetch_candidates(root: Path, candidates: tuple[str, ...]) -> tuple[tuple[str, ...], list[str]]:
    present: list[str] = []
    unknown: list[str] = []
    for sha in candidates:
        try:
            fetch_commits(root, sha)
        except TriageError as exc:
            unknown.append(f"{sha[:11]}: reviewed head not fetchable: {exc}")
            continue
        present.append(sha)
    return tuple(present), unknown


def _reviewed_at(name: str, reviews: list[dict], inline: list[dict], issue_comments: list[dict], sha: str) -> bool:
    from scripts.review_coverage import _classify_from_evidence, _collect_evidence

    return _classify_from_evidence(name, _collect_evidence(name, reviews, inline, issue_comments, sha)).reviewed


class _BaseTip:
    """Pins live main and checks history depth once per attempt, only if needed."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._tip = ""

    def get(self) -> str:
        if not self._tip:
            require_full_history(self._root)
            self._tip = pin_base_tip(self._root)
        return self._tip


def carry_attempt(
    name: str,
    pr: str,
    head_sha: str,
    reviews: list[dict],
    inline: list[dict],
    issue_comments: list[dict],
    repo_root: Path,
) -> CarryAttempt:
    """Try each rule on each reviewed ancestor head, newest first."""
    from scripts.review_coverage import ReviewerVerdict, _matches

    candidates = reviewed_shas_for_reviewer(name, reviews, inline, issue_comments, matches_login=_matches)
    if not candidates:
        return CarryAttempt(None)

    fetch_commits(repo_root, head_sha)
    present, unknown = _fetch_candidates(repo_root, candidates)
    base_tip = _BaseTip(repo_root)
    for carried_sha in sort_candidates_newest_first(repo_root, head_sha, present):
        if not _reviewed_at(name, reviews, inline, issue_comments, carried_sha):
            continue
        short = carried_sha[:11]
        if is_debt_only_since(repo_root, pr, carried_sha, head_sha):
            return CarryAttempt(
                ReviewerVerdict(
                    name=name,
                    reviewed=True,
                    reason=f"carried from {short} (debt-only since)",
                    carried_from=carried_sha,
                    carried_paths=paths_between(repo_root, carried_sha, head_sha),
                )
            )
        try:
            proof = base_merge_carry(repo_root, base_tip.get(), carried_sha, head_sha)
        except CarryUnknown as exc:
            unknown.append(f"{short}: {exc}")
            continue
        if proof is not None:
            return CarryAttempt(
                ReviewerVerdict(
                    name=name,
                    reviewed=True,
                    reason=f"carried from {short} (net diff unchanged since; base merge only)",
                    carried_from=carried_sha,
                    carry_proof=proof.proof_lines(),
                )
            )
    return CarryAttempt(None, tuple(unknown))


def try_carry_verdict(
    name: str,
    pr: str,
    head_sha: str,
    reviews: list[dict],
    inline: list[dict],
    issue_comments: list[dict],
    repo_root: Path,
):
    """Return a carried ReviewerVerdict, or None when carry does not apply."""
    return carry_attempt(name, pr, head_sha, reviews, inline, issue_comments, repo_root).verdict
