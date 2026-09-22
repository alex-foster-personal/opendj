"""PERFBATCH-02/03 gate: no pipeline throughput/latency change without idle+quality.

A PR that does not touch a stems or lyrics pipeline path is measurement-only
and passes with no markers. A PR that does touch a pipeline path is classified
by the CONTENT of its hunks (PERFBATCH-07): when every added or removed line in
that file is on the narrow allowlist below, the touch is non-behavioral and
needs no markers. Anything else, including a hunk the gate cannot read, is a
pipeline mutation and must carry both of these on ONE line each, horizontal
whitespace only:

    perfbatch: pipelines-idle <non-empty-evidence>
    perfbatch-03: quality-ok reqs=<ID> signal=<path> before_after=<path>

Allowlist (see docs/perf/perfbatch-gates.md for the reasoning and residuals):

    Python   blank lines; ``#`` comments that are not a shebang and not a
             PEP 723 metadata line; docstring lines whose opener follows a
             ``def``/``class`` header (or opens the module); widening of an
             ``except (...)`` / ``contextlib.suppress(...)`` tuple to a
             strict superset that adds no broad base; wrapping an existing
             import in ``try: ... except ImportError: <name> = None``.
    Shell    blank lines; ``#`` comments that are not a shebang.
    TS/JS    blank lines; ``//`` line comments (never ``*`` block lines).
    Markdown every line (prose is never executed).
    Other    nothing: any changed line is a mutation.

The gate fails closed: a pipeline path with no parseable hunks (binary,
rename-only, mode-only, or a name list with no diff text) is a mutation.
``gh pr view`` / ``gh pr diff`` / ``git diff`` failing prints UNKNOWN and
exits 2, never a silent pass.

    python -m scripts.perf.perfbatch_gate --pr N
    python -m scripts.perf.perfbatch_gate --diff
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REPO = "maintainer/music-dj-tools"
DEFAULT_BASE = "origin/main"

PIPELINE_PREFIXES: tuple[str, ...] = (
    "apps/stems/",
    "apps/lyrics/",
    "scripts/stems_modal_worker.py",
    "scripts/stems_local_worker.py",
    "scripts/stem_bundle_worker.py",
    "scripts/stem_farm_runner.py",
    "scripts/stem_farm_watch_pull_down.sh",
    "scripts/modal_vocal_farm.py",
    "scripts/modal_demucs_ab.py",
    "apps/webui/server/lyric_index_autostart.py",
    "apps/webui/frontend/src/lib/components/rb/wave/lyrics-fetch.svelte.ts",
)

# Install-time machine capability (LATENCY-04) is not a stems/lyrics throughput
# or latency pipeline mutation: it benchmarks once, persists a JSON record, and
# exposes a read-only route. PERFBATCH-02/03 still apply to real pipeline edits.
PIPELINE_EXEMPT: tuple[str, ...] = (
    "apps/stems/live_capability.py",
    "apps/stems/live_capability_api.py",
    # Backward-compat kwarg restore only (``root=``); no throughput/latency change.
    "apps/lyrics/register_stems.py",
)

# AGT-01 (issue #2123): luna primer plus persona checker. Markdown lives under
# ops/agentic-testing/; the importable checker lives under ops/agentic_testing/.
# Neither path changes stems or lyrics throughput or latency.
#
# PERF-UI-01 (issue #2303): short-viewport /performance layout (compact
# waverow, library panel auto-collapse, history list hide). Viewport CSS/DOM
# only; no stems or lyrics throughput or latency change.
MEASUREMENT_ONLY_PREFIXES: tuple[str, ...] = (
    "scripts/perf/",
    "tests/perf/",
    "tests/fixtures/perf/",
    "docs/perf/",
    "specs/perf-latency-program.md",
    ".planning/REQUIREMENTS.md",
    "reqs.json",
    "justfile",
    ".github/workflows/perfbatch-gate.yml",
    "ops/agentic-testing/",
    "ops/agentic_testing/",
    "tests/agentic_testing/",
    "docs/decisions/",
    "specs/",
)

# Horizontal whitespace only. Never `\\s`: a newline as the "reason" must fail.
_IDLE = re.compile(r"perfbatch:[ \t]*pipelines-idle[ \t]+\S", re.IGNORECASE)
_QUALITY = re.compile(
    r"perfbatch-03:[ \t]*quality-ok[ \t]+reqs=\S+[ \t]+signal=\S+[ \t]+before_after=\S+",
    re.IGNORECASE,
)


def _matches_prefix(path: str, prefixes: tuple[str, ...]) -> bool:
    normalized = path.replace("\\", "/")
    return any(normalized == prefix or normalized.startswith(prefix) for prefix in prefixes)


def pipeline_mutation_paths(paths: list[str]) -> list[str]:
    """Pipeline-prefixed paths that are not file-level exempt (the hunk rule applies next)."""
    return [
        path
        for path in paths
        if _matches_prefix(path, PIPELINE_PREFIXES) and not _matches_prefix(path, PIPELINE_EXEMPT)
    ]


def measurement_only_paths(paths: list[str]) -> list[str]:
    return [path for path in paths if _matches_prefix(path, MEASUREMENT_ONLY_PREFIXES)]


def idle_marker(body: str) -> bool:
    return bool(_IDLE.search(body))


def quality_marker(body: str) -> bool:
    return bool(_QUALITY.search(body))


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


_DIFF_HEADER = re.compile(r'^diff --git "?a/(?P<a>.*?)"? "?b/(?P<b>.*?)"?$')
_HUNK_HEADER = re.compile(r"^@@ -(?P<old>\d+)(?:,\d+)? \+(?P<new>\d+)(?:,\d+)? @@")


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
            continue  # extended headers: index, mode, rename, similarity
        if raw.startswith("\\"):
            continue  # "\ No newline at end of file"
        tag = raw[:1] if raw else " "
        if tag not in (" ", "+", "-"):
            hunk = None  # trailing junk after a hunk; stop attributing lines to it
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

# Names that make an exception widening broad enough to swallow cancellation
# or interpreter exit. Adding any of these is never allowlisted.
_BROAD_EXCEPTIONS = frozenset(
    {
        "Exception",
        "BaseException",
        "KeyboardInterrupt",
        "SystemExit",
        "GeneratorExit",
        "CancelledError",
        "asyncio.CancelledError",
        "concurrent.futures.CancelledError",
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
_DOCSTRING_OPEN_RE = re.compile(r"^\s*[rR]?(?P<q>\"\"\"|''')")
_DOCSTRING_ONE_LINE_RE = re.compile(
    r"^\s*[rR]?(?P<q>\"\"\"|''')(?:(?!(?P=q)).)*(?P=q)" + _TRAILING_COMMENT
)
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
    return not (new_names - old_names) & _BROAD_EXCEPTIONS


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


def _docstring_indices(hunk: Hunk, tag: str, start_line: int) -> set[int]:
    """Hunk indices (on side ``tag``) that lie inside a docstring.

    A triple-quoted opener counts as a docstring only when the nearest
    preceding non-blank, non-comment line on that side is a ``def``/``class``
    header (or a multi-line signature close), or when nothing but comments and
    blank lines precede it from line 1 of the file. Any other triple-quoted
    string (a prompt, SQL, a shell template) is code and is NOT allowlisted.
    """
    inside: set[int] = set()
    prev_code: str | None = None
    at_file_top = start_line == 1
    quote: str | None = None
    block: list[int] = []
    for index, text in _side_lines(hunk, tag):
        if quote is not None:
            block.append(index)
            if quote in text:
                tail = text.split(quote, 1)[1].strip()
                if not tail or tail.startswith("#"):
                    inside.update(block)
                # Code after the closer (``""".format(x)``) is an expression,
                # not a bare docstring: the whole block stays unclassified.
                quote = None
                block = []
                prev_code = text
            continue
        stripped = text.strip()
        if not stripped or stripped.startswith("#"):
            continue
        opener = _DOCSTRING_OPEN_RE.match(text)
        follows_header = prev_code is not None and bool(
            _BLOCK_HEADER_RE.match(prev_code) or _SIGNATURE_CLOSE_RE.match(prev_code)
        )
        if opener and (follows_header or (prev_code is None and at_file_top)):
            delimiter = opener.group("q")
            if delimiter in text[opener.end() :]:
                if _DOCSTRING_ONE_LINE_RE.match(text):
                    inside.add(index)
            else:
                quote = delimiter
                block = [index]
        prev_code = text
    return inside


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


def classify_file(diff: FileDiff) -> FileVerdict:
    """Benign only when every changed line is on the allowlist; otherwise a mutation."""
    lang = _language(diff.path)
    if diff.binary:
        return _mutation(diff.path, "binary diff has no readable hunks")
    if not diff.hunks:
        return _mutation(diff.path, "no readable hunks (rename-only, mode-only, or no diff text)")
    if lang is None:
        suffix = Path(diff.path).suffix or "<none>"
        return _mutation(diff.path, f"no allowlist for {suffix} files")
    reasons: Counter[str] = Counter()
    for hunk in diff.hunks:
        changed = [i for i, (tag, _) in enumerate(hunk.lines) if tag != " "]
        if not changed:
            continue
        if lang == _PROSE:
            reasons["prose"] += len(changed)
            continue
        consumed: set[int] = set()
        if lang == _PY:
            old_doc = _docstring_indices(hunk, "-", hunk.old_start)
            new_doc = _docstring_indices(hunk, "+", hunk.new_start)
            for block_removed, block_added in _replacement_blocks(hunk):
                _pair_python_blocks(hunk, block_removed, block_added, consumed, reasons)
        else:
            old_doc = new_doc = set()
        for index in changed:
            if index in consumed:
                continue
            tag, text = hunk.lines[index]
            kind = _benign_line_kind(lang, text, index in (new_doc if tag == "+" else old_doc))
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
) -> None:
    """Consume exception widenings and import guards inside one replacement block."""
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


# ----------------------------------------------------------------------------
# Data sources


def _run(argv: list[str], *, cwd: Path | None = None) -> str:
    # git and gh emit UTF-8 regardless of the console code page; decoding with
    # the Windows default (cp1252) raised inside the reader thread and left
    # stdout empty, which the gate then read as "no diff text" (fail-closed,
    # but for the wrong reason). Undecodable bytes become U+FFFD, which is
    # never on the allowlist, so a garbled code line still fails closed.
    proc = subprocess.run(
        argv,
        cwd=cwd,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(argv)} failed rc={proc.returncode}: {proc.stderr.strip()}")
    return proc.stdout


def pr_view(pr: int, repo: str = DEFAULT_REPO) -> dict:
    out = _run(["gh", "pr", "view", str(pr), "--repo", repo, "--json", "number,title,body"])
    return json.loads(out)


def pr_diff_names(pr: int, repo: str = DEFAULT_REPO) -> list[str]:
    out = _run(["gh", "pr", "diff", str(pr), "--repo", repo, "--name-only"])
    return [line.strip() for line in out.splitlines() if line.strip()]


def pr_diff_text(pr: int, repo: str = DEFAULT_REPO) -> str:
    return _run(["gh", "pr", "diff", str(pr), "--repo", repo])


def _merge_base(repo_root: Path, base: str) -> str:
    return _run(["git", "merge-base", base, "HEAD"], cwd=repo_root).strip()


def local_diff_names(repo_root: Path = REPO_ROOT, base: str = DEFAULT_BASE) -> list[str]:
    """Net change of the working tree (committed, staged, unstaged) against the merge base."""
    out = _run(["git", "diff", "--name-only", _merge_base(repo_root, base)], cwd=repo_root)
    return sorted({line.strip() for line in out.splitlines() if line.strip()})


def local_diff_text(paths: list[str], repo_root: Path = REPO_ROOT, base: str = DEFAULT_BASE) -> str:
    return _run(["git", "diff", _merge_base(repo_root, base), "--", *paths], cwd=repo_root)


# ----------------------------------------------------------------------------
# Verdict


def verdict(
    paths: list[str], body: str, file_diffs: dict[str, FileDiff] | None = None
) -> tuple[int, str]:
    candidates = pipeline_mutation_paths(paths)
    if not candidates:
        if paths and len(measurement_only_paths(paths)) == len(paths):
            return 0, "[perfbatch-gate] OK -- measurement-only (declared prefix)"
        return 0, "[perfbatch-gate] OK -- measurement-only"
    verdicts = classify_pipeline_paths(candidates, file_diffs)
    mutations = [v for v in verdicts if not v.benign]
    if not mutations:
        listed = ", ".join(v.describe() for v in verdicts)
        return 0, f"[perfbatch-gate] OK -- measurement-only; allowlisted hunks only in {listed}"
    missing: list[str] = []
    if not idle_marker(body):
        missing.append("perfbatch: pipelines-idle <evidence>")
    if not quality_marker(body):
        missing.append("perfbatch-03: quality-ok reqs= signal= before_after=")
    if not missing:
        return 0, "[perfbatch-gate] OK -- pipeline mutation cited idle+quality"
    listed = ", ".join(v.describe() for v in mutations)
    needed = "; ".join(missing)
    return (
        1,
        f"[perfbatch-gate] pipeline-mutation path(s) {listed} need {needed}",
    )


def main(  # noqa: PLR0913 -- every keyword is a test seam for one data source
    argv: list[str] | None = None,
    *,
    fetch=pr_view,
    fetch_files=pr_diff_names,
    fetch_diff=pr_diff_text,
    diff_names: list[str] | None = None,
    diff_text: str | None = None,
    body: str | None = None,
    repo_root: Path = REPO_ROOT,
    base: str = DEFAULT_BASE,
) -> int:
    """``diff_names`` injected without ``diff_text`` fails closed: no hunks, so mutation."""
    parser = argparse.ArgumentParser(prog="perfbatch_gate")
    parser.add_argument("--pr", type=int)
    parser.add_argument("--diff", action="store_true")
    parser.add_argument("--repo", default=DEFAULT_REPO)
    args = parser.parse_args(argv)
    if not args.pr and not args.diff:
        print("[perfbatch-gate] UNKNOWN: pass --pr N or --diff", file=sys.stderr)
        return 2

    pr_body = body if body is not None else ""
    paths = list(diff_names) if diff_names is not None else None
    text = diff_text if diff_text is not None else ("" if diff_names is not None else None)

    if args.pr:
        try:
            pr = fetch(args.pr, args.repo)
        except Exception as exc:
            print(
                f"[perfbatch-gate] UNKNOWN: could not read PR #{args.pr} ({exc})",
                file=sys.stderr,
            )
            return 2
        pr_body = body if body is not None else (pr.get("body") or "")
        if paths is None:
            try:
                paths = fetch_files(args.pr, args.repo)
            except Exception as exc:
                print(
                    f"[perfbatch-gate] UNKNOWN: could not list PR #{args.pr} files ({exc})",
                    file=sys.stderr,
                )
                return 2

    if paths is None:
        try:
            paths = local_diff_names(repo_root, base)
        except Exception as exc:
            print(f"[perfbatch-gate] UNKNOWN: could not read local diff ({exc})", file=sys.stderr)
            return 2

    candidates = pipeline_mutation_paths(paths)
    if candidates and text is None:
        try:
            if args.pr:
                text = fetch_diff(args.pr, args.repo)
            else:
                text = local_diff_text(candidates, repo_root, base)
        except Exception as exc:
            listed = ", ".join(candidates)
            print(
                f"[perfbatch-gate] UNKNOWN: could not read hunks for {listed} ({exc})",
                file=sys.stderr,
            )
            return 2

    file_diffs = parse_unified_diff(text) if text is not None else None
    code, message = verdict(paths, pr_body, file_diffs)
    stream = sys.stdout if code == 0 else sys.stderr
    print(message, file=stream)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
