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

Equal patch-ids alone do not prove the hunk still sits where the reviewer saw
it: patch-id hashes a hunk's content (context plus change) but not its
position, by design, so it cannot tell two occurrences of the SAME content
apart (found live by Sol on this PR, scripts/review_coverage_base_merge.py#167:
a merge that moves an edit between two repeated blocks with identical
three-line context produces the same patch-id, even though the edit now
changes a different block's behavior). A carry therefore also requires every
hunk's exact context-plus-removed slice (its "pre-image") to occur exactly
once in the base file it is diffed against, at both the reviewed and the head
commit. Ignoring the hunk's own line number stays safe (that is what lets an
unrelated insertion elsewhere carry); trusting a non-unique slice is not, so a
duplicated pre-image fails closed to no carry rather than being read as a
match.

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
  / A hunk relocated to a different, textually identical, spot does not carry.
    [if] a merge relocates a hunk between two repeated identical-context blocks
         and coverage still carries [then broken]
    [if] an unrelated same-file insertion with no duplicate content blocks
         carry [then broken]
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
    diff: bytes


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
    diff = _git(root, "diff", *NET_DIFF_FLAGS, base, sha)
    return NetDiff(sha, base, patch_id_of(root, diff), diff)


def own_commits_since(root: Path, tip: str, reviewed: str, head: str) -> tuple[str, ...]:
    """Non-merge commits on `head` that are neither in `reviewed` nor on main."""
    out = _git(root, "rev-list", "--no-merges", head, f"^{reviewed}", f"^{tip}")
    return tuple(out.decode().split())


# ----------------------------------------------------------------------------
# hunk relocation: a matching patch-id is necessary but not sufficient


def _hunk_preimages(diff: bytes) -> tuple[tuple[str, tuple[bytes, ...]], ...]:
    """Per hunk: (base-side path, the context+removed lines the hunk rewrites).

    That slice is exactly what the hunk claims to sit on top of in the base
    file. patch-id strips the `@@ -a,b +c,d @@` line numbers before hashing,
    so two hunks with equal patch-ids can still rewrite DIFFERENT slices when
    the slice recurs more than once in the base file; this is the raw material
    for the uniqueness check that catches that (`_unique_in_base`).
    """
    hunks: list[tuple[str, tuple[bytes, ...]]] = []
    path: str | None = None
    preimage: list[bytes] = []

    def flush() -> None:
        if path is not None and preimage:
            hunks.append((path, tuple(preimage)))

    for line in diff.split(b"\n"):
        if line.startswith(b"--- "):
            flush()
            preimage = []
            path = None if line == b"--- /dev/null" else line[len("--- a/") :].decode()
        elif line.startswith(b"@@ "):
            flush()
            preimage = []
        elif path is not None and line[:1] in (b" ", b"-"):
            preimage.append(line[1:])
    flush()
    return tuple(hunks)


def _unique_in_base(root: Path, base: str, path: str, preimage: tuple[bytes, ...]) -> bool:
    """Whether `preimage` occurs exactly once as a contiguous block of `path` at `base`."""
    if not preimage:
        return True
    content = tuple(_git(root, "show", f"{base}:{path}").split(b"\n"))
    width = len(preimage)
    matches = sum(1 for i in range(len(content) - width + 1) if content[i : i + width] == preimage)
    return matches == 1


def _hunks_map_unambiguously(root: Path, net: NetDiff) -> bool:
    return all(_unique_in_base(root, net.base, path, preimage) for path, preimage in _hunk_preimages(net.diff))


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
    if not (_hunks_map_unambiguously(root, at_reviewed) and _hunks_map_unambiguously(root, at_head)):
        return None
    return BaseMergeCarry(at_reviewed, at_head, tip)
