"""Refuse to render a review-gate verdict from gate code older than main's.

Found live on PR #1720, Thu 10 Sep 2026. PR #1731 fixed the inline-comment
head-tie in scripts/review_coverage.py (GitHub re-anchors an inline comment's
`commit_id` to every newer push its line survives into; `original_commit_id`
is the push it was written against) and merged at 13:35Z. Forty minutes later
`just review-triage 1720` printed `PASS ... ok Codex: 1 artifact(s) found` at
head 9e86251a while Codex's review of that head was still Running. The gate
that printed it was not main's: `just review-triage` executes whatever
scripts/review_*.py the CURRENT checkout holds, and #1720's branch predated
#1731, so the PR under review supplied its own, already-fixed-on-main,
false-green gate.

So a fix to the gate protects only checkouts that contain it, and every PR
branch cut before the fix keeps the defect until it is rebased. This module
makes that condition loud: the newest commit on main touching any gate file
must be an ancestor of this checkout's HEAD, or the run is a failed
MEASUREMENT (exit 3), never a verdict. It deliberately does not demand a full
rebase: main moving on in files that are not gate code leaves the gate as
trustworthy as it was, and refusing then would block work for nothing.

Requirements (mini-PRD):
  / A checkout missing main's newest gate commit cannot render a verdict.
    [if] a branch cut before a gate fix on main prints PASS or FAIL [then] broken
  / A checkout containing it proceeds, whatever else main has gained since.
    [if] a non-gate commit on main blocks a checkout that has every gate commit
    [then] broken -- without this, refusing every run satisfies the row above
  / A pathspec that matches nothing on main is a failed measurement.
    [if] a renamed gate directory makes this check pass vacuously [then] broken
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from scripts.review_gh import TriageError

#: Every module whose code decides a review-gate verdict. Broad on purpose:
#: over-matching costs a rebase, under-matching reopens the #1720 false green.
GATE_PATHSPEC: tuple[str, ...] = ("scripts/review_*.py",)
CANONICAL_REMOTE = "origin"
CANONICAL_BRANCH = "main"
#: The checkout whose gate code is ACTUALLY executing, not the caller's CWD.
CHECKOUT_ROOT = Path(__file__).resolve().parents[1]


def _git(root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=False
    )


def _git_out(root: Path, args: list[str]) -> str:
    proc = _git(root, args)
    if proc.returncode != 0:
        raise TriageError(
            f"git {' '.join(args)} failed ({proc.returncode}): "
            f"{proc.stderr.strip() or '<no stderr>'}"
        )
    return proc.stdout.strip()


def pin_remote_branch(root: Path, remote: str, branch: str) -> str:
    """Resolve `remote`'s `branch` to one SHA and make its objects local.

    Pinned via `ls-remote` and fetched BY SHA with `--no-write-fetch-head`:
    FETCH_HEAD is one mutable pointer shared by every agent in this repo (see
    CLAUDE.md), so this neither reads it nor clobbers someone else's.
    """
    advertised = _git_out(root, ["ls-remote", remote, f"refs/heads/{branch}"])
    if not advertised:
        raise TriageError(f"{remote} advertises no refs/heads/{branch}")
    sha = advertised.split()[0]
    _git_out(root, ["fetch", "--quiet", "--no-write-fetch-head", remote, sha])
    return sha


def require_gate_current(root: Path, tip: str, pathspec: tuple[str, ...]) -> str:
    """Raise unless the newest commit at or before `tip` touching `pathspec`
    is an ancestor of `root`'s HEAD. Returns that gate commit's SHA."""
    gate_commit = _git_out(root, ["log", "-1", "--format=%H", tip, "--", *pathspec])
    if not gate_commit:
        raise TriageError(
            f"no commit reachable from {tip} touches {' '.join(pathspec)}; "
            "the gate pathspec is wrong, so gate freshness cannot be measured"
        )
    proc = _git(root, ["merge-base", "--is-ancestor", gate_commit, "HEAD"])
    if proc.returncode == 0:
        return gate_commit
    if proc.returncode == 1:
        subject = _git_out(root, ["log", "-1", "--format=%h %s", gate_commit])
        raise TriageError(
            f"this checkout's review-gate code is STALE: it lacks {subject}, the "
            f"newest gate change on {CANONICAL_BRANCH} ({root}). A stale gate can "
            f"print a false PASS. Run from an up-to-date {CANONICAL_BRANCH} "
            "checkout, or rebase this branch, then re-run"
        )
    raise TriageError(
        f"git merge-base --is-ancestor {gate_commit} HEAD failed "
        f"({proc.returncode}): {proc.stderr.strip() or '<no stderr>'}"
    )


def require_gate_current_with_main() -> str:
    """Production entry: check the executing checkout against live main."""
    tip = pin_remote_branch(CHECKOUT_ROOT, CANONICAL_REMOTE, CANONICAL_BRANCH)
    return require_gate_current(CHECKOUT_ROOT, tip, GATE_PATHSPEC)
