"""Base-merge carry rule for reviewer coverage (REVIEW-12, ADR-NEW-review-coverage-base-merge-carry).

Coverage is keyed to the PR's current head, so a plain `git merge origin/main`
into a PR used to void every review even when the PR's own change was
untouched. Measured live on PR #4426, Thu 1 Oct 2026: fully reviewed at
80208dca8, a clean merge of main moved the head to 2b2140757, all three
reviewers read MISS, and a fresh Sol run cost about 36K tokens.

This rule carries a review at an earlier head R to the current head H when the
PR's NET change against its base is identical at both heads, which is the same
test GitHub applies before dismissing a stale approval:

    tip    = live main, pinned by SHA (`pin_remote_branch`, no FETCH_HEAD)
    net(X) = git diff --binary $(git merge-base tip X) X
    carry iff  patch-id(net(R)) == patch-id(net(H))   (git patch-id --verbatim)
          and  no NON-merge commit sits in R..H outside main (base merges only)

`--verbatim` and not `--stable`: `--stable` strips whitespace before hashing,
so re-indenting a Python block in a conflict resolution would keep the
patch-id and carry. Hunk-header line numbers are still ignored, so main
inserting lines elsewhere in a file the PR touches keeps the patch-id, while a
main change inside a reviewed hunk's context lines does not.

The second condition keeps the PASS line's "base merge only" claim true: a
revert pair pushed after review leaves the net diff unchanged too, but nobody
reviewed that it nets to nothing, so it does not carry.

Fail closed. Every git step that cannot be computed (shallow clone, missing
object, no or several merge-bases, empty net diff, an old git without
`--verbatim`) raises CarryUnknown, which the caller renders as `carry UNKNOWN`
on a MISS row. It is never read as a carry.

Requirements (mini-PRD):
  / A clean merge of main that leaves the PR's net diff unchanged carries.
    [if] a clean base merge still reads MISS [then broken]
    [if] a main change far from the PR's hunks in a shared file blocks carry [then broken]
  / Any change to the PR's own net diff needs a fresh review.
    [if] a new commit on the PR's own file carries [then broken]
    [if] a conflict resolution that alters the net diff carries [then broken]
    [if] a whitespace-only change to a reviewed line keeps the patch-id [then broken]
  / An unmeasurable carry is UNKNOWN, never a carry.
    [if] a missing reviewed-head object or a shallow clone carries [then broken]
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from scripts.review_gate_freshness import CANONICAL_BRANCH, CANONICAL_REMOTE, pin_remote_branch
from scripts.review_gh import TriageError

#: `--binary` so two different binary edits never share a "Binary files
#: differ" line; `--no-renames`/`--no-ext-diff`/`--no-textconv` so user git
#: config cannot change what is hashed. `--unified=3` pins the context width
#: the carry contract relies on (a reviewed hunk's three surrounding lines):
#: `diff.context=0` would drop those lines from the diff entirely, so a main
#: change inside them would vanish from both patch-ids and wrongly carry.
#: `--src-prefix`/`--dst-prefix` pin the `a/`/`b/` path prefixes so
#: `diff.noprefix` cannot change the "diff --git" header line that patch-id
#: hashes. Every flag here exists because a plausible git config value was
#: proven, not assumed, to change the resulting patch-id.
NET_DIFF_FLAGS: tuple[str, ...] = (
    "--binary",
    "--no-color",
    "--no-ext-diff",
    "--no-textconv",
    "--no-renames",
    "--unified=3",
    "--src-prefix=a/",
    "--dst-prefix=b/",
)
PATCH_ID_ARGS: tuple[str, ...] = ("patch-id", "--verbatim")


class CarryUnknown(Exception):
    """A base-merge carry input could not be measured. Never read as a verdict."""


@dataclass(frozen=True)
class NetDiff:
    sha: str
    base: str
    patch_id: str


@dataclass(frozen=True)
class BaseMergeCarry:
    reviewed: NetDiff
    head: NetDiff
    base_tip: str

    def proof_lines(self) -> tuple[str, ...]:
        return (
            f"  base-merge carry: reviewed {self.reviewed.sha} -> head {self.head.sha}",
            f"    net diff patch-id (git patch-id --verbatim), equal at both heads: {self.head.patch_id}",
            f"    merge-base with {CANONICAL_REMOTE}/{CANONICAL_BRANCH} {self.base_tip}: "
            f"reviewed {self.reviewed.base}, head {self.head.base}",
        )


# ----------------------------------------------------------------------------
# git plumbing: every failure is CarryUnknown


def _git(root: Path, *args: str, stdin: bytes | None = None) -> bytes:
    proc = subprocess.run(["git", "-C", str(root), *args], input=stdin, capture_output=True, check=False)
    if proc.returncode != 0:
        detail = proc.stderr.decode(errors="replace").strip() or "<no stderr>"
        raise CarryUnknown(f"git {' '.join(args)} failed ({proc.returncode}): {detail}")
    return proc.stdout


def require_full_history(root: Path) -> None:
    if _git(root, "rev-parse", "--is-shallow-repository").strip() != b"false":
        raise CarryUnknown(f"{root} is a shallow clone, so merge-base cannot be trusted")


def pin_base_tip(root: Path) -> str:
    try:
        return pin_remote_branch(root, CANONICAL_REMOTE, CANONICAL_BRANCH)
    except TriageError as exc:
        raise CarryUnknown(f"cannot pin {CANONICAL_REMOTE}/{CANONICAL_BRANCH}: {exc}") from exc


def _single_merge_base(root: Path, tip: str, sha: str) -> str:
    bases = _git(root, "merge-base", "--all", tip, sha).decode().split()
    if len(bases) != 1:
        raise CarryUnknown(f"{len(bases)} merge-bases between {tip} and {sha}; net diff is ambiguous")
    return bases[0]


def patch_id_of(root: Path, diff: bytes) -> str:
    fields = _git(root, *PATCH_ID_ARGS, stdin=diff).split()
    if not fields:
        raise CarryUnknown("net diff is empty, so it has no patch-id to compare")
    return fields[0].decode()


def net_diff(root: Path, tip: str, sha: str) -> NetDiff:
    base = _single_merge_base(root, tip, sha)
    return NetDiff(sha, base, patch_id_of(root, _git(root, "diff", *NET_DIFF_FLAGS, base, sha)))


def own_commits_since(root: Path, tip: str, reviewed: str, head: str) -> tuple[str, ...]:
    """Non-merge commits on `head` that are neither in `reviewed` nor on main."""
    out = _git(root, "rev-list", "--no-merges", head, f"^{reviewed}", f"^{tip}")
    return tuple(out.decode().split())


# ----------------------------------------------------------------------------
# the rule


def base_merge_carry(root: Path, tip: str, reviewed: str, head: str) -> BaseMergeCarry | None:
    """A measured answer: the carry proof, or None when coverage does not carry.

    Raises CarryUnknown when the answer cannot be measured.
    """
    if reviewed == head:
        return None
    if own_commits_since(root, tip, reviewed, head):
        return None
    at_reviewed = net_diff(root, tip, reviewed)
    at_head = net_diff(root, tip, head)
    if at_reviewed.patch_id != at_head.patch_id:
        return None
    return BaseMergeCarry(at_reviewed, at_head, tip)
