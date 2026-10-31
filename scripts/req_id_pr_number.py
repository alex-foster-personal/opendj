"""New requirement ids carry the PR number (REQHYG-7077, Fri 16 Oct 2026).

Hand-picked "next free" ids collided all the time: 31 renumber commits on main
in the 30 days to Thu 15 Oct 2026, and seven ids defined twice on main that day.
GitHub hands out PR numbers one at a time, so an id built from the PR number
cannot collide. A requirement first defined by PR #7101 is ``LIBM-7101``; a
second one in the same category and PR is ``LIBM-7101b`` (the letter suffix
``scripts/build_reqs_json`` already accepts). Ids that exist on the base keep
their numbers.

    python -m scripts.req_id_pr_number --base <sha> --head <sha> --pr <N> [--head-ref <ref>]

Exit 0 when every id new in this diff is the next canonical name for the PR (or
none are new) and no id gained a second definition, 1 with the fix to make when
one does not, 2 ([UNKNOWN]) when a side cannot be read.
A Trunk merge-queue batch PR (head ref ``trunk-merge/...``) is skipped: its
members were checked on their own PRs and the batch number is not theirs.

What could satisfy this without its intent: an id that is new but not parsed as
a bullet. The id set is read with build_reqs_json's own id pattern, so a bullet
that the generator would see is a bullet this sees.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from collections import Counter

from scripts.build_reqs_json import _REQ_ID

SOURCE = ".planning/REQUIREMENTS.md"
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


def _show(git: str, rev: str) -> tuple[str | None, str]:
    """The file's text at rev ("" when it does not exist there), or None plus git's stderr."""
    proc = subprocess.run([git, "show", f"{rev}:{SOURCE}"], capture_output=True, text=True, encoding="utf-8", check=False)
    if proc.returncode == 0:
        return proc.stdout, ""
    if "does not exist" in proc.stderr or "exists on disk, but not in" in proc.stderr:
        return "", ""
    return None, f"git show {rev}:{SOURCE} exited {proc.returncode}: {proc.stderr.strip() or '(no stderr)'}"


def check(base_ids: Counter[str], head_ids: Counter[str], pr: int) -> list[str]:
    """Return one failure line per new id that is not the next canonical name for its code.

    Per code, this PR's ids are CODE-PR, CODE-PRb, CODE-PRc, ... in that order, so
    LIBM-007101, LIBM-7101a and a lone LIBM-7101z all fail with the rename. A new
    definition of an id that is already defined (on the base or twice in the head)
    fails too: that duplicate is the collision this check exists to prevent.
    """
    failures: list[str] = []
    taken = set(head_ids) | set(base_ids)
    by_code: dict[str, list[str]] = {}
    for rid in sorted(set(head_ids) - set(base_ids), key=_order):
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


def _next_free(code: str, pr: int, taken: set[str]) -> str:
    return next(
        (f"{code}-{pr}{s}" for s in _SUFFIXES if f"{code}-{pr}{s}" not in taken),
        f"{code}-{pr} plus an unused letter suffix",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--head-ref", default="")
    args = parser.parse_args(argv)

    if args.head_ref.startswith("trunk-merge/"):
        print(f"[OK] skipped: {args.head_ref} is a merge-queue batch; members were checked on their own PRs")
        return 0
    if args.pr < 1:
        print("[UNKNOWN] PR number must be positive", file=sys.stderr)
        return 2
    git = shutil.which("git")
    if git is None:
        print("[UNKNOWN] git is not on PATH, so neither side can be read", file=sys.stderr)
        return 2
    (base_text, base_err), (head_text, head_err) = _show(git, args.base), _show(git, args.head)
    if base_text is None or head_text is None:
        print(f"[UNKNOWN] cannot read {SOURCE} at {args.base} or {args.head}", file=sys.stderr)
        for err in (base_err, head_err):
            if err:
                print(f"  {err}", file=sys.stderr)
        return 2
    base_ids, head_ids = defined_ids(base_text), defined_ids(head_text)
    if base_ids and not head_text.strip():
        print(f"[UNKNOWN] {SOURCE} is missing or empty at {args.head} but defines ids at {args.base}", file=sys.stderr)
        return 2
    if head_text and not head_ids:
        print(f"[UNKNOWN] {SOURCE} at {args.head} defines no ids: the parser is looking at the wrong thing", file=sys.stderr)
        return 2
    failures = check(base_ids, head_ids, args.pr)
    for line in failures:
        print(line, file=sys.stderr)
    if failures:
        return 1
    new = len(set(head_ids) - set(base_ids))
    print(f"[OK] {new} new requirement id(s), all numbered with PR #{args.pr}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
