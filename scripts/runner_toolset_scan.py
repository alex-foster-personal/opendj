"""Static scan of what a self-hosted Linux CI runner must provide.

Reads the places CI can reach an external executable from and reports each
executable, apt package and Playwright browser it names, with the file and
line that names it. `tests/scripts/test_runner_toolset_complete.py` compares
that against `ci/runner-toolset.yml`, the declared toolset
(docs/decisions/ADR-NEW-runner-toolset-manifest.md), so a tool a job needs is
declared before a host first fails on it rather than after.

Surfaces scanned:
  1. `run:` blocks of every workflow (`.github/workflows/*.yml` and `*.yaml`)
     job whose `runs-on` reads a `vars.CI_RUNS_ON_*` variable (the
     self-hosted pool). Jobs pinned to
     GitHub-hosted images (ubuntu-latest, macos-*, windows-*) run on an image
     GitHub provisions, so they are out of scope.
  2. Repo shell and Python scripts those blocks call, followed transitively
     for shell scripts (`scripts/x.sh`, `python -m scripts.x`, `python
     scripts/x.py`, also through a relative interpreter such as
     `.venv/bin/python`). A Python entry point is read for its own subprocess
     calls and, transitively, for those of the repo-local modules it imports
     (`scripts.*`, `ops.*`, `apps.*`; third-party imports are not followed).
     A command launched from another's arguments (`find -exec`, `uv run`,
     `doppler run --`: scripts/runner_toolset_shell_lex.LAUNCHERS) is walked too.
  3. justfile and Makefile recipes those blocks or scripts invoke (`just
     <recipe>`, `make <target>`), with their dependencies.
  4. Every Python file under `tests/`: `subprocess.*([...])` argv literals and
     `shutil.which("...")`, since the pytest lanes run the suite on the runner.

Requirements:
  ✔︎ ✅ 🎯 R1 an executable at command position in a scanned run block is
    reported with its file:line.
    [if] `unzip` is called in a test via subprocess.run(["unzip", ...]) [then ⛔️]
      it is missing from the report.
    [if] a word appears only in a shell comment [then ⛔️] it is reported.
    [if] a word appears only as an argument (`echo unzip`) [then ⛔️] it is reported.
  ✔︎ ✅ 🎯 R2 apt packages named to ci_apt_present.sh or apt-get install are
    reported as packages, not executables.
  ✔︎ ✅ 🎯 R3 Playwright browsers named to `playwright install` are reported.

CLI (read-only): `python -m scripts.runner_toolset_scan [--json]` prints every
used name with its first source.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import shlex
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from scripts import runner_toolset_shell_lex as lex
from scripts import runner_toolset_sources as sources

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "ci" / "runner-toolset.yml"
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
RECIPE_FILES = {"just": REPO_ROOT / "justfile", "make": REPO_ROOT / "Makefile"}
TESTS_DIR = REPO_ROOT / "tests"
MANIFEST_REQUIRED = {"name", "kind", "version", "source", "install", "verify", "needed_by"}

# ----- shell grammar knobs -------------------------------------------------------

KEYWORDS_BEFORE_COMMAND = {"then", "do", "else", "elif", "if", "while", "until", "time", "!"}
COMPOUND_OPENERS = {"{", "(", "((", "[[", "if", "while", "until", "for", "case", "select"}
KEYWORDS_IGNORED = {"fi", "done", "esac", "in", "function", "select", "}", "{"}
# Wrappers that run a command given as their arguments. Value: (flags that take
# a value, positional args before the wrapped command, is itself an executable).
WRAPPERS: dict[str, tuple[set[str], int, bool]] = {
    "sudo": ({"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U"}, 0, True),
    "env": ({"-C", "-u", "-S", "--chdir", "--unset"}, 0, False),
    "exec": ({"-a"}, 0, False),
    "command": (set(), 0, False),
    "time": (set(), 0, False),
    "nohup": (set(), 0, True),
    "nice": ({"-n"}, 0, True),
    "timeout": ({"-s", "-k", "--signal", "--kill-after"}, 1, True),
    "stdbuf": ({"-i", "-o", "-e"}, 0, True),
    "xargs": ({"-I", "-n", "-P", "-d", "-L", "-s", "-E", "-a"}, 0, True),
    "setsid": (set(), 0, True),
    "flock": ({"-w", "-E", "-c"}, 1, True),
    "ci_host_lock.sh": (set(), 1, False),
}
RECIPE_FLAGS_WITH_VALUE = {"-f", "-C", "--justfile", "--working-directory", "-j", "--jobs"}
WORD_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.+-]*$")
APT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9.+-]+$")
ASSIGNMENT_RE = re.compile(r"^[A-Za-z_]\w*(\[[^]]*\])?\+?=.*", re.S)
# `2>x`, `{fd}>x`: shlex splits the fd off, but it belongs to the redirect after it.
FD_PREFIX_RE = re.compile(r"\d+|\{[A-Za-z_]\w*\}")
PY_MODULE_RE = re.compile(r"^(scripts|ops|apps)(\.[A-Za-z_]\w*)+$")
GH_EXPR_RE = re.compile(r"\$\{\{.*?\}\}", re.S)
JUST_INTERP_RE = re.compile(r"\{\{.*?\}\}", re.S)
SUBPROCESS_FUNCS = {"run", "call", "check_call", "check_output", "Popen", "create_subprocess_exec"}


# ----- result model --------------------------------------------------------------


@dataclass
class Usage:
    """Every external name CI reaches, keyed by name, with where it was seen."""

    executables: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    apt_packages: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    playwright_browsers: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    scanned_files: set[str] = field(default_factory=set)


@dataclass
class _Ctx:
    usage: Usage
    shell_queue: list[Path] = field(default_factory=list)
    python_queue: list[Path] = field(default_factory=list)
    recipe_queue: list[tuple[str, str]] = field(default_factory=list)
    recipes: dict[str, dict[str, sources.Recipe]] = field(default_factory=dict)
    seen_recipes: set[str] = field(default_factory=set)
    cwd: str = ""  # the workflow step's working directory, while its text is walked


def _rel(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


# ----- shell: the command-position walker -------------------------------------------


def scan_shell(
    text: str, source: str, base_line: int, ctx: _Ctx, functions: frozenset[str] = frozenset()
) -> None:
    """Record every executable at command position in `text`."""
    clean, heredocs = lex.strip_comments_and_heredocs(GH_EXPR_RE.sub(lex.PLACEHOLDER, text))
    for doc in heredocs:
        if re.search(r"\bpython3?\b", doc.feeder):
            scan_python_source(doc.body, source, base_line + doc.first_line, ctx)
        elif re.search(r"\b(ba)?sh\b", doc.feeder.split("<<")[0]):
            scan_shell(doc.body, source, base_line + doc.first_line, ctx)
    subs = lex.command_substitutions(clean)
    toks = lex.tokens(lex.mask_substitutions(clean, subs))
    functions = functions | _defined_functions(toks)
    for start, _end, body in subs:
        scan_shell(body, source, base_line + clean.count("\n", 0, start), ctx, functions)
    _Walker(toks, source, base_line, ctx, functions).walk()


def _defined_functions(toks: list[tuple[str, int]]) -> frozenset[str]:
    names = {toks[k][0] for k in range(len(toks) - 1) if toks[k + 1][0] in {"()", "(){"}}
    names |= {toks[k + 1][0] for k in range(len(toks) - 1) if toks[k][0] == "function"}
    return frozenset(names)


@dataclass
class _Walker:
    """Walks tokens, tracking whether the next word is in command position."""

    toks: list[tuple[str, int]]
    source: str
    base_line: int
    ctx: _Ctx
    functions: frozenset[str]
    k: int = 0
    at_command: bool = True
    skip_until: str | None = None
    case_depth: int = 0
    case_pattern: bool = False

    def walk(self) -> None:
        while self.k < len(self.toks):
            tok = self.toks[self.k][0]
            _note_repo_path(tok, self.source, self.ctx)
            if not (self._skipping(tok) or self._structure(tok) or self._operator(tok)):
                self._word(tok)

    def _where(self) -> str:
        return f"{self.source}:{self.base_line + self.toks[min(self.k, len(self.toks) - 1)][1] + 1}"

    def _skipping(self, tok: str) -> bool:
        """Inside `[[ ]]`, `(( ))`, a for-list or a case pattern: nothing runs."""
        if self.skip_until:
            if tok == self.skip_until or (self.skip_until == "))" and tok.startswith("))")):
                self.case_pattern = self.skip_until == "in" and self.case_depth > 0
                self.at_command = self.skip_until not in {"]]", "]", "))", "in"}
                self.skip_until = None
        elif self.case_pattern:
            self.case_pattern = not (tok.startswith(")") or tok == "esac")
            self.at_command = tok.startswith(")")
            self.case_depth -= tok == "esac"
        else:
            return False
        self.k += 1
        return True

    def _structure(self, tok: str) -> bool:
        """Compound-command openers and case terminators."""
        opener = {"[[": "]]", "[": "]", "for": "do", "select": "do", "case": "in"}.get(tok)
        if opener and self.at_command:
            self.skip_until = opener
            self.case_depth += tok == "case"
        elif tok.startswith("((") and set(tok) <= lex.PUNCT:
            self.skip_until = "))"
        elif tok.startswith(";;") or tok == ";&":
            self.case_pattern = self.case_depth > 0
        elif tok == "esac":
            self.case_depth -= 1
        else:
            return False
        self.k += 1
        return True

    def _operator(self, tok: str) -> bool:
        if self._is_fd_prefix(tok):
            self.k += 1  # command position is whatever it was before the redirect
        elif lex.is_redirect(tok):
            self.k += 2  # the next token is the redirect target, never a command
        elif tok == "coproc":  # `coproc [NAME] command`: a NAME only precedes a compound command
            named = self.k + 2 < len(self.toks) and self.toks[self.k + 2][0] in COMPOUND_OPENERS
            self.at_command = True
            self.k += 2 if named else 1
        elif lex.is_separator(tok) or tok in KEYWORDS_BEFORE_COMMAND or tok in {"{", "}"}:
            self.at_command = True
            self.k += 1
        elif not self.at_command or ASSIGNMENT_RE.match(tok):
            self.k += 1
        else:
            return False
        return True

    def _is_fd_prefix(self, tok: str) -> bool:
        following = self.toks[self.k + 1][0] if self.k + 1 < len(self.toks) else ""
        return bool(FD_PREFIX_RE.fullmatch(tok)) and lex.is_redirect(following)

    def _word(self, tok: str) -> None:
        """A word in command position.

        Wrapper and argument handling key on the basename, so `.venv/bin/python
        -m scripts.x` queues scripts.x like `python -m scripts.x` does. Only a
        bare name (a PATH lookup) or an absolute path (a host file) is recorded
        as an executable the runner provides; a relative or variable-prefixed
        path runs a workspace file, such as a venv's `.venv/bin/pytest`.
        """
        self.at_command = False
        base = tok.rsplit("/", 1)[-1]
        where = self._where()
        repo_script = sources.repo_script_for(tok, self.source, self.ctx.cwd, REPO_ROOT)
        if base in WRAPPERS:
            if WRAPPERS[base][2]:
                self.ctx.usage.executables[base].add(where)
            self._skip_wrapper(base)
        elif tok in KEYWORDS_IGNORED or tok in self.functions or tok == lex.PLACEHOLDER:
            self.k += 1
        elif repo_script:
            _queue_repo_file(repo_script, self.ctx)
            self.k = _scan_arguments(base, self.toks, self.k + 1, where, self.ctx)
            self._after_reaper(base)
        elif tok in lex.FIND_ACTIONS:  # find's next action, after an escaped `\;` separator
            self.k = _scan_arguments("find", self.toks, self.k, where, self.ctx)
        elif WORD_RE.match(base) and not base.isdigit():
            if "/" not in tok or tok.startswith("/"):
                self.ctx.usage.executables[base].add(where)
            self.k = _scan_arguments(base, self.toks, self.k + 1, where, self.ctx)
        else:
            self.k += 1

    def _skip_wrapper(self, name: str) -> None:
        flags_with_value, positional, _ = WRAPPERS[name]
        self.k += 1
        while self.k < len(self.toks):
            tok = self.toks[self.k][0]
            if tok == "--":
                self.k += 1
                break
            if not (tok.startswith("-") or (name == "env" and "=" in tok)):
                break
            self.k += 2 if tok in flags_with_value else 1
        self.k += positional
        self.at_command = True

    def _after_reaper(self, base: str) -> None:
        """`ci_reap_port_holders.sh PORTS -- CMD`: CMD is a command."""
        if base == "ci_reap_port_holders.sh" and self.k < len(self.toks):
            self.at_command = self.toks[self.k][0] == "--"
            self.k += self.at_command


# ----- shell: arguments that carry nested programs -------------------------------------


def _scan_arguments(name: str, toks: list[tuple[str, int]], k: int, where: str, ctx: _Ctx) -> int:
    """Hand the arguments of `name` to its handler; return where they end.

    For the reaper, stops at its `--` so the wrapped command is walked.
    """
    end = k
    while end < len(toks) and not lex.is_separator(toks[end][0]) and toks[end][0] != "--":
        end += 1
    args = [tok for tok, _ in toks[k:end]]
    _note_argument_paths(args, where.rsplit(":", 1)[0], ctx)
    handler = ARGUMENT_HANDLERS.get(name)
    if handler:
        handler(name, args, where, ctx)
    if name in lex.LAUNCHERS:
        source, line = where.rsplit(":", 1)
        for argv in lex.launched_commands(name, [tok for tok, _ in toks[k:]]):
            scan_shell(shlex.join(argv), source, int(line) - 1, ctx)
    if name == "ci_reap_port_holders.sh" or end >= len(toks) or toks[end][0] != "--":
        return end
    return _scan_arguments(name, toks, end + 1, where, ctx)


def _args_inline_program(name: str, args: list[str], where: str, ctx: _Ctx) -> None:
    """`bash -c '...'` and `python -c '...'` carry a program in one argument."""
    if "-c" not in args or args.index("-c") + 1 >= len(args):
        return
    program, (source, line) = args[args.index("-c") + 1], where.rsplit(":", 1)
    scan = scan_shell if name in {"bash", "sh"} else scan_python_source
    scan(program, source, int(line) - 1, ctx)


def _args_recipes(name: str, args: list[str], _where: str, ctx: _Ctx) -> None:
    """`just RECIPE ...` runs one recipe; `make T1 T2` runs every target named."""
    idx = 0
    while idx < len(args):
        arg = args[idx]
        idx += 2 if arg in RECIPE_FLAGS_WITH_VALUE else 1
        if arg.startswith("-") or "=" in arg or not WORD_RE.match(arg):
            continue
        ctx.recipe_queue.append((name, arg))
        if name == "just":
            return


def _args_executables(_name: str, args: list[str], where: str, ctx: _Ctx) -> None:
    """scripts/ci_runner_preflight.sh EXE...: each argument is a required executable."""
    for exe in filter(WORD_RE.match, args):
        ctx.usage.executables[exe].add(where)


def _args_apt(name: str, args: list[str], where: str, ctx: _Ctx) -> None:
    if name != "ci_apt_present.sh" and "install" not in args:
        return
    pkgs = args[args.index("install") + 1 :] if "install" in args else args
    for pkg in pkgs:
        if not pkg.startswith("-") and APT_NAME_RE.match(pkg):
            ctx.usage.apt_packages[pkg].add(where)


def _args_playwright(_name: str, args: list[str], where: str, ctx: _Ctx) -> None:
    if "playwright" not in args or "install" not in args:
        return
    for browser in args[args.index("install") + 1 :]:
        if not browser.startswith("-"):
            ctx.usage.playwright_browsers[browser].add(where)


ARGUMENT_HANDLERS: dict[str, Callable[[str, list[str], str, _Ctx], None]] = {
    "bash": _args_inline_program,
    "sh": _args_inline_program,
    "python": _args_inline_program,
    "python3": _args_inline_program,
    "just": _args_recipes,
    "make": _args_recipes,
    "ci_runner_preflight.sh": _args_executables,
    "ci_apt_present.sh": _args_apt,
    "apt-get": _args_apt,
    "apt": _args_apt,
    "pnpm": _args_playwright,
    "npx": _args_playwright,
}


def _note_argument_paths(args: list[str], source: str, ctx: _Ctx) -> None:
    """Queue repo scripts an argument names (`bash scripts/x.sh`, `python -m scripts.x`)."""
    for idx, arg in enumerate(args):
        _note_repo_path(arg, source, ctx)
        if arg == "-m" and idx + 1 < len(args) and PY_MODULE_RE.match(args[idx + 1]):
            _queue_python_module(args[idx + 1], ctx)


def _queue_python_module(module: str, ctx: _Ctx, entry: str = "__main__.py") -> None:
    """Queue the file `module` names: `x.py`, else the package's `entry` file."""
    base = REPO_ROOT / Path(*module.split("."))
    ctx.python_queue += [p for p in (base.with_suffix(".py"), base / entry) if p.is_file()][:1]


def _queue_python_imports(text: str, path: Path, ctx: _Ctx) -> None:
    """Queue the repo-local modules (scripts.*, ops.*, apps.*) a CI Python file imports,
    so a subprocess call moved into a helper is still scanned."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return
    package = path.parent.relative_to(REPO_ROOT).parts
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            parent = package[: len(package) - node.level + 1] if node.level else ()
            base = ".".join([*parent, *([node.module] if node.module else [])])
            modules = [base, *(f"{base}.{alias.name}" for alias in node.names)]
        else:
            continue
        for module in filter(PY_MODULE_RE.match, modules):
            _queue_python_module(module, ctx, entry="__init__.py")


def _note_repo_path(tok: str, source: str, ctx: _Ctx) -> None:
    """Queue the repo script a token names, resolved exactly as a command token is."""
    rel = sources.repo_script_for(tok, source, ctx.cwd, REPO_ROOT)
    if rel:
        _queue_repo_file(rel, ctx)


def _queue_repo_file(rel: str, ctx: _Ctx) -> None:
    path = REPO_ROOT / rel
    if not path.is_file():
        return
    if path.suffix == ".py":
        ctx.python_queue.append(path)
    elif path.suffix in {".sh", ""} and path.read_bytes()[:2] == b"#!":
        ctx.shell_queue.append(path)


# ----- python ------------------------------------------------------------------------


def _const_str(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _call_target(node: ast.Call) -> tuple[str, str]:
    """(owner, attribute) of a call: `subprocess.run` -> ("subprocess", "run")."""
    func = node.func
    if isinstance(func, ast.Attribute):
        return (func.value.id if isinstance(func.value, ast.Name) else "?"), func.attr
    return "", func.id if isinstance(func, ast.Name) else ""


def _argv_as_shell(node: ast.Call) -> str | None:
    """A literal argv (or a shell=True string) rendered as one shell command."""
    first = node.args[0]
    if isinstance(first, (ast.List, ast.Tuple)) and first.elts and _const_str(first.elts[0]):
        return shlex.join([_const_str(e) or lex.PLACEHOLDER for e in first.elts])
    if _const_str(first) and any(kw.arg == "shell" for kw in node.keywords):
        return _const_str(first)
    if _call_target(node)[1] == "create_subprocess_exec":
        return _const_str(first)
    return None


def scan_python_source(text: str, source: str, base_line: int, ctx: _Ctx) -> None:
    """Record argv[0] literals of subprocess calls and shutil.which() names."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        owner, attr = _call_target(node)
        name = _const_str(node.args[0])
        if attr == "which" and owner in {"shutil", ""} and name and WORD_RE.match(name):
            ctx.usage.executables[name].add(f"{source}:{base_line + node.lineno}")
        elif attr in SUBPROCESS_FUNCS and owner in {"subprocess", "sp", "asyncio", ""}:
            shell = _argv_as_shell(node)
            if shell:
                scan_shell(shell, source, base_line + node.lineno - 1, ctx)


# ----- workflows and recipes ------------------------------------------------------------


def workflow_files(root: Path = REPO_ROOT) -> list[Path]:
    """Every workflow GitHub runs: `.github/workflows/*.yml` and `*.yaml`, nothing else."""
    workflows = root / WORKFLOW_DIR.relative_to(REPO_ROOT)
    return sorted([*workflows.glob("*.yml"), *workflows.glob("*.yaml")])


def scan_workflows(ctx: _Ctx, root: Path = REPO_ROOT) -> None:
    for path in workflow_files(root):
        rel = str(path.relative_to(root))
        for step, ctx.cwd in sources.self_hosted_steps(path):
            ctx.usage.scanned_files.add(rel)
            run, shell = step.get("run"), sources.yaml_scalar(step.get("shell"))
            if not isinstance(run, yaml.ScalarNode) or shell in {"pwsh", "powershell", "cmd"}:
                continue
            first_line = run.start_mark.line + (1 if run.style in {"|", ">"} else 0)
            scan = scan_python_source if shell.startswith("python") else scan_shell
            scan(run.value, rel, first_line, ctx)
    ctx.cwd = ""


def _scan_recipe(runner: str, name: str, ctx: _Ctx) -> None:
    path = RECIPE_FILES[runner]
    recipes = ctx.recipes.setdefault(runner, sources.parse_recipes(path))
    if f"{runner}:{name}" in ctx.seen_recipes or name not in recipes:
        return
    ctx.seen_recipes.add(f"{runner}:{name}")
    recipe = recipes[name]
    ctx.recipe_queue.extend((runner, dep) for dep in recipe.deps)
    ctx.usage.scanned_files.add(f"{path.name}[{name}]")
    body = [JUST_INTERP_RE.sub(lex.PLACEHOLDER, line) for line in recipe.body]
    shebang = body[0].strip() if body and body[0].strip().startswith("#!") else ""
    if "python" in shebang:
        indent = len(body[0]) - len(body[0].lstrip())
        text = "\n".join(line[indent:] for line in body)
        scan_python_source(text, path.name, recipe.header_line + 1, ctx)
    else:
        scan_shell(sources.recipe_shell(body, shebang), path.name, recipe.header_line + 1, ctx)


# ----- entry points ------------------------------------------------------------------


def _drain_queues(ctx: _Ctx) -> None:
    seen: set[Path] = set()
    while ctx.shell_queue or ctx.python_queue or ctx.recipe_queue:
        if ctx.recipe_queue:
            _scan_recipe(*ctx.recipe_queue.pop(), ctx)
            continue
        is_shell = bool(ctx.shell_queue)
        path = (ctx.shell_queue or ctx.python_queue).pop()
        if path in seen:
            continue
        seen.add(path)
        ctx.usage.scanned_files.add(_rel(path))
        text = path.read_text(encoding="utf-8", errors="replace")
        python = not is_shell or text.startswith("#!/usr/bin/env python")
        (scan_python_source if python else scan_shell)(text, _rel(path), 0, ctx)
        if python:
            _queue_python_imports(text, path, ctx)


def scan_repo() -> Usage:
    """Scan every CI surface and return the names it reaches."""
    ctx = _Ctx(usage=Usage())
    scan_workflows(ctx)
    _drain_queues(ctx)
    for path in sorted(TESTS_DIR.rglob("*.py")):
        ctx.usage.scanned_files.add(_rel(path))
        scan_python_source(path.read_text(encoding="utf-8", errors="replace"), _rel(path), 0, ctx)
    return ctx.usage


def load_manifest(path: Path = MANIFEST_PATH) -> dict:
    """Parse the toolset manifest, failing loud on a malformed one."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise TypeError(f"{path}: expected a mapping with an `entries` list")
    names: set[str] = set()
    for entry in data["entries"]:
        missing = MANIFEST_REQUIRED - set(entry)
        if missing:
            raise ValueError(f"{path}: entry {entry.get('name', '?')!r} lacks {sorted(missing)}")
        if entry["name"] in names:
            raise ValueError(f"{path}: duplicate entry {entry['name']!r}")
        names.add(entry["name"])
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()
    usage = scan_repo()
    kinds = ("executables", "apt_packages", "playwright_browsers")
    report = {
        kind: {name: sorted(srcs)[0] for name, srcs in sorted(getattr(usage, kind).items())}
        for kind in kinds
    }
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    for kind, names in report.items():
        print(f"## {kind} ({len(names)})")
        for name, src in names.items():
            print(f"  {name:28} {src}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
