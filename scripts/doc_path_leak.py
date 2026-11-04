"""Reject a real host home path on an ADDED line of a doc or plan file.

A plan or doc that names the author's home directory (a /home or /Users path with a
login in it, usually the lane worktree) leaks the login and the host layout into the
tracked tree, and Codex review holds the PR for it. Wed 4 Nov 2026: 6 of 9 merge-head
holds were this one finding (#8585, #8586, #8590, #8591, #8599, #8602, #8603).
`scripts/oss_tip_rules.py` skips `specs/`, `app_docs/` and `.planning/` as record
paths, so nothing else catches it before review.

Scope: added lines only, in files under specs/, app_docs/, docs/ or .planning/. A line
passes when it also carries `<worktree>`, when it sits inside a fenced code block
tagged `console` or `log` (a verbatim transcript), or when the login is one of the
repo's placeholder logins (`PLACEHOLDER_USERS`, the same list the OSS tip audit uses).

    python -m scripts.doc_path_leak --base <sha> --head <sha>
    python -m scripts.doc_path_leak --diff-file <unified.diff> --head-dir <tree>
    python -m scripts.doc_path_leak --control <fixture dir>

Exit 0 clean, 1 findings (each printed as file:line with the fix), 2 not measured.
`--control` is the positive control: it renders the fixture with a login minted per
run and exits 0 only when exactly its planted leaks are found, else 2. `--report-md`
appends the findings as a markdown section for the CI summary.
Run in CI by scripts/security/scan_secrets.sh (PR mode), row `doc-path-leak`.
"""

from __future__ import annotations

import argparse
import re
import secrets
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from scripts.oss_tip_rules import PLACEHOLDER_USERS

DOC_PREFIXES = ("specs/", "app_docs/", "docs/", ".planning/")
# Not preceded by a path or host character, so `apps/home/x/` and a URL route such as
# `example.com/home/x/` are not home directories.
HOME_PATH = re.compile(r"(?<![\w.-])/(?:home|Users)/([A-Za-z0-9_.-]+)/")
WORKTREE_PLACEHOLDER = "<worktree>"
TRANSCRIPT_FENCE_TAGS = frozenset({"console", "log"})
FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})\s*([^\s`]*)")
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
FIX = "use a repo-relative path, or <worktree>/... for this checkout"
# The control fixture plants exactly this many leaks (tests/fixtures/security/doc-path-leak-control).
CONTROL_PLANTED = 3
# Built by join so no tracked line spells a home root followed by a login.
CONTROL_ROOTS = {"@HOME@": "/" + "home", "@MACHOME@": "/" + "Users"}


@dataclass(frozen=True)
class Leak:
    path: str
    line: int
    login: str
    text: str


# ----- diff parsing ---------------------------------------------------------------------------------
def added_doc_lines(diff_text: str) -> dict[str, dict[int, str]]:
    """Map each doc file in a unified diff to {new line number: added text}."""
    added: dict[str, dict[int, str]] = {}
    path: str | None = None
    new_line = 0
    for raw in diff_text.splitlines():
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            path = target[2:] if target.startswith("b/") else None
            if path is not None and not path.startswith(DOC_PREFIXES):
                path = None
            continue
        if raw.startswith("--- "):
            continue
        hunk = HUNK.match(raw)
        if hunk:
            new_line = int(hunk.group(1))
            continue
        if path is None or not raw:
            continue
        if raw.startswith("+"):
            added.setdefault(path, {})[new_line] = raw[1:]
            new_line += 1
        elif raw.startswith(" "):
            new_line += 1
    return added


def transcript_fence_lines(content: str) -> set[int]:
    """1-based line numbers inside a fenced block tagged console or log (fences excluded)."""
    inside: set[int] = set()
    open_fence: str | None = None
    open_tag = ""
    for number, line in enumerate(content.splitlines(), start=1):
        fence = FENCE.match(line)
        if open_fence is None:
            if fence:
                open_fence, open_tag = fence.group(1), fence.group(2).lower()
            continue
        if (
            fence
            and fence.group(1)[0] == open_fence[0]
            and len(fence.group(1)) >= len(open_fence)
            and not fence.group(2)
        ):
            open_fence = None
            continue
        if open_tag in TRANSCRIPT_FENCE_TAGS:
            inside.add(number)
    return inside


# ----- policy -----------------------------------------------------------------------------------------
def find_leaks(diff_text: str, read_head: Callable[[str], str]) -> list[Leak]:
    leaks: list[Leak] = []
    for path, lines in sorted(added_doc_lines(diff_text).items()):
        exempt = transcript_fence_lines(read_head(path))
        for number, text in sorted(lines.items()):
            if WORKTREE_PLACEHOLDER in text or number in exempt:
                continue
            for match in HOME_PATH.finditer(text):
                if match.group(1) not in PLACEHOLDER_USERS:
                    leaks.append(Leak(path, number, match.group(1), text.strip()))
                    break
    return leaks


# ----- shell --------------------------------------------------------------------------------------------
def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True, errors="replace"
    ).stdout


def _report(leaks: list[Leak], report_md: Path | None) -> int:
    for leak in leaks:
        print(f"{leak.path}:{leak.line}: real home path ({leak.login}): {FIX}")
        print(f"    {leak.text[:200]}")
    print(f"doc-path-leak: {len(leaks)} finding(s)")
    if leaks and report_md is not None:
        rows = "".join(f"- `{leak.path}:{leak.line}: real home path ({leak.login})`\n" for leak in leaks)
        with report_md.open("a", encoding="utf-8") as out:
            out.write(f"\n### doc-path-leak ({len(leaks)}): {FIX}\n\n{rows}")
    return 1 if leaks else 0


def _render_control(control: Path, work: Path) -> str:
    """Copy the control fixture into work with a login minted per run; return the login."""
    login = "zq" + secrets.token_hex(4)
    for src in [control / "control.diff", *(control / "head").rglob("*.md")]:
        dest = work / src.relative_to(control)
        dest.parent.mkdir(parents=True, exist_ok=True)
        text = src.read_text(encoding="utf-8")
        for token, root in CONTROL_ROOTS.items():
            text = text.replace(token, f"{root}/{login}")
        dest.write_text(text)
    return login


def run_control(control: Path) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        _render_control(control, work)
        diff_text = (work / "control.diff").read_text(encoding="utf-8")
        leaks = find_leaks(diff_text, lambda path: (work / "head" / path).read_text(encoding="utf-8"))
    print(f"doc-path-leak control: fired({len(leaks)}) of {CONTROL_PLANTED} planted")
    return 0 if len(leaks) == CONTROL_PLANTED else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base")
    parser.add_argument("--head")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--diff-file", type=Path)
    parser.add_argument("--head-dir", type=Path, help="tree holding the post-diff files (with --diff-file)")
    parser.add_argument("--control", type=Path, help="run the positive control over this fixture dir")
    parser.add_argument("--report-md", type=Path, help="append findings to this markdown report")
    args = parser.parse_args(argv)
    try:
        if args.control is not None:
            return run_control(args.control)
        if args.diff_file is not None and args.head_dir is not None:
            diff_text = args.diff_file.read_text(encoding="utf-8")
            head_dir: Path = args.head_dir

            def read_head(path: str) -> str:
                return (head_dir / path).read_text(encoding="utf-8", errors="replace")

        elif args.base and args.head:
            diff_text = _git(
                args.root,
                "-c",
                "core.quotePath=false",
                "diff",
                "--no-color",
                "--no-ext-diff",
                "-U0",
                args.base,
                args.head,
                "--",
                *DOC_PREFIXES,
            )

            def read_head(path: str) -> str:
                return _git(args.root, "show", f"{args.head}:{path}")

        else:
            parser.error("give --base and --head, or --diff-file and --head-dir")
        return _report(find_leaks(diff_text, read_head), args.report_md)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"UNKNOWN: doc-path-leak could not read the diff: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
