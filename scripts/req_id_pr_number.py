"""New requirement ids carry the PR number (REQHYG-7077, Fri 16 Oct 2026).

Hand-picked "next free" ids collided all the time: 31 renumber commits on main
in the 30 days to Thu 15 Oct 2026, and seven ids defined twice on main that day.
GitHub hands out PR numbers one at a time, so an id built from the PR number
cannot collide. A requirement first defined by PR #7101 is ``LIBM-7101``; a
second one in the same category and PR is ``LIBM-7101b`` (the letter suffix
``scripts/build_reqs_json`` already accepts). Ids that exist on the base keep
their numbers. So do ids listed in ``(private path)``: ids a
long-lived branch already defined, carried unchanged onto main by its slice PRs
(the B2B branch, #7955), because that branch's code markers and its preview build
use those numbers. A second definition of a carried id still fails.

    python -m scripts.req_id_pr_number --base <sha> --head <sha> --pr <N> [--head-ref <ref>]
    python -m scripts.req_id_pr_number --base <sha> --head-worktree --pr <N>

``--head-worktree`` reads the head side from the working tree instead of a commit, so a
tool that repairs ids in place (scripts/ci_mechanical_fix.py) can check its repair before
it stages anything.

Exit 0 when every id new in this diff is the next canonical name for the PR (or
none are new) and no id gained a second definition, 1 with the fix to make when
one does not, 2 ([UNKNOWN]) when a side cannot be read.
A Trunk merge-queue batch PR (head ref ``trunk-merge/...``) or a lander bundle-train PR
(head ref ``lander-train/...``) is skipped: its members were checked on their own PRs and
the batch number is not theirs. The workflow passes ``--head-ref`` only when the author is
that ref's bot (trunk-io[bot], opendj-devops-trunk-merge[bot]) in this repo.

From PR #12882 on, a NEW id may not be a bullet in REQUIREMENTS.md itself: it lives
in ``(private path)/<ID>.md`` (issue #10541 format). Every open PR that
appended to the one ledger conflicted with every other one that did (59 of 203
conflicting PRs on Wed 11 Nov 2026), and the fragment README and lane prompts alone
did not stop it (132 ledger commits on main against 77 fragment commits in its first
day). The same holds for the dated "recorded" log lines appended at its end (33 of
the 221 open ledger PRs add one). Flipping or editing an existing bullet in place stays allowed. Lower PR numbers
keep the old rule so in-flight PRs are not failed by a rule they predate; their new
ledger bullets are converted to fragments when main is merged into them.

What could satisfy this without its intent: an id that is new but not parsed as
a bullet. The id set is read with build_reqs_json's own id pattern, so a bullet
that the generator would see is a bullet this sees.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

from scripts.build_reqs_json import _REQ_ID, FRAGMENTS_REL, fragments_text_at, fragments_text_in

SOURCE = ".planning/REQUIREMENTS.md"
#: First PR number whose new ids may not be bullets in SOURCE itself (fragments only).
MONOLITH_CLOSED_FROM_PR = 12882
CARRIED = "(private path)"
#: Head refs of PRs that batch already-checked members: Trunk merge-queue drafts and lander
#: bundle trains. Only the workflow decides the author is trusted; this only matches the ref.
BATCH_HEAD_REF_PREFIXES = ("trunk-merge/", "lander-train/")
_BULLET = re.compile(rf"^\s*-\s+(?:\[[ x]\]\s+)?\*\*({_REQ_ID})\*\*")
_NUMBERED = re.compile(r"^(?P<code>.+)-(?P<num>\d+)(?P<suffix>[a-z]?)$")
_SUFFIXES = ["", *"bcdefghijklmnopqrstuvwxyz"]


def defined_ids(text: str) -> Counter[str]:
    """Every defined id with how many bullets define it (a duplicate is a count above 1)."""
    return Counter(m.group(1) for line in text.splitlines() if (m := _BULLET.match(line)))


def _code(rid: str) -> str:
    m = _NUMBERED.match(rid)
    return m.group("code") if m else rid.rsplit("-", 1)[0]


def _order(rid: str) -> tuple[int, str, str]:
    m = _NUMBERED.match(rid)
    return (int(m.group("num")), m.group("suffix"), rid) if m else (sys.maxsize, "", rid)


def _show(git: str, rev: str, path: str = SOURCE) -> tuple[str | None, str]:
    """The file's text at rev ("" when it does not exist there), or None plus git's stderr."""
    proc = subprocess.run([git, "show", f"{rev}:{path}"], capture_output=True, text=True, encoding="utf-8", check=False)
    if proc.returncode == 0:
        return proc.stdout, ""
    if "does not exist" in proc.stderr or "exists on disk, but not in" in proc.stderr:
        return "", ""
    return None, f"git show {rev}:{path} exited {proc.returncode}: {proc.stderr.strip() or '(no stderr)'}"


def _with_fragments(git: str, rev: str, shown: tuple[str | None, str]) -> tuple[str | None, str]:
    """REQUIREMENTS.md text at rev plus every (private path) fragment there (issue #10541)."""
    text, err = shown
    if text is None:
        return None, err
    fragments, frag_err = fragments_text_at(git, rev)
    if fragments is None:
        return None, frag_err
    return text + "\n" + fragments, ""


def _read_worktree(path: str) -> str:
    """The working-tree file's text, "" when it does not exist (the same answer _show gives for a missing file)."""
    file = Path(path)
    return file.read_text(encoding="utf-8") if file.is_file() else ""


def carried_ids(text: str) -> frozenset[str]:
    """Ids the carried-ids file exempts from the rename, whichever PR they came from."""
    data = json.loads(text) if text.strip() else {}
    return frozenset(rid for ids in data.get("carried", {}).values() for rid in ids)


def check(base_ids: Counter[str], head_ids: Counter[str], pr: int, carried: frozenset[str] = frozenset()) -> list[str]:
    """Return one failure line per new id that is not the next canonical name for its code.

    Per code, this PR's ids are CODE-PR, CODE-PRb, CODE-PRc, ... in that order, so
    LIBM-007101, LIBM-7101a and a lone LIBM-7101z all fail with the rename. A new
    definition of an id that is already defined (on the base or twice in the head)
    fails too: that duplicate is the collision this check exists to prevent.
    """
    failures: list[str] = []
    taken = set(head_ids) | set(base_ids)
    by_code: dict[str, list[str]] = {}
    for rid in sorted(set(head_ids) - set(base_ids) - carried, key=_order):
        by_code.setdefault(_code(rid), []).append(rid)
    for code, rids in by_code.items():
        free = [f"{code}-{pr}{s}" for s in _SUFFIXES if f"{code}-{pr}{s}" not in base_ids]
        expected = free[: len(rids)]
        spare = [name for name in expected if name not in rids]
        for rid in rids:
            if rid in expected:
                continue
            target = spare.pop(0) if spare else _next_free(code, pr, taken)
            taken.add(target)
            failures.append(f"[FAIL] new requirement {rid} must use PR #{pr}: rename it to {target}")
    for rid, n in sorted(head_ids.items()):
        for _ in range(n - max(base_ids[rid], 1)):
            target = _next_free(_code(rid), pr, taken)
            taken.add(target)
            failures.append(f"[FAIL] requirement {rid} is defined {n} times: rename the new copy to {target}")
    return failures


#: The dated "_<day> <date>: ID recorded (...)_" log at the end of SOURCE, the ledger's second append point.
_RECORDED_LOG = re.compile(r"^_.*\brecorded\b")


def monolith_failures(base_md: str, head_md: str, pr: int) -> list[str]:
    """One failure per id a PR from MONOLITH_CLOSED_FROM_PR on adds as a bullet in REQUIREMENTS.md itself.

    ``base_md`` and ``head_md`` are SOURCE alone, never stitched with fragments: the
    point is where the new bullet lives. Ids already in SOURCE at the base, edited or
    not, and ids that leave SOURCE are never a failure.
    """
    if pr < MONOLITH_CLOSED_FROM_PR:
        return []
    new = set(defined_ids(head_md)) - set(defined_ids(base_md))
    failures = [
        f"[FAIL] new requirement {rid} is a bullet in {SOURCE}: add it as {FRAGMENTS_REL}/{rid}.md "
        f"(format in {FRAGMENTS_REL}/README.md) and remove it from {SOURCE}"
        for rid in sorted(new, key=_order)
    ]
    base_log = {line.strip() for line in base_md.splitlines() if _RECORDED_LOG.match(line)}
    failures.extend(
        f"[FAIL] new 'recorded' log line in {SOURCE}: delete it, the fragment and its PR already record it: "
        f"{line.strip()[:80]}"
        for line in head_md.splitlines()
        if _RECORDED_LOG.match(line) and line.strip() not in base_log
    )
    return failures


def _next_free(code: str, pr: int, taken: set[str]) -> str:
    return next(
        (f"{code}-{pr}{s}" for s in _SUFFIXES if f"{code}-{pr}{s}" not in taken),
        f"{code}-{pr} plus an unused letter suffix",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", required=True)
    head = parser.add_mutually_exclusive_group(required=True)
    head.add_argument("--head", help="the head commit to read the requirements from")
    head.add_argument(
        "--head-worktree", action="store_true", help="read the head side from the working tree, not a commit"
    )
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--head-ref", default="")
    args = parser.parse_args(argv)

    if args.head_ref.startswith(BATCH_HEAD_REF_PREFIXES):
        print(f"[OK] skipped: {args.head_ref} is a merge batch; members were checked on their own PRs")
        return 0
    if args.pr < 1:
        print("[UNKNOWN] PR number must be positive", file=sys.stderr)
        return 2
    git = shutil.which("git")
    if git is None:
        print("[UNKNOWN] git is not on PATH, so neither side can be read", file=sys.stderr)
        return 2
    head_label = "the working tree" if args.head_worktree else args.head
    base_text, base_err = _with_fragments(git, args.base, _show(git, args.base))
    head_text, head_err = (
        (_read_worktree(SOURCE) + "\n" + fragments_text_in(Path(FRAGMENTS_REL)), "")
        if args.head_worktree
        else _with_fragments(git, args.head, _show(git, args.head))
    )
    if base_text is None or head_text is None:
        print(f"[UNKNOWN] cannot read {SOURCE} at {args.base} or {head_label}", file=sys.stderr)
        for err in (base_err, head_err):
            if err:
                print(f"  {err}", file=sys.stderr)
        return 2
    base_ids, head_ids = defined_ids(base_text), defined_ids(head_text)
    if base_ids and not head_text.strip():
        print(f"[UNKNOWN] {SOURCE} is missing or empty at {head_label} but defines ids at {args.base}", file=sys.stderr)
        return 2
    if head_text and not head_ids:
        print(
            f"[UNKNOWN] {SOURCE} at {head_label} defines no ids: the parser is looking at the wrong thing",
            file=sys.stderr,
        )
        return 2
    carried_text = _read_worktree(CARRIED) if args.head_worktree else _show(git, args.head, CARRIED)[0]
    head_md = _read_worktree(SOURCE) if args.head_worktree else _show(git, args.head)[0]
    base_md = _show(git, args.base)[0]
    failures = check(base_ids, head_ids, args.pr, carried_ids(carried_text or "")) + monolith_failures(
        base_md or "", head_md or "", args.pr
    )
    for line in failures:
        print(line, file=sys.stderr)
    if failures:
        return 1
    new = len(set(head_ids) - set(base_ids))
    print(f"[OK] {new} new requirement id(s), all numbered with PR #{args.pr}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
