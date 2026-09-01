"""
shell_construct_lint.py -- ban three shell constructs that answer a question
nobody asked.

Each construct below returns a PLAUSIBLE VALUE WITH NO ERROR when it is wrong.
That is the whole family: not a crash, not an empty result you would notice, but
a confident answer to a question you did not ask. Fourteen instances landed in
one night of fleet work, and the prose rule against them
(.claude/rules/verification.md) was merged and then violated twice inside an hour
by its own author. Memory is the wrong enforcement layer for this, so it lives
here instead.

MATCH THE CONSTRUCT, NEVER THE SYMPTOM. Construct (c) below produced 0 on one run
and 1 on the next from the same command, so any value-based check ("suspicious
zero") passes half the time. The construct is stable; what it returns is not.

Requirements (status key: -> out-of-scope, ? todo, OK done, RUN done+ran,
REG done+ran+regression-tested):

REG  S-01 Flag `$?` read after a pipeline, where it reports the LAST command's
          status rather than the pipeline's. Exempt a file that set pipefail
          before the read, which is the correct fix.
          [if `cmd | tee log` is followed by `RC=$?` then the run exits 1]
          [if the same pair sits under `set -o pipefail` then the run exits 0]
          [if `$?` follows a plain non-pipeline command then the run exits 0]
REG  S-02 Flag `gh api` invoked with `--arg`/`--argjson`. `gh api` SILENTLY
          IGNORES them (unlike `gh run list`, which errors), so the jq query
          runs unparameterized and returns a confident wrong answer.
          [if `gh api repos/x --jq ... --arg n v` appears then the run exits 1]
          [if the `--arg` sits after a pipe, on a real `jq`, then exits 0]
          [if `gh api` uses `-f`/`--field` then the run exits 0]
REG  S-03 Flag an unbraced `"$VAR:<modifier>"`, where the character after the
          colon is a zsh history modifier and the path is mangled or eaten.
          [if `git cat-file -e "$SHA:apps/x"` appears then the run exits 1]
          [if it is written `"${SHA}:apps/x"` then the run exits 0]
          [if the colon is followed by a digit (`$HOST:8080`) then exits 0]
REG  S-04 Refuse to report a clean scan over an empty file set: a count-based
          check needs a floor, because zero is both a value and an error
          signature (.claude/rules/verification.md).
          [if discovery returns fewer than CFG.MIN_FILES files then the run
           aborts rather than printing PASS]

The zsh modifier alphabet in CFG.ZSH_MODIFIERS is MEASURED, not remembered.
Probe (zsh 5.9, macOS 15, Mon 31 Aug 2026), one isolated process per letter:

    zsh -c 'S=deadbeef; print -r -- "$S:<L>NNN/tail"'

Letters whose output differed from the literal `deadbeef:<L>NNN/tail`:

    a  -> /cwd/deadbeefNNN/tail   (absolute path, letter eaten AND cwd prefixed)
    c  -> deadbeefNNN/tail        (PATH search)
    e  -> NNN/tail                (extension only; everything before it gone)
    h  -> .NNN/tail               (head)
    l  -> deadbeefNNN/tail        (lowercase)
    q  -> deadbeefNNN/tail        (quote)
    r  -> deadbeefNNN/tail        (root, extension removed)
    s  -> zsh: no previous substitution   (hard error)
    t  -> deadbeefNNN/tail        (tail)
    u  -> DEADBEEFNNN/tail        (uppercase)
    A  -> /cwd/deadbeefNNN/tail   (absolute, no symlink resolution)
    P  -> /cwd/deadbeefNNN/tail   (realpath)
    Q  -> deadbeefNNN/tail        (dequote)
    &  -> deadbeefNNN/tail        (repeat last substitution)

Every other ASCII letter printed the literal, which is a measurement of their
inertness and not an assumption about it: the fourteen above prove the probe can
tell the difference. Re-run it rather than trusting this list if zsh changes.

Usage:
    python -m scripts.shell_construct_lint              # scan the repo
    python -m scripts.shell_construct_lint path ...     # scan explicit paths
    python -m scripts.shell_construct_lint --list-files # show what is in scope
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


class CFG:
    """What is scanned and what counts. Change these deliberately."""

    SHELL_SUFFIXES: frozenset[str] = frozenset({".sh", ".zsh", ".bash", ".ksh"})
    JUSTFILE_NAMES: frozenset[str] = frozenset({"justfile", "Justfile", ".justfile"})
    # Extensionless files are only sniffed for a shebang under these roots, so
    # the walk does not open every binary in the tree.
    SHEBANG_ROOTS: tuple[str, ...] = ("scripts", ".agents", ".githooks", "ops")
    # Third-party source, build output, and the lint's own bad fixtures. The
    # fixtures are deliberately excluded from DISCOVERY only: they are passed to
    # the linter explicitly by tests, and tests/scripts/test_shell_construct_lint.py
    # also proves an identical file at a discovered path IS caught, so this
    # exclusion cannot be the reason the repo scans clean.
    SKIP_PREFIXES: tuple[str, ...] = (
        "apps/sync/usb/pioneer/_vendor/",
        "apps/desktop/src-tauri/target/",
        "apps/desktop/src-tauri/payload/",
        "tests/fixtures/shell_lint/",
    )
    SKIP_DIR_NAMES: frozenset[str] = frozenset(
        {".git", "node_modules", "__pycache__", ".venv", ".pytest_cache", "dist", "build"}
    )
    # A clean scan over nothing is not a clean scan. See S-04.
    MIN_FILES: int = 20
    # Measured, not remembered. See the probe in the module docstring.
    ZSH_MODIFIERS: str = "acehlqrstuAPQ&"


RULES: dict[str, str] = {
    "pipeline-status": (
        "`$?` after a pipeline reports the LAST command's status, not the "
        "pipeline's. Use ${PIPESTATUS[0]} on the very next line, or set -o pipefail."
    ),
    "gh-api-arg": (
        "`gh api` SILENTLY IGNORES --arg/--argjson, so the --jq query runs "
        "unparameterized and returns a confident wrong answer. Interpolate the "
        "value into the query, or pipe real output to a real jq."
    ),
    "zsh-modifier-path": (
        "An unbraced \"$VAR:x\" where x is a zsh history modifier mangles or eats "
        "the path, so the command answers a different question with exit 0. Brace "
        "it: \"${VAR}:path\"."
    ),
}


@dataclass(frozen=True)
class Violation:
    path: Path
    lineno: int
    rule: str
    source: str

    def render(self, root: Path) -> str:
        try:
            rel = self.path.relative_to(root).as_posix()
        except ValueError:
            rel = self.path.as_posix()
        return f"{rel}:{self.lineno}: {self.rule}: {self.source.strip()}"


@dataclass(frozen=True)
class LogicalLine:
    """One shell statement, continuations joined and comments removed.

    ``code`` is the statement text. ``line_of`` holds the PHYSICAL line number of
    each character in ``code``, so a match reports the line a human can open
    rather than the line the statement happened to start on. ``literal`` marks
    characters inside single quotes, where no expansion happens.
    """

    lineno: int
    code: str
    line_of: tuple[int, ...]
    literal: tuple[bool, ...]
    heredoc_body: bool = False
    # Independent-shell boundary. A shell script is one block; a justfile is one
    # block PER RECIPE, because just runs each recipe in its own shell and a
    # `set -o pipefail` in one recipe protects no other.
    block: int = 0

    def physical(self, index: int) -> int:
        return self.line_of[index] if index < len(self.line_of) else self.lineno


# ----- scanning ------------------------------------------------------------

_HEREDOC = re.compile(r"<<-?\s*(?P<q>[\"']?)(?P<delim>[A-Za-z_][A-Za-z0-9_]*)(?P=q)")


@dataclass
class _QuoteState:
    """Quote nesting carried ACROSS physical lines, so a `#` inside a multi-line
    string is not mistaken for a comment."""

    squote: bool = False
    dquote: bool = False

    @property
    def open(self) -> bool:
        return self.squote or self.dquote


def _consume_line(
    raw: str, state: _QuoteState
) -> tuple[list[tuple[str, bool]], bool, list[tuple[str, bool]]]:
    """Scan one physical line.

    Returns the surviving characters as (char, is_single_quoted) pairs with the
    comment removed, whether the line ends in a backslash continuation, and any
    heredocs it opened as (delimiter, expands_parameters).
    """
    chars: list[tuple[str, bool]] = []
    heredocs: list[tuple[str, bool]] = []
    continued = False
    i = 0
    while i < len(raw):
        ch = raw[i]
        if state.squote:
            state.squote = ch != "'"
            chars.append((ch, True))
            i += 1
        elif ch == "\\" and i + 1 < len(raw):
            chars += [(ch, False), (raw[i + 1], False)]
            i += 2
        elif ch == "\\":
            continued = not state.dquote
            chars.append((" ", False))
            i += 1
        elif ch == "#" and not state.dquote and (i == 0 or raw[i - 1] in " \t;&|("):
            break
        else:
            if ch == '"':
                state.dquote = not state.dquote
            elif ch == "'" and not state.dquote:
                state.squote = True
            elif ch == "<" and not state.dquote and raw.startswith("<<", i):
                match = _HEREDOC.match(raw, i)
                if match:
                    heredocs.append((match.group("delim"), not match.group("q")))
            chars.append((ch, state.squote))
            i += 1
    return chars, continued, heredocs


def _heredoc_line(raw: str, lineno: int) -> LogicalLine:
    """A heredoc body line, scanned only for the modifier rule."""
    return LogicalLine(
        lineno,
        raw,
        tuple([lineno] * len(raw)),
        tuple([False] * len(raw)),
        heredoc_body=True,
    )


def _scan(text: str, block_of: dict[int, int] | None = None) -> list[LogicalLine]:
    """Split shell source into logical lines, tracking quotes and heredocs.

    Heredoc bodies follow the measured zsh behavior: an UNQUOTED delimiter
    expands parameters (and so applies modifiers), a quoted one does not, so only
    the former is emitted.
    """
    out: list[LogicalLine] = []
    blocks = block_of or {}
    buf: list[tuple[str, int, bool]] = []
    start = 0
    state = _QuoteState()
    heredoc: tuple[str, bool] | None = None

    def flush() -> None:
        nonlocal buf, start
        code = "".join(ch for ch, _, _ in buf)
        if code.strip():
            out.append(
                LogicalLine(
                    start,
                    code,
                    tuple(n for _, n, _ in buf),
                    tuple(lit for _, _, lit in buf),
                    block=blocks.get(start, 0),
                )
            )
        buf, start = [], 0

    for lineno, raw in enumerate(text.split("\n"), start=1):
        if heredoc is not None:
            delim, expands = heredoc
            if raw.strip() == delim:
                heredoc = None
            elif expands:
                out.append(_heredoc_line(raw, lineno))
            continue

        if not buf:
            start = lineno
        chars, continued, heredocs = _consume_line(raw, state)
        buf += [(ch, lineno, lit) for ch, lit in chars]
        if continued or state.open:
            buf.append(("\n", lineno, state.squote))
            continue

        flush()
        heredoc = heredocs[0] if heredocs else None

    flush()
    return out


def _unquoted_spans(line: LogicalLine) -> list[bool]:
    """Per-character 'this is an operator position' mask.

    An operator only operates outside quotes of EITHER kind, so this is stricter
    than ``literal`` (which marks only single quotes, where expansion stops).
    """
    mask: list[bool] = []
    in_squote = False
    in_dquote = False
    skip = False
    for ch in line.code:
        if skip:
            skip = False
            mask.append(False)
            continue
        if ch == "\\":
            skip = True
            mask.append(False)
            continue
        if in_squote:
            mask.append(False)
            if ch == "'":
                in_squote = False
            continue
        if ch == '"':
            in_dquote = not in_dquote
            mask.append(False)
            continue
        if ch == "'" and not in_dquote:
            in_squote = True
            mask.append(False)
            continue
        mask.append(not in_dquote)
    return mask


def _pipe_positions(line: LogicalLine) -> list[int]:
    """Indexes of real pipe operators: not ||, not |&, not >|, not a case pattern."""
    mask = _unquoted_spans(line)
    code = line.code
    out: list[int] = []
    for i, ch in enumerate(code):
        if ch != "|" or not mask[i]:
            continue
        prev = code[i - 1] if i else ""
        nxt = code[i + 1] if i + 1 < len(code) else ""
        if prev in "|>" or nxt in "|&":
            continue
        out.append(i)
    # `case` patterns spell alternatives with `|` and run no commands.
    stripped = code.strip()
    if re.match(r"^\(?\s*[^()]*\)\s*$", stripped) and ";;" not in stripped:
        return []
    return out


# ----- rules ---------------------------------------------------------------

_STATUS = re.compile(r"\$\?")
_PIPEFAIL = re.compile(r"\bpipefail\b")
_GH_API = re.compile(r"\bgh\s+(?:-{1,2}[\w-]+(?:[= ]\S+)?\s+)*api\b")
_JQ_ARG = re.compile(r"(?<![\w-])--arg(?:json)?(?![\w-])")


_CLOSES_BLOCK = re.compile(r"(?:\}|\bfi\b|\bdone\b|\besac\b|;;)\s*$")


def _check_pipeline_status(lines: list[LogicalLine], path: Path) -> list[Violation]:
    out: list[Violation] = []
    pipefail = False
    prev_piped = False
    block = None
    for line in lines:
        if line.heredoc_body:
            continue
        if line.block != block:
            # New independent shell: neither the pipefail setting nor the
            # preceding pipeline carries across.
            block, pipefail, prev_piped = line.block, False, False
        if _PIPEFAIL.search(line.code):
            pipefail = True
        pipes = _pipe_positions(line)
        reads = [
            m for m in _STATUS.finditer(line.code) if not line.literal[m.start()]
        ]
        if not pipefail:
            for match in reads:
                after_own_pipe = any(p < match.start() for p in pipes)
                if after_own_pipe or (prev_piped and not pipes):
                    out.append(
                        Violation(
                            path,
                            line.physical(match.start()),
                            "pipeline-status",
                            line.code,
                        )
                    )
                    break
        # A pipeline that ends with its block closed (`foo() { a | b; }`) is
        # over. The next statement's $? refers to whatever ran after it.
        prev_piped = bool(pipes) and not _CLOSES_BLOCK.search(line.code.strip())
    return out


def _check_gh_api_arg(lines: list[LogicalLine], path: Path) -> list[Violation]:
    out: list[Violation] = []
    for line in lines:
        if line.heredoc_body:
            continue
        mask = _unquoted_spans(line)
        for match in _GH_API.finditer(line.code):
            if not mask[match.start()]:
                continue
            # A `--arg` after the next pipe or command separator belongs to a
            # DIFFERENT command -- very often a real jq, where it is correct.
            end = len(line.code)
            for i in range(match.end(), len(line.code)):
                if mask[i] and (line.code[i] in "|;" or line.code[i : i + 2] == "&&"):
                    end = i
                    break
            arg = _JQ_ARG.search(line.code, match.end(), end)
            if arg:
                out.append(
                    Violation(path, line.physical(arg.start()), "gh-api-arg", line.code)
                )
    return out


def _modifier_pattern() -> re.Pattern[str]:
    mods = re.escape(CFG.ZSH_MODIFIERS)
    return re.compile(rf"\$(?:[A-Za-z_][A-Za-z0-9_]*|\d):[{mods}]")


_MODIFIER = _modifier_pattern()


def _check_zsh_modifier(lines: list[LogicalLine], path: Path) -> list[Violation]:
    out: list[Violation] = []
    for line in lines:
        for match in _MODIFIER.finditer(line.code):
            # Single quotes stop expansion entirely, so the construct is inert.
            if line.literal[match.start()]:
                continue
            out.append(
                Violation(
                    path, line.physical(match.start()), "zsh-modifier-path", line.code
                )
            )
    return out


# ----- discovery -----------------------------------------------------------


def _justfile_shell_only(text: str) -> tuple[str, dict[int, int]]:
    """Blank every non-recipe line of a justfile, preserving line numbers.

    Recipe bodies are shell; the rest is just's own syntax (`:=` assignments,
    `set`, imports) and linting it as shell invents violations that cannot run.

    Also returns a line -> recipe-index map. just gives every recipe its own
    shell, so a `set -o pipefail` inside one recipe must not exempt the next.
    """
    kept: list[str] = []
    block_of: dict[int, int] = {}
    in_recipe = False
    recipe = 0
    for lineno, raw in enumerate(text.split("\n"), start=1):
        if raw.strip() and not raw[0].isspace():
            in_recipe = bool(re.match(r"^[@\w][\w@.-]*(\s+[^:=]*)?:(?!=)", raw))
            if in_recipe:
                recipe += 1
            kept.append("")
            continue
        if not raw.strip():
            kept.append("")
            continue
        kept.append(raw if in_recipe else "")
        block_of[lineno] = recipe
    return "\n".join(kept), block_of


def _has_shell_shebang(path: Path) -> bool:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            first = handle.readline(200)
    except OSError:
        return False
    return bool(re.match(r"^#!.*\b(bash|zsh|ksh|dash|sh)\b", first))


def discover(root: Path) -> list[Path]:
    """Every file in scope: shell sources, justfiles, and shebang'd scripts."""
    out: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if any(part in CFG.SKIP_DIR_NAMES for part in path.parts):
            continue
        rel = path.relative_to(root).as_posix()
        if any(rel.startswith(prefix) for prefix in CFG.SKIP_PREFIXES):
            continue
        named = path.suffix in CFG.SHELL_SUFFIXES or path.name in CFG.JUSTFILE_NAMES
        sniffable = not path.suffix and rel.split("/", 1)[0] in CFG.SHEBANG_ROOTS
        if named or (sniffable and _has_shell_shebang(path)):
            out.append(path)
    return out


def lint_file(path: Path) -> list[Violation]:
    """Every violation in one file. Raises rather than skipping an unreadable one."""
    text = path.read_text(encoding="utf-8", errors="replace")
    block_of: dict[int, int] | None = None
    if path.name in CFG.JUSTFILE_NAMES:
        text, block_of = _justfile_shell_only(text)
    lines = _scan(text, block_of)
    return [
        *_check_pipeline_status(lines, path),
        *_check_gh_api_arg(lines, path),
        *_check_zsh_modifier(lines, path),
    ]


def lint_paths(paths: list[Path]) -> list[Violation]:
    out: list[Violation] = []
    for path in paths:
        out.extend(lint_file(path))
    return sorted(out, key=lambda v: (v.path.as_posix(), v.lineno, v.rule))


# ----- main ----------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("paths", nargs="*", type=Path,
                        help="files to lint; default is a full repo scan")
    parser.add_argument("--root", type=Path, default=REPO,
                        help="repo root to discover from (default: this repo)")
    parser.add_argument("--list-files", action="store_true",
                        help="print the files in scope and exit")
    args = parser.parse_args(argv)

    if args.paths:
        files = [p.resolve() for p in args.paths]
        floor = 0
    else:
        files = discover(args.root.resolve())
        floor = CFG.MIN_FILES

    if args.list_files:
        for path in files:
            print(path)
        return 0

    # S-04: a count-based check needs a floor. A clean scan over nothing looks
    # exactly like a clean scan over everything.
    if len(files) < floor:
        raise SystemExit(
            f"[shell-lint] ABORT: discovered {len(files)} files under "
            f"{args.root}, expected at least {floor}. This is a broken scan, "
            "not a clean tree."
        )

    violations = lint_paths(files)
    root = args.root.resolve()
    for violation in violations:
        print(violation.render(root))
        print(f"    {RULES[violation.rule]}")

    if violations:
        print(f"\n[shell-lint] FAIL: {len(violations)} violation(s) "
              f"in {len(files)} file(s).")
        return 1
    print(f"[shell-lint] PASS: 0 violations in {len(files)} file(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
