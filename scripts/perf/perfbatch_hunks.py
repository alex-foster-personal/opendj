"""Hunk-level classification for the PERFBATCH-02/03 gate (PERFBATCH-07).

Parses a unified diff (``git diff`` / ``gh pr diff``) and decides, per file,
whether every added or removed line is on the narrow non-behavioral
allowlist described in ``scripts/perf/perfbatch_gate.py`` and
``docs/perf/perfbatch-gates.md``. Anything not provably non-behavioral,
including anything unreadable, is a pipeline mutation.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

# ----------------------------------------------------------------------------
# Unified diff parsing


@dataclass
class Hunk:
    old_start: int
    new_start: int
    # (tag, text) with tag in {" ", "+", "-"}; text has no trailing newline.
    lines: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class FileDiff:
    path: str
    old_path: str
    hunks: list[Hunk] = field(default_factory=list)
    binary: bool = False
    # Extended headers. Each one is behavioral on its own: an executable bit
    # changes how a worker or shell script is launched, a rename or copy moves
    # an import path, a created or deleted module changes what the pipeline
    # loads. They are recorded so classify_file() can refuse the file even
    # when every hunk line is allowlisted.
    mode_changed: bool = False
    renamed: bool = False
    created: bool = False
    deleted: bool = False


_DIFF_HEADER = re.compile(r'^diff --git "?a/(?P<a>.*?)"? "?b/(?P<b>.*?)"?$')
_HUNK_HEADER = re.compile(r"^@@ -(?P<old>\d+)(?:,\d+)? \+(?P<new>\d+)(?:,\d+)? @@")


def _note_extended_header(diff: FileDiff, raw: str) -> None:
    if raw.startswith(("old mode ", "new mode ")):
        diff.mode_changed = True
    elif raw.startswith(("rename from ", "rename to ", "copy from ", "copy to ")):
        diff.renamed = True
    elif raw.startswith("new file mode "):
        diff.created = True
    elif raw.startswith("deleted file mode "):
        diff.deleted = True


def parse_unified_diff(text: str) -> dict[str, FileDiff]:
    """Parse ``git diff`` / ``gh pr diff`` output into per-path FileDiff records.

    Keyed by BOTH the old and the new path so a rename out of a pipeline
    prefix is still found under the pipeline name.
    """
    files: dict[str, FileDiff] = {}
    current: FileDiff | None = None
    hunk: Hunk | None = None
    for raw in text.splitlines():
        header = _DIFF_HEADER.match(raw)
        if header:
            current = FileDiff(path=header.group("b"), old_path=header.group("a"))
            hunk = None
            files[current.path] = current
            files[current.old_path] = current
            continue
        if current is None:
            continue
        if raw.startswith(("--- ", "+++ ")):
            continue
        if raw.startswith(("Binary files ", "GIT binary patch")):
            current.binary = True
            hunk = None
            continue
        hunk_header = _HUNK_HEADER.match(raw)
        if hunk_header:
            hunk = Hunk(int(hunk_header.group("old")), int(hunk_header.group("new")))
            current.hunks.append(hunk)
            continue
        if hunk is None:
            _note_extended_header(current, raw)
            continue  # index, similarity and the mode/rename/new/deleted headers
        if raw.startswith("\\"):
            continue  # "\ No newline at end of file"
        tag = raw[:1] if raw else " "
        if tag not in (" ", "+", "-"):
            hunk = None  # trailing junk after a hunk; stop attributing lines to it
            _note_extended_header(current, raw)
            continue
        hunk.lines.append((tag, raw[1:]))
    return files


# ----------------------------------------------------------------------------
# Hunk classification (PERFBATCH-07)

_PY = "python"
_SH = "shell"
_TS = "typescript"
_PROSE = "prose"

_LANGUAGE_BY_SUFFIX: dict[str, str] = {
    ".py": _PY,
    ".sh": _SH,
    ".ts": _TS,
    ".js": _TS,
    ".mjs": _TS,
    ".svelte": _TS,
    ".md": _PROSE,
}

# Bare names that make an exception widening broad enough to swallow
# cancellation, interpreter exit, or an exception group. Compared against the
# LAST dotted component, so ``builtins.Exception`` and
# ``asyncio.exceptions.CancelledError`` are refused the same as the bare name.
# An alias (``from builtins import Exception as E``) is invisible to a hunk
# and remains an accepted residual, documented in perfbatch-gates.md.
_BROAD_EXCEPTIONS = frozenset(
    {
        "Exception",
        "BaseException",
        "ExceptionGroup",
        "BaseExceptionGroup",
        "KeyboardInterrupt",
        "SystemExit",
        "GeneratorExit",
        "CancelledError",
    }
)

_TRAILING_COMMENT = r"\s*(?:#.*)?$"
_SUPPRESS_RE = re.compile(
    r"^(?P<indent>\s*)with\s+(?P<mod>contextlib\.)?suppress\((?P<names>[^()]*)\)"
    r"(?P<as>\s+as\s+\w+)?\s*:" + _TRAILING_COMMENT
)
_EXCEPT_RE = re.compile(
    r"^(?P<indent>\s*)except\s*(?:\((?P<names>[^()]*)\)|(?P<single>[\w.]+))"
    r"(?P<as>\s+as\s+\w+)?\s*:" + _TRAILING_COMMENT
)
_DOTTED_NAME = re.compile(r"^[A-Za-z_][\w.]*$")
_BLOCK_HEADER_RE = re.compile(r"^\s*(?:async\s+def|def|class)\s+\w+.*:" + _TRAILING_COMMENT)
_SIGNATURE_CLOSE_RE = re.compile(r"^\s*\)\s*(?:->\s*.+?)?\s*:" + _TRAILING_COMMENT)
_TRIPLE_QUOTE_RE = re.compile(r'"""|\'\'\'')
_IMPORT_RE = re.compile(
    r"^(?P<indent>\s*)(?P<stmt>import\s+[\w.]+(?:\s+as\s+\w+)?"
    r"|from\s+[\w.]+\s+import\s+\w+(?:\s+as\s+\w+)?(?:\s*,\s*\w+(?:\s+as\s+\w+)?)*)"
    + _TRAILING_COMMENT
)
_IMPORT_GUARD_EXCEPT_RE = re.compile(
    r"^(?P<indent>\s*)except\s*(?:ImportError|ModuleNotFoundError"
    r"|\(\s*ImportError\s*,\s*ModuleNotFoundError\s*\)"
    r"|\(\s*ModuleNotFoundError\s*,\s*ImportError\s*\))\s*:" + _TRAILING_COMMENT
)
_TOML_ASSIGNMENT_RE = re.compile(r"^[\w.-]+\s*=")


def _language(path: str) -> str | None:
    suffix = Path(path.replace("\\", "/")).suffix.lower()
    return _LANGUAGE_BY_SUFFIX.get(suffix)


def _is_hash_comment(text: str, *, python: bool) -> bool:
    stripped = text.strip()
    if not stripped.startswith("#") or stripped.startswith("#!"):
        return False
    if not python:
        return True
    body = stripped[1:].strip()
    # PEP 723 inline metadata rides on comment lines and pins the model and
    # torch versions of the standalone workers: never a plain comment.
    if body.startswith("///") or body[:1] in ('"', "'", "[", "]"):
        return False
    return not _TOML_ASSIGNMENT_RE.match(body)


def _is_slash_comment(text: str) -> bool:
    return text.strip().startswith("//")


def _split_names(raw: str) -> list[str] | None:
    names = [part.strip() for part in raw.split(",")]
    names = [name for name in names if name]
    if not names or any(not _DOTTED_NAME.match(name) for name in names):
        return None
    return names


def _is_broad(name: str) -> bool:
    return name.rsplit(".", 1)[-1] in _BROAD_EXCEPTIONS


def _exception_clause(text: str) -> tuple[str, str, str, frozenset[str]] | None:
    """(family, indent+modifier, as-target, names) for an except/suppress line."""
    match = _SUPPRESS_RE.match(text)
    if match:
        names = _split_names(match.group("names"))
        if names is None:
            return None
        head = match.group("indent") + (match.group("mod") or "")
        return "suppress", head, match.group("as") or "", frozenset(names)
    match = _EXCEPT_RE.match(text)
    if match:
        if match.group("single"):
            names = [match.group("single")]
        else:
            names = _split_names(match.group("names"))
            if names is None:
                return None
        return "except", match.group("indent"), match.group("as") or "", frozenset(names)
    return None


def is_exception_widening(removed: str, added: str) -> bool:
    """True when ``added`` is ``removed`` with extra, non-broad exception names."""
    before = _exception_clause(removed)
    after = _exception_clause(added)
    if before is None or after is None:
        return False
    if before[:3] != after[:3]:
        return False
    old_names, new_names = before[3], after[3]
    if not old_names < new_names:
        return False
    return not any(_is_broad(name) for name in new_names - old_names)


def _bound_names(stmt: str) -> list[str]:
    """Names an import statement binds in the enclosing scope."""
    if stmt.startswith("from "):
        _, _, targets = stmt.partition(" import ")
        names = []
        for target in targets.split(","):
            parts = target.split()
            names.append(parts[-1] if len(parts) == 3 else parts[0])
        return names
    parts = stmt.split()
    if len(parts) == 4:
        return [parts[3]]
    return [parts[1].split(".")[0]]


def _guard_prelude_ok(step: int, text: str, indent: str, stmt: str) -> bool:
    """Step 0 ``try:``, step 1 the same import one level deeper, step 2 ``except ImportError:``."""
    code = text.split("#", 1)[0].rstrip()
    if step == 0:
        return code == f"{indent}try:"
    if step == 1:
        return code == f"{indent}    {stmt}"
    guard = _IMPORT_GUARD_EXCEPT_RE.match(text)
    return guard is not None and guard.group("indent") == indent


def match_import_guard(removed: str, added: list[str]) -> int:
    """Number of ``added`` lines consumed when they wrap ``removed`` in an import guard.

    Shape (comments and blank lines may sit between the four parts)::

        try:
            <removed import, +4 indent>
        except ImportError:
            <name> = None  (one per bound name, any order) | pass

    Returns 0 when the lines do not form exactly that shape.
    """
    match = _IMPORT_RE.match(removed)
    if not match:
        return 0
    indent, stmt = match.group("indent"), match.group("stmt")
    inner = indent + "    "
    step = 0
    body: list[str] = []
    consumed = 0
    for text in added:
        if not text.strip() or _is_hash_comment(text, python=True):
            consumed += 1
            continue
        code = text.split("#", 1)[0].rstrip()
        if step < 3:
            if not _guard_prelude_ok(step, text, indent, stmt):
                return 0
            step += 1
        elif code.startswith(inner) and not code[len(inner) :].startswith(" "):
            body.append(code[len(inner) :])
        else:
            break  # dedent: the guard's except body has ended
        consumed += 1
    wanted = sorted(f"{name} = None" for name in _bound_names(stmt))
    if step < 3 or sorted(body) not in (["pass"], wanted):
        return 0
    return consumed


def _side_lines(hunk: Hunk, tag: str) -> list[tuple[int, str]]:
    """(hunk index, text) for one side of a hunk: context plus that side's tag."""
    return [(i, text) for i, (t, text) in enumerate(hunk.lines) if t in (" ", tag)]


@dataclass
class _StringWalk:
    """Per-side triple-quoted string tracker: docstring lines vs other string content."""

    docstring: set[int] = field(default_factory=set)
    other: set[int] = field(default_factory=set)
    kind: str | None = None  # "doc" | "other" while inside a multi-line string
    delimiter: str = ""
    block: list[int] = field(default_factory=list)
    prev_code: str | None = None

    def open(self, kind: str, delimiter: str, index: int) -> None:
        self.kind, self.delimiter, self.block = kind, delimiter, [index]

    def close(self, index: int, tail: str) -> None:
        self.block.append(index)
        bare = not tail.strip() or tail.strip().startswith("#")
        # A docstring whose closer is followed by code (``""".format(x)``) is
        # an expression, not a bare statement: its lines are string content.
        target = self.docstring if self.kind == "doc" and bare else self.other
        target.update(self.block)
        self.kind, self.block = None, []

    def abandon(self) -> None:
        """Closer beyond the hunk window: nothing inside can be proven benign."""
        self.other.update(self.block)
        self.kind, self.block = None, []


def _string_regions(hunk: Hunk, tag: str, start_line: int) -> _StringWalk:
    """Classify each line on side ``tag`` that lies inside a triple-quoted string.

    A string is a DOCSTRING only when it opens at the start of a line whose
    nearest preceding code line on that side is a ``def``/``class`` header (or
    a multi-line signature close), or when nothing but comments and blank
    lines precede it from line 1 of the file, and its closer is visible with
    nothing but a comment after it. Every other triple-quoted region (a
    prompt, SQL, a shell template, an unclosed block) is OTHER string content:
    never benign, never eligible for widening or import-guard pairing.
    """
    walk = _StringWalk()
    at_file_top = start_line == 1
    for index, text in _side_lines(hunk, tag):
        if walk.kind is not None:
            position = text.find(walk.delimiter)
            if position == -1:
                walk.block.append(index)
            else:
                walk.close(index, text[position + 3 :])
                walk.prev_code = text
            continue
        stripped = text.strip()
        if not stripped or stripped.startswith("#"):
            continue
        opener = _TRIPLE_QUOTE_RE.search(text)
        if opener is None:
            walk.prev_code = text
            continue
        delimiter = opener.group(0)
        at_line_start = text[: opener.start()].strip() in ("", "r", "R")
        follows_header = walk.prev_code is not None and bool(
            _BLOCK_HEADER_RE.match(walk.prev_code) or _SIGNATURE_CLOSE_RE.match(walk.prev_code)
        )
        is_doc = at_line_start and (follows_header or (walk.prev_code is None and at_file_top))
        rest = text[opener.end() :]
        closer = rest.find(delimiter)
        if closer == -1:
            walk.open("doc" if is_doc else "other", delimiter, index)
        else:
            after = rest[closer + 3 :]
            if is_doc and (not after.strip() or after.strip().startswith("#")):
                walk.docstring.add(index)
            elif _TRIPLE_QUOTE_RE.search(after) is not None:
                walk.open("other", delimiter, index)  # a second string opens on this line
        walk.prev_code = text
    if walk.kind is not None:
        walk.abandon()
    return walk


@dataclass(frozen=True)
class FileVerdict:
    path: str
    benign: bool
    reasons: tuple[tuple[str, int], ...]
    offending: str | None

    def describe(self) -> str:
        if self.benign:
            parts = ", ".join(f"{kind} x{count}" for kind, count in self.reasons)
            return f"{self.path} ({parts})"
        return f"{self.path} ({self.offending})"


def _mutation(path: str, offending: str) -> FileVerdict:
    return FileVerdict(path, False, (), offending)


def _describe_line(hunk: Hunk, index: int) -> str:
    tag, text = hunk.lines[index]
    old, new = hunk.old_start, hunk.new_start
    for t, _ in hunk.lines[:index]:
        if t in (" ", "-"):
            old += 1
        if t in (" ", "+"):
            new += 1
    line_no = new if tag == "+" else old
    # ascii(): a cp1252 console must never choke on the line it is refusing.
    return f"{tag}{line_no}: {text.strip()!a}"


def _metadata_refusal(diff: FileDiff, lang: str | None) -> str | None:
    if diff.binary:
        return "binary diff has no readable hunks"
    if diff.mode_changed:
        return "file mode changed"
    if diff.renamed:
        return f"renamed from {diff.old_path}"
    if diff.created and lang != _PROSE:
        return "new file"
    if diff.deleted and lang != _PROSE:
        return "deleted file"
    if not diff.hunks:
        return "no readable hunks (rename-only, mode-only, or no diff text)"
    if lang is None:
        suffix = Path(diff.path).suffix or "<none>"
        return f"no allowlist for {suffix} files"
    return None


def classify_file(diff: FileDiff) -> FileVerdict:
    """Benign only when every changed line is on the allowlist; otherwise a mutation."""
    lang = _language(diff.path)
    refusal = _metadata_refusal(diff, lang)
    if refusal is not None or lang is None:
        return _mutation(diff.path, refusal or "no allowlist for this file")
    reasons: Counter[str] = Counter()
    for hunk in diff.hunks:
        changed = [i for i, (tag, _) in enumerate(hunk.lines) if tag != " "]
        if not changed:
            continue
        if lang == _PROSE:
            reasons["prose"] += len(changed)
            continue
        consumed: set[int] = set()
        docstring: dict[str, set[int]] = {"-": set(), "+": set()}
        string_content: set[int] = set()
        if lang == _PY:
            old_walk = _string_regions(hunk, "-", hunk.old_start)
            new_walk = _string_regions(hunk, "+", hunk.new_start)
            docstring = {"-": old_walk.docstring, "+": new_walk.docstring}
            string_content = old_walk.other | new_walk.other
            for block_removed, block_added in _replacement_blocks(hunk):
                _pair_python_blocks(
                    hunk, block_removed, block_added, consumed, reasons, string_content
                )
        for index in changed:
            if index in consumed:
                continue
            tag, text = hunk.lines[index]
            if index in string_content:
                return _mutation(
                    diff.path, _describe_line(hunk, index) + " inside a string literal"
                )
            kind = _benign_line_kind(lang, text, index in docstring[tag])
            if kind is None:
                return _mutation(diff.path, _describe_line(hunk, index))
            reasons[kind] += 1
    return FileVerdict(diff.path, True, tuple(sorted(reasons.items())), None)


def _replacement_blocks(hunk: Hunk) -> list[tuple[list[int], list[int]]]:
    """Maximal runs of ``-`` lines immediately followed by ``+`` lines."""
    blocks: list[tuple[list[int], list[int]]] = []
    removed: list[int] = []
    added: list[int] = []
    for index, (tag, _) in enumerate([*hunk.lines, (" ", "")]):
        if tag == "-" and not added:
            removed.append(index)
        elif tag == "+":
            added.append(index)
        else:
            if removed or added:
                blocks.append((removed, added))
            removed, added = [], []
            if tag == "-":
                removed.append(index)
    return blocks


def _pair_python_blocks(
    hunk: Hunk,
    removed: list[int],
    added: list[int],
    consumed: set[int],
    reasons: Counter[str],
    string_content: set[int],
) -> None:
    """Consume exception widenings and import guards inside one replacement block.

    Lines inside a non-docstring string literal are never paired: ``except``
    text inside a prompt is prose that happens to look like code.
    """
    removed = [i for i in removed if i not in string_content]
    added = [i for i in added if i not in string_content]
    widen_removed = [i for i in removed if _exception_clause(hunk.lines[i][1])]
    widen_added = [i for i in added if _exception_clause(hunk.lines[i][1])]
    for r_index, a_index in zip(widen_removed, widen_added, strict=False):
        if is_exception_widening(hunk.lines[r_index][1], hunk.lines[a_index][1]):
            consumed.update((r_index, a_index))
            reasons["exception widening"] += 1
    for r_index in removed:
        if r_index in consumed or not _IMPORT_RE.match(hunk.lines[r_index][1]):
            continue
        candidates = [i for i in added if i not in consumed]
        texts = [hunk.lines[i][1] for i in candidates]
        count = match_import_guard(hunk.lines[r_index][1], texts)
        if count:
            consumed.add(r_index)
            consumed.update(candidates[:count])
            reasons["import guard"] += 1


def _benign_line_kind(lang: str, text: str, in_docstring: bool) -> str | None:
    if not text.strip():
        return "blank"
    if lang == _PY:
        if in_docstring:
            return "docstring"
        if _is_hash_comment(text, python=True):
            return "comment"
        return None
    if lang == _SH:
        return "comment" if _is_hash_comment(text, python=False) else None
    if lang == _TS:
        return "comment" if _is_slash_comment(text) else None
    return None


def classify_pipeline_paths(
    paths: list[str], file_diffs: dict[str, FileDiff] | None
) -> list[FileVerdict]:
    verdicts: list[FileVerdict] = []
    for path in paths:
        diff = (file_diffs or {}).get(path.replace("\\", "/"))
        if diff is None:
            verdicts.append(_mutation(path, "no diff text for this path"))
        else:
            verdicts.append(classify_file(diff))
    return verdicts
