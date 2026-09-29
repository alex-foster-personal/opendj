"""Where the runner toolset scan reads its text from: self-hosted workflow steps
and justfile/Makefile recipes.

Split out of `scripts/runner_toolset_scan.py`, which walks the text these
return; this module parses structure only and runs nothing.

Requirements:
  ✔︎ ✅ R1 only steps of jobs whose `runs-on` reads a `CI_RUNS_ON_*` variable
    are returned.
    [if] a job on `ubuntu-latest` has its steps returned [then ⛔️]
  ✔︎ ✅ R3 each step carries its working directory, and a relative repo path
    resolves from it.
    [if] `bash scripts/x.sh` under `working-directory: apps/w` is read from the
      repo root [then ⛔️]
    [if] a `${{ }}` working directory is scanned from a guessed base [then ⛔️]
  ✔︎ ✅ R2 a recipe's dependencies and body lines are returned with its header line.
    [if] a `set` / `export` / `alias` line parses as a recipe [then ⛔️]
    [if] a Makefile `$(PY)` is left unresolved when PY is defined [then ⛔️]
  Exercised end-to-end through `scan_repo()` by
  `tests/scripts/test_runner_toolset_complete.py`; no direct unit test yet.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from scripts import runner_toolset_shell_lex as lex

SELF_HOSTED_MARKER = "CI_RUNS_ON"
REPO_SCRIPT_RE = re.compile(r"^(?:\./)?((?:scripts|ops|tests|\.github)/[\w./-]+)$")
PATH_VARIABLE_PREFIX_RE = re.compile(r"^(?:\$\{?[A-Za-z_]\w*\}?|GHEXPR)/")
# A recipe header: name, parameters, `:` (never `:=`), dependencies. A parameter
# default is quoted, backticked or parenthesized (`out="a:b"`, `N='2'`), so a `:`
# inside quotes is not the header's colon, and a bare `=` (a Makefile `X = y`)
# never starts a default.
RECIPE_HEADER_RE = re.compile(
    r"^@?([A-Za-z_][\w.-]*)"
    r"((?:\"[^\"]*\"|'[^']*'|`[^`]*`|[^:\"'`=\n]|=(?=[\"'`(]))*?)"
    r":(?!=)(.*)$"
)


@dataclass
class Recipe:
    """One justfile or Makefile recipe: its dependencies, body lines, header line."""

    deps: list[str]
    body: list[str]
    header_line: int


def yaml_mapping(node: yaml.Node | None) -> dict[str, yaml.Node]:
    if not isinstance(node, yaml.MappingNode):
        return {}
    return {k.value: v for k, v in node.value if isinstance(k, yaml.ScalarNode)}


def yaml_scalar(node: yaml.Node | None) -> str:
    return node.value if isinstance(node, yaml.ScalarNode) else ""


def self_hosted_steps(path: Path) -> list[tuple[dict[str, yaml.Node], str]]:
    """(step, working directory) for every job whose runs-on reads CI_RUNS_ON_*.

    The directory is the step's `working-directory`, else its job's, else the
    workflow's `defaults.run` one, as GitHub resolves it; "" is the repo root.
    """
    root = yaml_mapping(yaml.compose(path.read_text(encoding="utf-8")))
    workflow_dir = _default_working_directory(root, path)
    steps: list[tuple[dict[str, yaml.Node], str]] = []
    for job in yaml_mapping(root.get("jobs")).values():
        fields = yaml_mapping(job)
        if SELF_HOSTED_MARKER in yaml_scalar(fields.get("runs-on")):
            job_dir = _default_working_directory(fields, path) or workflow_dir
            seq = fields.get("steps")
            for node in seq.value if isinstance(seq, yaml.SequenceNode) else []:
                step = yaml_mapping(node)
                steps.append((step, _working_directory(step, path) or job_dir))
    return steps


def _default_working_directory(fields: dict[str, yaml.Node], path: Path) -> str:
    return _working_directory(yaml_mapping(yaml_mapping(fields.get("defaults")).get("run")), path)


def _working_directory(fields: dict[str, yaml.Node], path: Path) -> str:
    """A literal `working-directory`; an expression cannot be resolved statically, so it
    fails loud rather than letting the scan read paths from a guessed base."""
    value = yaml_scalar(fields.get("working-directory"))
    if "${{" in value:
        raise ValueError(f"{path}: expression working-directory {value!r} cannot be scanned")
    return value


def repo_path(tok: str, cwd: str, root: Path) -> str | None:
    """The repo-root-relative path a token names. Bash resolves a relative path from
    the working directory `cwd`, so under one a token counts only when that file
    exists there; from the root, a scripts/ops/tests/.github prefix marks it."""
    if not cwd:
        match = REPO_SCRIPT_RE.match(tok)
        return match.group(1) if match else None
    if tok.startswith(("/", "$", "~", "-")):
        return None
    rel = posixpath.normpath(posixpath.join(cwd, tok))
    return rel if not rel.startswith("..") and (root / rel).is_file() else None


def resolve_make_variables(text: str) -> str:
    """`$(PY)` -> its value, `$(shell cmd)` -> `$(cmd)`, anything else -> placeholder."""
    variables = dict(re.findall(r"^([A-Za-z_]\w*)\s*[:?+]?=\s*(.*)$", text, re.M))
    text = re.sub(r"\$\(shell ([^)]*)\)", r"$(\1)", text.replace("$$", "\x00"))
    for _ in range(5):  # variables reference variables: $(PY) -> $(VENV)/bin/python
        text = re.sub(
            r"\$[({]([A-Za-z_]\w*)[)}]",
            lambda m: (variables.get(m.group(1)) or lex.PLACEHOLDER).split(" ")[0],
            text,
        )
    return text.replace("\x00", "$")


def parse_recipes(path: Path) -> dict[str, Recipe]:
    """justfile or Makefile recipe -> dependencies, body lines, header line."""
    text = path.read_text(encoding="utf-8")
    if path.name == "Makefile":
        text = resolve_make_variables(text)
    recipes: dict[str, Recipe] = {}
    header = RECIPE_HEADER_RE
    skip = ("set ", "export ", "import ", "alias ", "mod ", ".")
    current: str | None = None
    for n, line in enumerate(text.split("\n")):
        if (not line or line[:1].isspace()) and current is not None:
            recipes[current].body.append(line)
        elif line:
            match = header.match(line)
            current = match.group(1) if match and not line.startswith(skip) else None
            if match and current:
                deps = re.findall(r"\(?([A-Za-z_][\w.-]*)", match.group(3).split("#")[0])
                recipes[current] = Recipe(deps=deps, body=[], header_line=n)
    return recipes


def recipe_shell(body: list[str], shebang: str) -> str:
    """A recipe body as one shell text; a leading @ or - is recipe syntax, not shell."""
    lines: list[str] = []
    for line in body:
        continued = bool(lines) and lines[-1].endswith("\\")
        lines.append(line.strip() if continued or shebang else line.strip().lstrip("@-"))
    return "\n".join(lines)


def repo_script_for(tok: str, source: str, cwd: str, root: Path) -> str | None:
    """The repo path a command token runs, when it names a repo script:
    `scripts/x.sh`, `"${ROOT}/scripts/x.sh"`, or `"$HERE/x.sh"` beside its caller."""
    direct = repo_path(tok, cwd, root)
    if direct:
        return direct
    tail = PATH_VARIABLE_PREFIX_RE.sub("", tok, count=1)
    if tail == tok:
        return None
    for candidate in (tail, str(Path(source).parent / tail)):
        if REPO_SCRIPT_RE.match(candidate) and (root / candidate).is_file():
            return candidate
    return None
