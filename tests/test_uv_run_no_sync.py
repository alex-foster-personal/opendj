"""Every `uv run` command launched from this repo must carry --no-sync, unless
it targets a standalone PEP 723 script that resolves its own env.

A bare `uv run` re-syncs the environment before invoking the command, which has
taken the live preview down twice. Two scans enforce that:

PYTHON ARGVs (`ast`): every .py file under apps/ and scripts/, looking for list
literals of string constants that invoke `uv run`.

LAUNCHER COMMANDS (text): shell scripts, Playwright/Node configs, CI workflow
YAML, the root `justfile` and `Makefile` -- files that spell the command as a
STRING rather than an argv list. Restricting the guard to `rglob("*.py")` left
these unprotected and let `apps/webui/frontend/tests/e2e/
playwright.performance.config.ts` hand Playwright a bare
`uv run python -m apps.webui.server` for both mandated headed performance runs
(issue #1436, Codex P1).

Three things make this more than a literal ["uv", "run"] grep, all learned from
real misses:

1. CONFIGURABLE EXECUTABLE. The Python scan resolves simple module-level
   assignments -- `NAME = "uv"` and `NAME = os.environ.get("ENV_VAR", "uv")` --
   to their constant value, so `apps.stems.job.build_argv`'s
   `UV_BIN = os.environ.get("MDT_UV_BIN", "uv")` is walked exactly like the
   literal string. The launcher scan does the shell equivalent: it matches
   `${ANY_UV_NAME} run` as well as `uv run`, so `scripts/ci_health_notify.sh`'s
   `"${UV_BIN}" run ...` is not invisible either. Both are properties of the
   FORM, not a hand list of known worker-command names.

2. PEP 723 EXEMPTION. `uv run <script.py>` auto-detects a `# /// script`
   inline-metadata block in the target and resolves ITS OWN isolated
   environment, never the project's -- that is the whole point of a standalone
   script (CLAUDE.md: "Heavy ML deps ... never enter the repo venv: standalone
   PEP 723 scripts via uv run"). Both scans resolve a positional `<path>.py`
   (or the module named after `-m`) to a repo file and skip the violation when
   that file declares itself a PEP 723 script. Neither special-cases
   `--with`/`--script` by name -- an overlay like `--with modal` is exactly
   what `apps.stems.job.build_argv` uses to run a NON-standalone, project-env
   worker, and that one must still carry --no-sync.

3. NO SILENT SKIPS. A file that cannot be parsed fails loudly rather than
   reading as "no violations found". The INTENTIONAL_SYNC allowlist below is
   the only way a real command is excused, every entry carries a reason, and a
   second test fails if an entry stops matching anything -- so the allowlist
   cannot rot into a silent exemption for a command that has since changed.

KNOWN LIMIT, stated rather than papered over: the launcher scan is textual. A
command assembled from fragments across statements (`CMD="uv"; "$CMD" run ...`)
is not resolved, and neither is a `uv run` that only ever exists inside a
string built at runtime from a variable this scan cannot see.

[if] a list literal is ["uv", "run", "--no-project", ...] [then OK] not
    flagged (no project env to resync)
[if] a list literal is ["uv", "run", str(WORKER_SCRIPT)] [then FLAG] flagged:
    no --no-sync
[if] a list literal is ["uv", "run", "--no-sync", "python", "-m", "x"]
    [then OK] not flagged
[if] a list literal is ["uv", "run", "--with", "modal", "python", "-m", "x"]
    [then FLAG] flagged: no --no-sync
[if] UV_BIN = os.environ.get("MDT_UV_BIN", "uv") is the first element
    [then FLAG] still flagged when --no-sync is missing (previously invisible)
[if] the positional target is a repo .py file with a `# /// script` header
    [then OK] not flagged even without --no-sync (it resolves its own env)
[if] a .ts/.sh/.yml/justfile line runs `uv run python -m apps.webui.server`
    [then FLAG] flagged: no --no-sync (previously invisible)
[if] that same line is a comment (`#`, `//`, `*`) or prose that merely names
    `'uv run'` with no command after uv's own flags [then OK] not flagged
[if] an INTENTIONAL_SYNC entry stops matching any scanned command
    [then FLAG] the allowlist test fails rather than excusing nothing
[if] a scanned file has a syntax error [then FLAG] the test fails loudly
    (UNKNOWN is not a pass) rather than silently skipping it
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS: tuple[Path, ...] = (REPO_ROOT / "apps", REPO_ROOT / "scripts")

# Non-Python files that spell a `uv run` command as text. Suffixes are matched
# under SCAN_DIRS; LAUNCHER_ROOT_FILES and LAUNCHER_EXTRA_DIRS are the repo-root
# entry points and the CI workflows, which launch against this same project
# environment.
LAUNCHER_SUFFIXES: tuple[str, ...] = (
    ".sh",
    ".bash",
    ".zsh",
    ".ts",
    ".js",
    ".mjs",
    ".cjs",
    ".yml",
    ".yaml",
    ".toml",
)
LAUNCHER_ROOT_FILES: tuple[Path, ...] = (
    REPO_ROOT / "justfile",
    REPO_ROOT / "Makefile",
)
LAUNCHER_EXTRA_DIRS: tuple[Path, ...] = (REPO_ROOT / ".github" / "workflows",)

# Vendored / generated trees: not this repo's launchers.
SKIP_DIR_NAMES = frozenset(
    {
        "node_modules",
        ".svelte-kit",
        ".venv",
        "__pycache__",
        "dist",
        "build",
        "target",
        "coverage",
    }
)

# (repo-relative path, distinctive substring of the command, why it may sync).
# Every entry must still match a scanned command -- see
# test_intentional_sync_allowlist_is_live -- so an entry cannot outlive the
# command it excuses.
INTENTIONAL_SYNC: tuple[tuple[str, str, str], ...] = (
    (
        "justfile",
        "pytest -q -n auto",
        "local full-suite recipe (`just test`) with no venv-sync prerequisite: uv's "
        "pre-run sync is what provisions .venv in a fresh worktree, and that sync is "
        "INEXACT (measured, uv 0.12.5: it adds missing deps and prunes nothing), so "
        "it cannot delete the extras this guard exists to protect",
    ),
    (
        "justfile",
        "pytest -q {{args}}",
        "same recipe, single-process variant (`just test-serial`): same provisioning "
        "sync, same reason",
    ),
    (
        "justfile",
        "pytest -q {{target}}",
        "same recipe, scoped variant (`just test-scope`): same provisioning sync, "
        "same reason",
    ),
    (
        "apps/desktop/setup/setup.js",
        "python -m apps.engine_core serve",
        "not a subprocess: the copy-paste command RENDERED on the desktop setup page "
        "for a first-run user whose .venv may be empty, where the sync is the point",
    ),
)

_PEP723_HEADER_RE = re.compile(r"(?m)^# /// script\s*$")

# `uv`, or a shell variable whose NAME contains UV (`"${UV_BIN}"`, `$MDT_UV`),
# followed by the `run` subcommand. The lookbehind lets `/usr/bin/uv run` and
# `"${UV_BIN}" run` match while `deluv run` does not.
_UV_EXE = r"(?:uv|\$\{?[A-Za-z_][A-Za-z0-9_]*[Uu][Vv][A-Za-z0-9_]*\}?)"
_UV_RUN_RE = re.compile(rf"(?<![\w.-]){_UV_EXE}\s+run(?=\s|$)")

_COMMENT_PREFIXES = ("#", "//", "*", "/*")

# uv's own options that consume the following token, so the first POSITIONAL
# after them is the command being launched rather than the option's value.
_VALUE_FLAGS = frozenset(
    {
        "--with",
        "--with-requirements",
        "--with-editable",
        "--python",
        "-p",
        "--extra",
        "--group",
        "--only-group",
        "--no-group",
        "--project",
        "--directory",
        "--index",
        "--index-url",
        "--extra-index-url",
        "--find-links",
        "--constraints",
        "--overrides",
        "--exclude-newer",
        "--refresh-package",
        "--cache-dir",
        "--config-file",
        "--env-file",
        "--color",
        "--resolution",
        "--prerelease",
        "--link-mode",
        "--python-preference",
    }
)
# uv options whose VALUE *is* the target being launched.
_TARGET_FLAGS = frozenset({"-m", "--module", "-s", "--script"})

# "this argv does not touch the project environment at all", so there is no
# project venv here for --no-sync to protect.
_NO_PROJECT_FLAGS = frozenset({"--no-project"})


def _element_strings(node: ast.List, constants: dict[str, str]) -> list[str | None]:
    """Return one entry per list element: the string value for a plain
    string constant, the resolved value for a Name bound to a module-level
    constant (see `_resolve_module_constants`), or None for anything else
    (Call, BinOp, unresolvable Name, ...). Positions are preserved so
    `["uv", "run", str(x), ...]` still lets us see "uv" and "run" even though
    a later element is dynamic."""
    out: list[str | None] = []
    for elt in node.elts:
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
            out.append(elt.value)
        elif isinstance(elt, ast.Name) and elt.id in constants:
            out.append(constants[elt.id])
        else:
            out.append(None)
    return out


def _resolve_module_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level `NAME = "literal"` and `NAME = <dict-like>.get("K", "literal")`
    assignments, resolved to their constant/default string value.

    The second form is what makes a configurable-but-defaulted executable
    (`UV_BIN = os.environ.get("MDT_UV_BIN", "uv")`) visible to the scan below
    as if it were the literal "uv" -- exercising the same default this test
    process runs under, since nothing here sets MDT_UV_BIN."""
    constants: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
            value = node.value
        else:
            continue
        if len(targets) != 1 or not isinstance(targets[0], ast.Name):
            continue
        name = targets[0].id
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            constants[name] = value.value
        elif (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Attribute)
            and value.func.attr == "get"
            and len(value.args) >= 2
            and isinstance(value.args[1], ast.Constant)
            and isinstance(value.args[1].value, str)
        ):
            constants[name] = value.args[1].value
    return constants


def _has_pep723_header(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        head = path.read_text(encoding="utf-8")[:4096]
    except (UnicodeDecodeError, OSError):
        return False
    return bool(_PEP723_HEADER_RE.search(head))


def _targets_standalone_script(rest: list[str | None]) -> bool:
    """True if this command's positional target (a `<path>.py`, or the module
    named after `-m`) is a repo file carrying its own PEP 723 header -- i.e.
    `uv run` resolves that script's own isolated env and --no-sync on the
    PROJECT env is moot. `rest` is everything after the `run` subcommand."""
    for i, value in enumerate(rest):
        if value is None:
            continue
        candidate: Path | None = None
        if value.endswith(".py"):
            candidate = REPO_ROOT / value
        elif value == "-m" and i + 1 < len(rest):
            next_value = rest[i + 1]
            if next_value:
                candidate = REPO_ROOT / (next_value.replace(".", "/") + ".py")
        if candidate is not None and _has_pep723_header(candidate):
            return True
    return False


def _find_uv_run_violations(tree: ast.Module) -> list[tuple[int, list[str | None]]]:
    constants = _resolve_module_constants(tree)

    violations: list[tuple[int, list[str | None]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.List):
            continue
        elements = _element_strings(node, constants)
        for i in range(len(elements) - 1):
            if elements[i] == "uv" and elements[i + 1] == "run":
                rest = elements[i + 2 :]
                has_no_sync = "--no-sync" in rest
                # `--no-project` is the other way to say "not the project
                # env": it skips project resolution entirely in favor of an
                # explicit `--with-requirements` manifest, so there is no
                # project venv here for --no-sync to protect.
                targets_project_env = not _NO_PROJECT_FLAGS.intersection(
                    value for value in rest if value is not None
                ) and not _targets_standalone_script(rest)
                if not has_no_sync and targets_project_env:
                    violations.append((node.lineno, elements))
                break  # one finding per list literal is enough
    return violations


# ----- launcher (non-Python) scan -----------------------------------------


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """(1-based line number of the first physical line, joined text). A shell,
    justfile or YAML command continued with a trailing backslash is ONE command,
    and its `--no-sync` may sit on a later physical line."""
    out: list[tuple[int, str]] = []
    buffer = ""
    start = 0
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if not buffer:
            start = lineno
        stripped = raw.rstrip()
        if stripped.endswith("\\"):
            buffer += stripped[:-1] + " "
            continue
        out.append((start, buffer + raw))
        buffer = ""
    if buffer:
        out.append((start, buffer))
    return out


def _quote_spans(line: str) -> list[tuple[int, int]]:
    """(open index, index just past the close) for every `'...'`, `"..."` and
    backtick run at this nesting level."""
    spans: list[tuple[int, int]] = []
    i = 0
    while i < len(line):
        char = line[i]
        if char in "'\"`":
            close = line.find(char, i + 1)
            if close == -1:
                break
            spans.append((i, close + 1))
            i = close + 1
        else:
            i += 1
    return spans


def _innermost_quote_end(line: str, match_start: int) -> int | None:
    """Index of the closing quote of the INNERMOST quoted run holding the
    match, or None when the match is unquoted. Innermost matters: a shell
    diagnostic string can name `'uv run --with cryptography'` inside a
    double-quoted message, and only that inner run is the command being named
    -- taking the outer span would drag the rest of the sentence in as argv."""
    interior_start, interior_end = 0, len(line)
    end: int | None = None
    while True:
        enclosing = next(
            (
                (interior_start + open_at, interior_start + close_at)
                for open_at, close_at in _quote_spans(line[interior_start:interior_end])
                if interior_start + open_at < match_start < interior_start + close_at
            ),
            None,
        )
        if enclosing is None:
            return end
        opened_at, closed_at = enclosing
        end = closed_at - 1
        interior_start, interior_end = opened_at + 1, closed_at - 1


def _command_slice(line: str, match_start: int) -> str:
    """The command text starting at `uv run`. Ends at the close of the string
    literal the match sits inside (Playwright configs and justfile recipes
    spell commands as quoted strings), else at the first shell separator."""
    quoted_end = _innermost_quote_end(line, match_start)
    if quoted_end is not None:
        return line[match_start:quoted_end]
    end = len(line)
    for separator in (";", "&&", "||", "|", ")"):
        found = line.find(separator, match_start)
        if found != -1:
            end = min(end, found)
    return line[match_start:end]


def _strip_quotes(token: str) -> str:
    return token.strip("'\"`")


def _launch_target_index(rest: list[str]) -> int | None:
    """Index in `rest` (everything after `run`) of the command uv will launch,
    skipping uv's own options and the values they consume. None when there is
    no command at all -- `uv run` named in prose, not invoked."""
    i = 0
    while i < len(rest):
        token = rest[i]
        if token in _TARGET_FLAGS:
            return i + 1 if i + 1 < len(rest) else None
        if token.startswith("-"):
            if token in _VALUE_FLAGS:
                i += 2
                continue
            i += 1
            continue
        return i
    return None


def _find_launcher_violations(text: str) -> list[tuple[int, str]]:
    violations: list[tuple[int, str]] = []
    for lineno, line in _logical_lines(text):
        if line.lstrip().startswith(_COMMENT_PREFIXES):
            continue
        for match in _UV_RUN_RE.finditer(line):
            command = _command_slice(line, match.start())
            tokens = [_strip_quotes(token) for token in command.split()]
            # tokens[0] is the executable, tokens[1] is "run".
            rest = tokens[2:]
            if _launch_target_index(rest) is None:
                continue  # prose naming `uv run`, with no command to launch
            if "--no-sync" in rest:
                continue
            if _NO_PROJECT_FLAGS.intersection(rest):
                continue
            if _targets_standalone_script(list(rest)):
                continue
            violations.append((lineno, command.strip()))
    return violations


# ----- file discovery ------------------------------------------------------


def _walk(base: Path) -> list[Path]:
    if not base.is_dir():
        return []
    return [
        path
        for path in base.rglob("*")
        if path.is_file() and not SKIP_DIR_NAMES.intersection(path.parts)
    ]


def _all_scanned_py_files() -> list[Path]:
    files: list[Path] = []
    for base in SCAN_DIRS:
        files.extend(path for path in _walk(base) if path.suffix == ".py")
    return sorted(files)


def _all_scanned_launcher_files() -> list[Path]:
    files: list[Path] = []
    for base in (*SCAN_DIRS, *LAUNCHER_EXTRA_DIRS):
        files.extend(path for path in _walk(base) if path.suffix in LAUNCHER_SUFFIXES)
    files.extend(path for path in LAUNCHER_ROOT_FILES if path.is_file())
    return sorted(set(files))


def _launcher_findings() -> list[tuple[str, int, str]]:
    """(repo-relative path, line, command) for every launcher `uv run` that
    lacks --no-sync, allowlist NOT yet applied."""
    findings: list[tuple[str, int, str]] = []
    for path in _all_scanned_launcher_files():
        rel = str(path.relative_to(REPO_ROOT))
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as exc:
            # Unreadable is UNKNOWN, and UNKNOWN is not a pass.
            findings.append((rel, 0, f"UNREADABLE, cannot verify: {exc}"))
            continue
        findings.extend(
            (rel, lineno, command)
            for lineno, command in _find_launcher_violations(text)
        )
    return findings


def _allowlist_reason(rel: str, command: str) -> str | None:
    for allowed_path, marker, reason in INTENTIONAL_SYNC:
        if rel == allowed_path and marker in command:
            return reason
    return None


# ----- tests ---------------------------------------------------------------


def test_every_uv_run_argv_has_no_sync() -> None:
    all_violations: list[str] = []
    for path in _all_scanned_py_files():
        # A parse failure must not read as "no violations found" -- that is
        # the exact "checks that cannot fail" shape this repo bans. Surface
        # it as its own loud failure, distinct from a real --no-sync miss.
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            rel = path.relative_to(REPO_ROOT)
            all_violations.append(f"{rel}: SYNTAX ERROR, cannot verify: {exc}")
            continue
        for lineno, strings in _find_uv_run_violations(tree):
            rel = path.relative_to(REPO_ROOT)
            all_violations.append(f"{rel}:{lineno}: {strings!r} is missing --no-sync")

    assert not all_violations, (
        "Found `uv run` argv list(s) without --no-sync against the project "
        "env (this re-syncs the venv and prunes engine extras on the live "
        "preview), or a file that could not be parsed to check:\n"
        + "\n".join(all_violations)
    )


def test_every_uv_run_launcher_command_has_no_sync() -> None:
    """The half `rglob("*.py")` could not see: shell scripts, Playwright and
    Node configs, CI workflow YAML, the root justfile and Makefile."""
    unexcused = [
        f"{rel}:{lineno}: {command!r} is missing --no-sync"
        for rel, lineno, command in _launcher_findings()
        if _allowlist_reason(rel, command) is None
    ]
    assert not unexcused, (
        "Found `uv run` launcher command(s) without --no-sync against the "
        "project env. Add --no-sync, or add an INTENTIONAL_SYNC entry in this "
        "file stating why the sync is wanted:\n" + "\n".join(unexcused)
    )


def test_intentional_sync_allowlist_is_live() -> None:
    """An allowlist entry that no longer matches any command is a silent
    exemption waiting to happen: the command it excused may have moved,
    changed, or been deleted, and nobody would be told."""
    findings = _launcher_findings()
    dead = [
        f"{allowed_path}: marker {marker!r} matches no scanned `uv run` command "
        f"(reason on file: {reason})"
        for allowed_path, marker, reason in INTENTIONAL_SYNC
        if not any(
            rel == allowed_path and marker in command for rel, _, command in findings
        )
    ]
    assert not dead, (
        "INTENTIONAL_SYNC entries no longer match anything -- delete them or "
        "re-point them at the command they excuse:\n" + "\n".join(dead)
    )
