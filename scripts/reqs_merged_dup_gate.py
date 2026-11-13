"""IDD ship-mode merge gate: refuse when merged-tree carries duplicate requirement ids.

Delegates to :func:`scripts.build_reqs_json.run_merged_with_check`; never
reimplements merge-tree stitching.

  - [if] HEAD merged with base would define a requirement id more than once
    [then] exit 1, name the ids, print a park comment on stdout, [else stop].
  - [if] git merge-tree or the base ref cannot be measured [then] exit 2 UNKNOWN,
    [else stop].

    python -m scripts.reqs_merged_dup_gate
    python -m scripts.reqs_merged_dup_gate --base origin/main --head HEAD
    python -m scripts.reqs_merged_dup_gate --repo-root /path/to/worktree
"""

from __future__ import annotations

import argparse
import io
import re
import sys
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path

from scripts.build_reqs_json import run_merged_with_check

REPO_ROOT = Path(__file__).resolve().parent.parent
PREFIX = "[reqs-merged-dup-gate]"

_ALL_DUPS_RE = re.compile(
    r"defines \d+ id\(s\) more than once: (.+)\.",
)
_PREEXISTING_RE = re.compile(
    r"\[reqs-merge\] Pre-existing on ([^ (]+)[^:]*: (.+)",
)
_INTRODUCED_RE = re.compile(
    r"\[reqs-merge\] Introduced by this merge: (.+)",
)


@dataclass(frozen=True)
class MergedDupVerdict:
    code: int
    gate_line: str
    park_comment: str
    merge_log: str


def _parse_merge_log(merge_log: str) -> tuple[str, str, str]:
    all_dups = ""
    preexisting = ""
    introduced = ""
    for line in merge_log.splitlines():
        if match := _ALL_DUPS_RE.search(line):
            all_dups = match.group(1).strip()
        if match := _PREEXISTING_RE.search(line):
            preexisting = match.group(2).strip()
        if match := _INTRODUCED_RE.search(line):
            introduced = match.group(1).strip()
    return all_dups, preexisting, introduced


def format_park_comment(base: str, head: str, merge_log: str) -> str:
    """GitHub-ready park comment when the merged requirement ledger has duplicates."""
    all_dups, preexisting, introduced = _parse_merge_log(merge_log)
    lines = [
        "Ship-mode merge parked (`reqs-merged-dup-gate`): merged requirement ledger defines duplicate id(s).",
        "",
        f"Base: `{base}`",
        f"Head: `{head}`",
    ]
    if all_dups:
        lines.extend(["", f"Duplicates: {all_dups}"])
    if introduced:
        lines.extend(["", f"Introduced by this merge: {introduced}"])
    if preexisting:
        lines.extend(
            [
                "",
                f"Pre-existing on {base} (repair on main, not this PR): {preexisting}",
            ]
        )
    lines.extend(
        [
            "",
            "Repair duplicate definitions in `.planning/REQUIREMENTS.md` and "
            "`(private path)/`, then re-run "
            "`python -m scripts.reqs_merged_dup_gate`.",
        ]
    )
    return "\n".join(lines)


def evaluate_merged_dups(base: str, head: str, repo_root: Path) -> MergedDupVerdict:
    """Run the merged-with check and shape stdout/stderr for the merge lane."""
    buf_out = io.StringIO()
    buf_err = io.StringIO()
    with redirect_stdout(buf_out), redirect_stderr(buf_err):
        code = run_merged_with_check(base, head, repo_root)
    merge_log = buf_out.getvalue() + buf_err.getvalue()
    if code == 0:
        return MergedDupVerdict(
            code=0,
            gate_line=f"{PREFIX} OK",
            park_comment="",
            merge_log=merge_log,
        )
    if code == 2:
        detail = merge_log.strip().splitlines()[-1] if merge_log.strip() else "merge check failed"
        return MergedDupVerdict(
            code=2,
            gate_line=f"{PREFIX} UNKNOWN: {detail}",
            park_comment="",
            merge_log=merge_log,
        )
    return MergedDupVerdict(
        code=1,
        gate_line=f"{PREFIX} REFUSED duplicates={_parse_merge_log(merge_log)[0] or 'unknown'}",
        park_comment=format_park_comment(base, head, merge_log),
        merge_log=merge_log,
    )


def main(argv: list[str] | None = None, *, evaluator=evaluate_merged_dups) -> int:
    parser = argparse.ArgumentParser(prog="reqs_merged_dup_gate")
    parser.add_argument(
        "--base",
        default="origin/main",
        help="Merge base ref (default: origin/main)",
    )
    parser.add_argument(
        "--head",
        default="HEAD",
        help="Head ref to merge into base (default: HEAD)",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=REPO_ROOT,
        help="Worktree root containing .planning/ (default: this checkout)",
    )
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()

    if not (repo_root / ".planning" / "REQUIREMENTS.md").is_file():
        print(
            f"{PREFIX} UNKNOWN: no .planning/REQUIREMENTS.md in {repo_root}",
            file=sys.stderr,
        )
        return 2

    verdict = evaluator(args.base, args.head, repo_root)
    if verdict.code == 0:
        print(verdict.gate_line)
        if verdict.merge_log.strip():
            print(verdict.merge_log.strip(), file=sys.stderr)
        return 0
    if verdict.code == 2:
        print(verdict.gate_line, file=sys.stderr)
        if verdict.merge_log.strip():
            print(verdict.merge_log.strip(), file=sys.stderr)
        return 2
    print(verdict.gate_line)
    print(verdict.park_comment)
    if verdict.merge_log.strip():
        print(verdict.merge_log.strip(), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
