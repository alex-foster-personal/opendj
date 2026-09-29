"""Prove a formatting commit is format-only, and keep .git-blame-ignore-revs honest.

Issue #4456 lands a whole-tree formatter pass in parts, and blame is told to skip
each part's commit. That is only safe if the commit really changed nothing but
layout, and only works if the SHA blame is told about is the one main contains.

    python -m scripts.format_proof prove --base <rev> [--head <rev>]
    python -m scripts.format_proof ignore-revs [--file .git-blame-ignore-revs]

Requirements (mini-PRD):
  prove  ✔︎
    Every .py file the range modifies parses on both sides and has an equal AST
    once docstring whitespace is normalized (Black's safety-check equivalence).
    The AST includes `# type:` comments and each `# type: ignore` tag.
      [if] a changed file's value, name or structure differs [then ⛔️] exit 1 naming it
      [if] a `# type:` comment or a `# type: ignore` tag is added, removed or changed [then ⛔️] exit 1
      [if] the range adds, deletes or renames a file [then ⛔️] exit 1, since that is not layout
      [if] the range also modifies a file that is not .py [then ⛔️] exit 2 UNKNOWN, since no AST can prove it
      [if] a file does not parse, or no .py file changed [then ⛔️] exit 2 UNKNOWN, never a pass
  ignore-revs  ✔︎
    Every listed SHA is a commit, an ancestor of HEAD, and has a style(format): subject.
      [if] a line is not a full 40-hex SHA or a comment [then ⛔️] exit 1
      [if] a SHA is not an ancestor of HEAD (squash or rebase merge) [then ⛔️] exit 1
      [if] the clone is shallow and cannot see a SHA [then ⛔️] exit 2 UNKNOWN

What could satisfy this without satisfying its intent: a normalizer that strips
whitespace from EVERY string would pass a real edit to a string value, so only
docstring positions are normalized (tests/quality/test_format_proof.py pins that).
A proof over zero files would read as success, so it exits 2 instead. A type-ignore's
LINE is not compared, because every re-wrap above it moves it; whether it still covers
its error is not an AST property, so the part's mypy ratchet run decides that.
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


class CFG:
    IGNORE_REVS_FILE: str = ".git-blame-ignore-revs"
    FORMAT_SUBJECT_PREFIX: str = "style(format):"
    SHA_RE: re.Pattern[str] = re.compile(r"^[0-9a-f]{40}$")
    # A fixed-length tuple type, so mypy narrows isinstance() to nodes that have .body.
    DOCSTRING_OWNERS: tuple[type[ast.Module], type[ast.ClassDef], type[ast.FunctionDef], type[ast.AsyncFunctionDef]] = (
        ast.Module,
        ast.ClassDef,
        ast.FunctionDef,
        ast.AsyncFunctionDef,
    )


@dataclass
class Result:
    exit_code: int
    files_checked: int = 0
    docstring_normalized: int = 0
    lines: list[str] = field(default_factory=list)


# ----- git helpers ----------------------------------------------------------


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=check)


def _changed_entries(repo: Path, base: str, head: str) -> list[tuple[str, str]]:
    out = _git(repo, "diff", "--name-status", "--no-renames", base, head).stdout
    return [(status, path) for status, path in (ln.split("\t", 1) for ln in out.splitlines() if ln)]


def _blob(repo: Path, rev: str, path: str) -> str:
    return _git(repo, "show", f"{rev}:{path}").stdout


# ----- AST comparison -------------------------------------------------------


def _normalize_docstring(text: str) -> str:
    return "\n".join(line.strip() for line in text.splitlines()).strip()


def _parse(source: str) -> ast.Module:
    """Parse with type comments, so `# type:` annotations and type-ignore tags are compared."""
    tree = ast.parse(source, type_comments=True)
    for ignore in tree.type_ignores:
        ignore.lineno = 0  # layout, not meaning: see the module docstring
    return tree


def _normalized_dump(source: str) -> tuple[str, int]:
    """AST dump with docstring whitespace normalized, and how many docstrings were touched."""
    tree = _parse(source)
    touched = 0
    for node in ast.walk(tree):
        if not isinstance(node, CFG.DOCSTRING_OWNERS) or not node.body:
            continue
        first = node.body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            normalized = _normalize_docstring(first.value.value)
            touched += normalized != first.value.value
            first.value.value = normalized
    return ast.dump(tree), touched


def _raw_dump(source: str) -> str:
    return ast.dump(_parse(source))


# ----- commands -------------------------------------------------------------


def prove(repo: Path, base: str, head: str) -> Result:
    entries = _changed_entries(repo, base, head)
    not_modified = [f"{status} {path}" for status, path in entries if status != "M"]
    if not_modified:
        return Result(1, lines=[f"[format-proof] FAIL not format-only, files added/deleted: {x}" for x in not_modified])
    py_paths = [path for _, path in entries if path.endswith(".py")]
    not_python = [path for _, path in entries if not path.endswith(".py")]
    if not py_paths:
        return Result(2, lines=["[format-proof] UNKNOWN no .py file changed in the range; nothing was proven"])
    result = Result(0, files_checked=len(py_paths))
    for path in py_paths:
        before, after = _blob(repo, base, path), _blob(repo, head, path)
        try:
            raw_equal = _raw_dump(before) == _raw_dump(after)
            (dump_before, _), (dump_after, _) = _normalized_dump(before), _normalized_dump(after)
        except SyntaxError as exc:
            return Result(2, lines=[f"[format-proof] UNKNOWN {path} does not parse: {exc.msg} line {exc.lineno}"])
        if dump_before != dump_after:
            result.exit_code = 1
            result.lines.append(f"[format-proof] FAIL {path}: AST differs, this is not a format-only change")
        elif not raw_equal:
            result.docstring_normalized += 1
    if result.exit_code == 0 and not_python:
        result.exit_code = 2
        result.lines.extend(f"[format-proof] UNKNOWN {path}: not Python, so no AST can prove it" for path in not_python)
    if result.exit_code == 0:
        result.lines.append(
            f"[format-proof] OK {result.files_checked} .py files AST-equal "
            f"({result.docstring_normalized} after docstring-whitespace normalization), "
            f"base={_git(repo, 'rev-parse', base).stdout.strip()} head={_git(repo, 'rev-parse', head).stdout.strip()}"
        )
    return result


def check_ignore_revs(repo: Path, path: Path) -> Result:
    shas: list[str] = []
    for number, raw in enumerate(path.read_text().splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not CFG.SHA_RE.match(line):
            return Result(1, lines=[f"[ignore-revs] FAIL line {number}: not a full 40-hex SHA: {line!r}"])
        shas.append(line)
    shallow = _git(repo, "rev-parse", "--is-shallow-repository").stdout.strip() == "true"
    result = Result(0, files_checked=len(shas))
    for sha in shas:
        if _git(repo, "cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode != 0:
            if shallow:
                return Result(2, lines=[f"[ignore-revs] UNKNOWN {sha} not visible in this shallow clone"])
            result.exit_code = 1
            result.lines.append(f"[ignore-revs] FAIL {sha} is not a commit in this repository")
            continue
        if _git(repo, "merge-base", "--is-ancestor", sha, "HEAD", check=False).returncode != 0:
            result.exit_code = 1
            result.lines.append(f"[ignore-revs] FAIL {sha} is not an ancestor of HEAD (squash or rebase merge?)")
            continue
        subject = _git(repo, "log", "-1", "--format=%s", sha).stdout.strip()
        if not subject.startswith(CFG.FORMAT_SUBJECT_PREFIX):
            result.exit_code = 1
            result.lines.append(f"[ignore-revs] FAIL {sha} subject is not {CFG.FORMAT_SUBJECT_PREFIX}: {subject!r}")
    if result.exit_code == 0:
        result.lines.append(f"[ignore-revs] OK {len(shas)} format-only commit(s) listed, all ancestors of HEAD")
    return result


# ----- CLI ------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_prove = sub.add_parser("prove", help="prove base..head changed Python layout only")
    p_prove.add_argument("--base", required=True)
    p_prove.add_argument("--head", default="HEAD")
    p_revs = sub.add_parser("ignore-revs", help="check every SHA in the blame ignore file")
    p_revs.add_argument("--file", type=Path, default=REPO / CFG.IGNORE_REVS_FILE)
    args = parser.parse_args(argv)
    if args.command == "prove":
        result = prove(REPO, args.base, args.head)
    elif args.command == "ignore-revs":
        result = check_ignore_revs(REPO, args.file)
    else:
        raise SystemExit(f"unhandled command {args.command}")
    print("\n".join(result.lines))
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
