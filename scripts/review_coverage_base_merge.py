"""Base-merge carry rule for reviewer coverage (REVIEW-16, ADR-NEW-review-coverage-base-merge-carry).

Coverage is keyed to the PR's current head, so a plain `git merge origin/main`
into a PR used to void every review even when the PR's own change was
untouched. Measured live on PR #4426, Thu 1 Oct 2026: fully reviewed at
80208dca8, a clean merge of main moved the head to 2b2140757, all three
reviewers read MISS, and a fresh Sol run cost about 36K tokens.

The rule is the "disjoint paths" rule merge-queue tools use. It is sound by
construction and matches no content, so there is no heuristic to leak:

    tip      = live main, pinned by SHA (`pin_remote_branch`, no FETCH_HEAD)
    B0, B1   = the single merge-base of tip with R (reviewed) and with H (head)
    main     = paths changed in B0..B1          (git diff --name-only -z --no-renames)
    pr       = paths changed in B0..R and B1..H
    carry iff  (1) every first-parent commit in R..H is a merge, and no
                   non-merge commit sits in R..H outside main
           and (2) main and pr share no path
           and (3) net(B0, R) == net(B1, H), byte for byte
           and (4) every merge in R..H has exactly the tree of
                   `git merge-tree --write-tree` of its two parents

With disjoint paths, every PR path has the same content at B0 and B1, so the
two net diffs have byte-identical pre-images and an exact text compare,
hunk headers included, proves the PR's files are identical at R and H. Paths
outside the PR are untouched by the PR at R (B0..R names none of them), and
(3) forces B1..H to name exactly the same paths as B0..R, so H's tree outside
the PR equals B1's: no hand edit to any other file rode in with the merge.
The net diffs need no pathspec: each already contains only its own changed
paths, which is (3)'s path set restricted to that side. (3) only sees the
endpoints, so (4) checks every merge on the way: a hand edit or conflict
resolution in one merge that a later merge restores is still refused
(Sol P2 on PR #4599).

Superseded after three Sol BLOCKING P1s on PR #4599: equal `git patch-id`s
plus a unique-pre-image check. Patch-id ignores hunk position by design, and
each fix to restore position (pinning diff flags, requiring a unique
pre-image) left another way to move a reviewed edit to a different block.

Trade-off: any main change to a file the PR touches now needs a fresh review,
even one far from the PR's hunks. It carries less often, but never wrongly.

Fail closed. Every git step that cannot be computed (shallow clone, missing
object, no or several merge-bases, empty net diff) raises CarryUnknown, which
the caller renders as `carry UNKNOWN` on a MISS row. It is never read as a
carry.

Requirements (mini-PRD):
  / A clean merge of main that changes only paths the PR does not touch carries.
    [if] a clean base merge of unrelated files still reads MISS [then broken]
    [if] a PR path with spaces or non-ASCII characters breaks the carry [then broken]
  / Any overlap with main, or any edit made inside the merge, needs a fresh review.
    [if] a main change to a PR-touched file carries, however far from the hunk [then broken]
    [if] a merge that ports a reviewed edit to another block carries [then broken]
    [if] a hand edit inside the merge, to a PR file or any other file, carries [then broken]
    [if] a hand edit in one merge that a later merge restores carries [then broken]
    [if] a merge whose first parent is main carries [then broken]
  / An unmeasurable carry is UNKNOWN, never a carry.
    [if] a missing reviewed-head object or a shallow clone carries [then broken]
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

from scripts.review_gate_freshness import CANONICAL_BRANCH, CANONICAL_REMOTE, pin_remote_branch
from scripts.review_gh import TriageError

#: Every flag pins something a git config value would otherwise change.
#: `--binary` so a binary edit is its full content; `--full-index` so every
#: `index` line names whole blob ids whatever core.abbrev says (`--binary`
#: does not imply it for text files: the hostile-config test caught that);
#: `--unified=3` against diff.context;
#: `--src-prefix`/`--dst-prefix` against diff.noprefix and diff.mnemonicPrefix;
#: `--no-relative` against diff.relative; `--submodule=short` against
#: diff.submodule; `--ignore-submodules=none` against diff.ignoreSubmodules
#: and per-submodule ignore settings, so a gitlink change is always seen;
#: color, external diff, textconv and renames off.
NET_DIFF_FLAGS: tuple[str, ...] = (
    "--binary",
    "--full-index",
    "--no-color",
    "--no-ext-diff",
    "--no-textconv",
    "--no-renames",
    "--no-relative",
    "--submodule=short",
    "--ignore-submodules=none",
    "--unified=3",
    "--src-prefix=a/",
    "--dst-prefix=b/",
)
NAME_FLAGS: tuple[str, ...] = ("--name-only", "-z", "--no-renames", "--no-relative", "--ignore-submodules=none")


class CarryUnknown(Exception):
    """A base-merge carry input could not be measured. Never read as a verdict."""


@dataclass(frozen=True)
class BaseMergeCarry:
    reviewed: str
    head: str
    reviewed_base: str
    head_base: str
    base_tip: str
    pr_paths: frozenset[bytes]
    main_paths: frozenset[bytes]
    net_diff_sha256: str

    def proof_lines(self) -> tuple[str, ...]:
        return (
            f"  base-merge carry: reviewed {self.reviewed} -> head {self.head}",
            f"    merge-base with {CANONICAL_REMOTE}/{CANONICAL_BRANCH} {self.base_tip}: "
            f"reviewed {self.reviewed_base}, head {self.head_base}",
            f"    disjoint paths: main changed {len(self.main_paths)}, PR changed {len(self.pr_paths)}, shared 0",
            f"    net diff byte-identical at both heads, sha256 {self.net_diff_sha256}",
        )


# ----------------------------------------------------------------------------
# git plumbing: every failure is CarryUnknown


def _git(root: Path, *args: str) -> bytes:
    proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=False)
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


def single_merge_base(root: Path, tip: str, sha: str) -> str:
    bases = _git(root, "merge-base", "--all", tip, sha).decode().split()
    if len(bases) != 1:
        raise CarryUnknown(f"{len(bases)} merge-bases between {tip} and {sha}; net diff is ambiguous")
    return bases[0]


def changed_paths(root: Path, old: str, new: str) -> frozenset[bytes]:
    """Raw repository paths changed between two commits (NUL-separated, so never quoted)."""
    return frozenset(path for path in _git(root, "diff", *NAME_FLAGS, old, new).split(b"\0") if path)


def net_diff(root: Path, base: str, sha: str) -> bytes:
    return _git(root, "diff", *NET_DIFF_FLAGS, base, sha)


# ----------------------------------------------------------------------------
# condition 1: only merges of main since review


def own_commits_since(root: Path, tip: str, reviewed: str, head: str) -> tuple[str, ...]:
    """Non-merge commits on `head` that are neither in `reviewed` nor on main."""
    out = _git(root, "rev-list", "--no-merges", head, f"^{reviewed}", f"^{tip}")
    return tuple(out.decode().split())


def first_parent_chain_is_merges_onto(root: Path, reviewed: str, head: str) -> bool:
    """Whether H's first-parent chain reaches R through merge commits only."""
    rows = [
        row.split()
        for row in _git(root, "rev-list", "--first-parent", "--parents", head, f"^{reviewed}").decode().splitlines()
    ]
    return bool(rows) and all(len(row) >= 3 for row in rows) and rows[-1][1] == reviewed


def merges_are_clean(root: Path, reviewed: str, head: str) -> bool:
    """Whether every first-parent merge in R..H has exactly the tree a clean merge of its parents gives."""
    for row in _git(root, "rev-list", "--first-parent", "--parents", head, f"^{reviewed}").decode().splitlines():
        sha, *parents = row.split()
        if len(parents) != 2:
            return False
        proc = subprocess.run(
            ["git", "-C", str(root), "merge-tree", "--write-tree", *parents], capture_output=True, check=False
        )
        if proc.returncode == 1:
            return False
        if proc.returncode != 0:
            detail = proc.stderr.decode(errors="replace").strip() or "<no stderr>"
            raise CarryUnknown(f"git merge-tree of {sha}'s parents failed ({proc.returncode}): {detail}")
        if proc.stdout.split(b"\n", 1)[0] != _git(root, "rev-parse", f"{sha}^{{tree}}").strip():
            return False
    return True


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
    if not first_parent_chain_is_merges_onto(root, reviewed, head):
        return None
    if not merges_are_clean(root, reviewed, head):
        return None
    reviewed_base = single_merge_base(root, tip, reviewed)
    head_base = single_merge_base(root, tip, head)
    pr_paths = changed_paths(root, reviewed_base, reviewed) | changed_paths(root, head_base, head)
    if not pr_paths:
        raise CarryUnknown(f"net diff of {reviewed} is empty, so there is no reviewed change to carry")
    main_paths = changed_paths(root, reviewed_base, head_base)
    if main_paths & pr_paths:
        return None
    at_reviewed = net_diff(root, reviewed_base, reviewed)
    if at_reviewed != net_diff(root, head_base, head):
        return None
    return BaseMergeCarry(
        reviewed=reviewed,
        head=head,
        reviewed_base=reviewed_base,
        head_base=head_base,
        base_tip=tip,
        pr_paths=pr_paths,
        main_paths=main_paths,
        net_diff_sha256=hashlib.sha256(at_reviewed).hexdigest(),
    )
