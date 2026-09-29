"""Prove a formatting commit is format-only, and keep .git-blame-ignore-revs honest.

Issue #4456 lands a whole-tree formatter pass in parts, and blame is told to skip
each part's commit. That is only safe if the commit really changed nothing but
layout, and only works if the SHA blame is told about is the one main contains.

    python -m scripts.format_proof prove --base <rev> [--head <rev>]
    python -m scripts.format_proof ignore-revs [--file .git-blame-ignore-revs]

Requirements (mini-PRD):
  prove  ✔︎
    Every .py file the range modifies decodes by its own cookie (so a cookie moved in
    or out of reach is judged by what the bytes then MEAN), parses on both sides and
    has an equal AST once docstrings get the PEP 257 trim (Black's safety check strips
    every line, which is looser: it would pass a change to a doctest's relative
    indentation). Every comment (prose, `# type:`, noqa, nosec, pragma, fmt: ...)
    keeps its text, its order, how many statements begin before it and the innermost
    statement around it. Only its line, its trailing space and the one space ruff
    adds after `#` may change. A shebang stays on byte 0, or stays off it.
      [if] a changed file's value, name or structure differs [then ⛔️] exit 1 naming it
      [if] a docstring's relative indentation changes [then ⛔️] exit 1
      [if] a comment is added, removed, reworded, reordered, or moved past or out of a statement [then ⛔️] exit 1
      [if] a shebang moves off byte 0 or onto it [then ⛔️] exit 1
      [if] the range adds, deletes or renames a file, or changes a file's mode [then ⛔️] exit 1
      [if] the range also modifies a file that is not .py [then ⛔️] exit 2 UNKNOWN, since no AST can prove it
      [if] a file does not decode or parse, or no .py file changed [then ⛔️] exit 2 UNKNOWN, never a pass
  ignore-revs  ✔︎
    Every listed SHA is a commit, an ancestor of HEAD, and has a style(format): subject.
      [if] a line is not a full 40-hex SHA or a comment [then ⛔️] exit 1
      [if] a SHA is not an ancestor of HEAD (squash or rebase merge) [then ⛔️] exit 1
      [if] the clone is shallow and cannot see a SHA [then ⛔️] exit 2 UNKNOWN

What could satisfy this without satisfying its intent: a normalizer that strips
whitespace from EVERY string would pass a real edit to a string value, so only
docstring positions are normalized (tests/quality/test_format_proof.py pins that).
A proof over zero files would read as success, so it exits 2 instead. A comment's
LINE is not compared, because every re-wrap above it moves it; its place among the
statements is, because a formatter never reorders tokens. So a noqa or type-ignore
moved to another statement fails even when a count-based ratchet would net to zero,
and so does a whole-module type-ignore moved below the module's first line of code.
Whether a pragma still covers its finding after a re-wrap inside one statement is not
an AST property: the part's ruff and mypy ratchet runs decide that. Reading both sides
as utf-8 would pass a cookie moved out of reach, so each side decodes as Python would.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import inspect
import io
import re
import subprocess
import sys
import tokenize
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


def _changed_entries(repo: Path, base: str, head: str) -> list[tuple[str, str, str, str]]:
    """(status, path, old mode, new mode) per changed file; a chmod-only change is status M."""
    out = _git(repo, "diff", "--raw", "--no-renames", "--no-abbrev", base, head).stdout
    entries: list[tuple[str, str, str, str]] = []
    for meta, path in (ln.split("\t", 1) for ln in out.splitlines() if ln):
        old_mode, new_mode, _, _, status = meta.lstrip(":").split()
        entries.append((status, path, old_mode, new_mode))
    return entries


def _blob(repo: Path, rev: str, path: str) -> bytes:
    """Raw bytes: only Python's own decode knows which of a file's lines its cookie may sit on."""
    return subprocess.run(["git", "-C", str(repo), "show", f"{rev}:{path}"], capture_output=True, check=True).stdout


# ----- AST comparison -------------------------------------------------------


def _normalize_docstring(text: str) -> str:
    """PEP 257 trim: the first line's indent, the common indent of the rest, trailing space and outer
    blank lines are layout. RELATIVE indentation is kept, because doctests and code blocks read it."""
    return inspect.cleandoc("\n".join(line.rstrip() for line in text.splitlines()))


def _normalized_dump(source: str) -> tuple[str, int]:
    """AST dump with docstring whitespace normalized, and how many docstrings were touched."""
    tree = ast.parse(source)
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
    return ast.dump(ast.parse(source))


def _statement_spans(tree: ast.Module) -> list[tuple[int, int, int, int]]:
    """(first line, last line, depth, column) of every statement, decorators included, in an order two equal
    ASTs share."""
    spans: list[tuple[int, int, int, int]] = []
    stack: list[tuple[ast.AST, int]] = [(tree, 0)]
    while stack:
        node, depth = stack.pop()
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt):
                first = min([child.lineno, *(dec.lineno for dec in getattr(child, "decorator_list", []))])
                spans.append((first, child.end_lineno or child.lineno, depth, child.col_offset))
            stack.append((child, depth + 1))
    return spans


def _anchor(line: int, spans: list[tuple[int, int, int, int]]) -> tuple[int, int]:
    """Where a comment on `line` sits: how many statements begin at or before it, and the innermost one spanning
    it (the last to begin, when a semicolon puts two on its line), else -1. A formatter never reorders tokens, so
    a re-wrap keeps both, while a comment moved past a statement or out of one changes one of them."""
    started = sum(1 for first, _, _, _ in spans if first <= line)
    holding = [i for i, (first, last, _, _) in enumerate(spans) if first <= line <= last]
    innermost = max(holding, key=lambda i: (spans[i][2], spans[i][0], spans[i][3])) if holding else -1
    return started, innermost


def _normalize_comment(text: str) -> str:
    """ruff format's comment rule, and nothing looser: trailing space goes, a leading no-break space becomes a
    space, and `#x` gains one space unless x is ! : # or ' (shebangs, Sphinx, banners). Inner spacing is kept,
    because `# no sec` and `# nosec` differ to the tools that read them."""
    body = text.rstrip()[1:]
    if body.startswith("\u00a0"):
        body = " " + body[1:]
    if body and not body.startswith((" ", "!", ":", "#", "'")):
        body = " " + body
    return "#" + body


def _comments(source: str) -> list[tuple[tuple[int, int], str]]:
    """Every comment in order, as (anchor, normalized text): its line is layout, its words and place are not."""
    spans = _statement_spans(ast.parse(source))
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    return [
        (_anchor(tok.start[0], spans), _normalize_comment(tok.string)) for tok in tokens if tok.type == tokenize.COMMENT
    ]


def _runs_a_shebang(data: bytes) -> bool:
    """The kernel reads a shebang only at byte 0, so its POSITION is semantics. Its words are a comment's."""
    return data.startswith(b"#!")


# ----- commands -------------------------------------------------------------


def prove(repo: Path, base: str, head: str) -> Result:
    entries = _changed_entries(repo, base, head)
    not_modified = [f"{status} {path}" for status, path, _, _ in entries if status != "M"]
    if not_modified:
        return Result(1, lines=[f"[format-proof] FAIL not format-only, files added/deleted: {x}" for x in not_modified])
    mode_changed = [f"{path} {old} -> {new}" for _, path, old, new in entries if old != new]
    if mode_changed:
        return Result(1, lines=[f"[format-proof] FAIL not format-only, file mode changed: {x}" for x in mode_changed])
    py_paths = [path for _, path, _, _ in entries if path.endswith(".py")]
    not_python = [path for _, path, _, _ in entries if not path.endswith(".py")]
    if not py_paths:
        return Result(2, lines=["[format-proof] UNKNOWN no .py file changed in the range; nothing was proven"])
    result = Result(0, files_checked=len(py_paths))
    for path in py_paths:
        data_before, data_after = _blob(repo, base, path), _blob(repo, head, path)
        if _runs_a_shebang(data_before) != _runs_a_shebang(data_after):
            result.exit_code = 1
            result.lines.append(f"[format-proof] FAIL {path}: the shebang moved on or off byte 0, where it runs")
            continue
        try:
            before, after = importlib.util.decode_source(data_before), importlib.util.decode_source(data_after)
            raw_equal = _raw_dump(before) == _raw_dump(after)
            (dump_before, _), (dump_after, _) = _normalized_dump(before), _normalized_dump(after)
            comments_equal = _comments(before) == _comments(after)
        except (SyntaxError, UnicodeDecodeError) as exc:
            return Result(2, lines=[f"[format-proof] UNKNOWN {path} does not decode or parse: {exc}"])
        if dump_before != dump_after:
            result.exit_code = 1
            result.lines.append(f"[format-proof] FAIL {path}: AST differs, this is not a format-only change")
        elif not comments_equal:
            result.exit_code = 1
            result.lines.append(f"[format-proof] FAIL {path}: a comment was added, removed, reworded or moved")
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
