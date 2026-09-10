"""Shared machinery for the NATIVE-12 guard suites.

Two concerns live here. The registry helpers build the throwaway registries and
record documents both suites consume through the production loader. The scanner
is the static half's instrument: it reads Python by AST and every other source
by path tokens, and answers one question -- does this file name the corpus?

Nothing here is a replacement implementation. A throwaway registry is a real
JSON file in a real shape; the scanner reads the real checkout.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

from apps.mik import reference_guard


def corpus_path() -> str:
    """The corpus location as the registry states it, slash-joined."""
    return "/".join(reference_guard.corpus_segments())


def corpus_tail() -> tuple[str, str]:
    """The last two segments of the corpus path, from the registry that owns it."""
    first, second = reference_guard.corpus_segments()[-2:]
    return first, second


def write_registry(path: Path, decisions: list[dict[str, str]]) -> Path:
    """Write a throwaway registry, taking corpus_path from the real one."""
    path.write_text(
        json.dumps({"version": 1, "corpus_path": corpus_path(), "decisions": decisions}),
        encoding="utf-8",
    )
    return path


def write_record(tmp_path: Path, decision_id: str) -> Path:
    """Write the decision document an entry must be backed by."""
    record = tmp_path / "decision.md"
    record.write_text(f"Decision {decision_id} records why this import was approved.\n", "utf-8")
    return record


def registry_entry(module: str, decision_id: str, action: str = "import") -> dict[str, str]:
    """A registry entry naming the record document ``write_record`` wrote."""
    return {"module": module, "action": action, "decision": decision_id, "record": "decision.md"}


SKIPPED_DIR_NAMES: frozenset[str] = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".remember",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "_vendor",
        "build",
        "data",
        "derived",
        "dist",
        "node_modules",
        "payload",
        "site",
        "target",
    }
)
SKIPPED_DIR_PREFIXES: tuple[str, ...] = (".tmp",)

# Sources that cannot call the Python refusal, so their corpus reads are found
# by path text rather than by AST.
TEXT_SUFFIXES: tuple[str, ...] = (
    ".bash",
    ".bat",
    ".cjs",
    ".cmd",
    ".js",
    ".json",
    ".mjs",
    ".ps1",
    ".rs",
    ".sh",
    ".svelte",
    ".ts",
    ".yaml",
    ".yml",
)

# Sources allowed to NAME the corpus without an import decision, with why. Only
# this suite is listed: its corpus paths are strings it hands to the scanner,
# and the production guard holds no path at all (the registry states it).
DEFINITION_SITES: dict[str, str] = {
    "apps/mik/import_decisions.json": (
        "states corpus_path, which is the single declaration of where the corpus is; "
        "it is the registry the guard reads, not a reader of the corpus"
    ),
    "tests/mik/guard_helpers.py": (
        "reads the corpus location out of the registry so both suites build the same "
        "throwaway registry; it opens no corpus file"
    ),
    "tests/mik/test_reference_import_guard_sweep.py": (
        "the scan and its synthetic control sources; every corpus path here is text "
        "fed to the scanner, not a location this module reads"
    ),
}

# The guard module: holding it is holding the corpus, whatever local name it is
# bound to, so an aliased import is classified alongside a literal path.
GUARD_MODULE: str = "apps.mik.reference_guard"

# Guard names whose use in code means "I am holding the corpus path". Without
# this the sweep would only see modules that spell the location out.
CORPUS_HANDLE_NAMES: frozenset[str] = frozenset({"corpus_segments", "MIK_REFERENCE_ROOT"})

PATH_FACTORY_NAMES: frozenset[str] = frozenset(
    {"Path", "PurePath", "PosixPath", "PurePosixPath", "WindowsPath", "PureWindowsPath", "join"}
)


TEXT_TOKEN_SEPARATORS = re.compile(r"[^A-Za-z0-9_.\-]+")





def split_segments(text: str) -> list[str]:
    """Split a path-ish string into its segments, either separator, case-folded."""
    return [part.strip().lower() for part in text.replace("\\", "/").split("/")]


def names_corpus(segments: list[str]) -> bool:
    """Does this ordered segment list contain the corpus tail, adjacent?"""
    tail = corpus_tail()
    width = len(tail)
    return any(tuple(segments[i : i + width]) == tail for i in range(len(segments) - width + 1))


def docstring_constants(tree: ast.Module) -> set[int]:
    """``id()`` of every string constant that is a docstring, not a value.

    A path mentioned in prose is documentation. Counting it would make the sweep
    cry wolf on every comment that explains the rule it enforces.
    """
    found: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if not node.body:
            continue
        first = node.body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            found.add(id(first.value))
    return found


def literal_parts(node: ast.AST, bindings: dict[str, list[str]] | None = None) -> list[str] | None:
    """Ordered literal segments of a path expression built from strings, or None.

    Handles ``Path("a", "b")``, ``os.path.join("a", "b")``, the ``/`` operator
    chained over literals and over a variable base, and a NAME that was assigned
    a path expression earlier in the file. A non-literal operand contributes
    nothing rather than making the whole expression opaque: the corpus tail only
    has to appear somewhere in the literal run for the read to be classified,
    and ``DATA_DIR / "reference" / "mik"`` is exactly that shape.
    """
    known = bindings or {}
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return split_segments(node.value)
    if isinstance(node, ast.Name):
        return list(known[node.id]) if node.id in known else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return (literal_parts(node.left, known) or []) + (literal_parts(node.right, known) or [])
    if isinstance(node, ast.Call):
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name not in PATH_FACTORY_NAMES:
            return None
        parts: list[str] = []
        for arg in node.args:
            parts += literal_parts(arg, known) or []
        return parts
    return None


def string_bindings(tree: ast.Module) -> dict[str, list[str]]:
    """Every ``name = <path expression>`` in the file, resolved to its segments.

    A sweep that only reads literals in place is defeated by the cheapest
    disguise there is: assign the segments to names, then join the names.
    Bindings are collected wherever they appear, module level or inside a
    function, because the question is what the name can hold, not where it is
    read. A value that reaches the join through a function ARGUMENT, a
    container or a computation is still outside this.
    """
    bindings: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        target: str | None = None
        value: ast.expr | None = None
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            target, value = node.targets[0].id, node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
        ):
            target, value = node.target.id, node.value
        if target is None or value is None:
            continue
        parts = literal_parts(value, bindings)
        if parts:
            bindings[target] = parts
    return bindings


def corpus_sites(tree: ast.Module) -> list[str]:
    """One line-numbered description per line that names the corpus or its guard."""
    docstrings = docstring_constants(tree)
    bindings = string_bindings(tree)
    sites: dict[int, str] = {}
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Name) and node.id in CORPUS_HANDLE_NAMES:
            if isinstance(node.ctx, ast.Load):
                sites.setdefault(line, f"line {line}: name {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in CORPUS_HANDLE_NAMES:
            sites.setdefault(line, f"line {line}: attribute {node.attr}")
        elif isinstance(node, ast.ImportFrom) and node.module == GUARD_MODULE:
            sites.setdefault(line, f"line {line}: from {GUARD_MODULE} import ...")
        elif isinstance(node, ast.Import) and any(
            alias.name == GUARD_MODULE for alias in node.names
        ):
            sites.setdefault(line, f"line {line}: import {GUARD_MODULE}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings:
                continue
            if names_corpus(split_segments(node.value)) or GUARD_MODULE in node.value:
                sites.setdefault(line, f"line {line}: {node.value!r}")
        elif isinstance(node, ast.BinOp | ast.Call):
            parts = literal_parts(node, bindings)
            if parts is not None and names_corpus(parts):
                sites.setdefault(line, f"line {line}: {'/'.join(parts)}")
    return [sites[key] for key in sorted(sites)]


def text_tokens(line: str) -> list[str]:
    """Path-ish tokens of one non-Python source line, case-folded.

    Splitting on separators rather than only on a slash is what makes a quoted
    TypeScript literal and a ``path.join`` argument list readable: neither has
    a slash between "reference" and "mik" for a path split to find.
    """
    return [token.lower() for token in TEXT_TOKEN_SEPARATORS.split(line) if token]


def text_sites(path: Path) -> list[str]:
    """Lines of a non-Python source that name the corpus.

    Shell, Rust, TypeScript, workflow YAML and the Windows script formats
    cannot call the Python refusal, so there is no runtime half for them; the
    sweep matches the path textually instead, and a comment counts. Tokens are
    taken from the whole file rather than per line, so a ``path.join`` split
    across lines is still one token run. The substring pre-filter keeps the
    tokenizing off files that carry no "reference" at all.

    Decoding is strict: a byte the scanner cannot read must fail the suite, not
    be replaced and then quietly not match.
    """
    text = path.read_text(encoding="utf-8")
    if "reference" not in text.lower():
        return []
    tokens: list[tuple[int, str]] = [
        (number, token)
        for number, line in enumerate(text.splitlines(), 1)
        for token in text_tokens(line)
    ]
    tail = corpus_tail()
    sites: dict[int, str] = {}
    for index in range(len(tokens) - 1):
        pair = (tokens[index][1], tokens[index + 1][1])
        if pair == tail:
            sites.setdefault(tokens[index][0], f"line {tokens[index][0]}: {'/'.join(pair)}")
    return [sites[number] for number in sorted(sites)]


def scanned_files(root: Path) -> list[Path]:
    """Every source file under the scan roots, sorted, caches and vendored trees skipped.

    The walk prunes the skipped directories rather than filtering paths after
    the fact: ``apps/desktop/src-tauri/payload`` is a vendored CPython of tens
    of thousands of files and walking into it costs seconds per run.
    """
    files: list[Path] = []
    stack = [root]
    while stack:
        directory = stack.pop()
        for entry in directory.iterdir():
            if entry.is_dir():
                if entry.name in SKIPPED_DIR_NAMES or entry.name.startswith(SKIPPED_DIR_PREFIXES):
                    continue
                stack.append(entry)
            elif entry.suffix in {".py", *TEXT_SUFFIXES}:
                files.append(entry)
    return sorted(files)


def corpus_namers(root: Path) -> dict[str, list[str]]:
    """Relative path -> reported sites, for every source under ``root`` naming the corpus."""
    found: dict[str, list[str]] = {}
    for path in scanned_files(root):
        if path.suffix == ".py":
            source = path.read_text(encoding="utf-8")
            sites = corpus_sites(ast.parse(source, filename=str(path)))
        else:
            sites = text_sites(path)
        if sites:
            found[path.relative_to(root).as_posix()] = sites
    return found
