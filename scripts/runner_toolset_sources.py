"""Where the runner toolset scan reads its text from: self-hosted workflow steps
and justfile/Makefile recipes.

Split out of `scripts/runner_toolset_scan.py`, which walks the text these
return; this module parses structure only and runs nothing.

Requirements:
  ✔︎ ✅ R1 only steps of jobs whose `runs-on` reads a `CI_RUNS_ON_*` variable
    are returned.
    [if] a job on `ubuntu-latest` has its steps returned [then ⛔️]
  ✔︎ ✅ R2 a recipe's dependencies and body lines are returned with its header line.
    [if] a `set` / `export` / `alias` line parses as a recipe [then ⛔️]
    [if] a Makefile `$(PY)` is left unresolved when PY is defined [then ⛔️]
  Exercised end-to-end through `scan_repo()` by
  `tests/scripts/test_runner_toolset_complete.py`; no direct unit test yet.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from scripts import runner_toolset_shell_lex as lex

SELF_HOSTED_MARKER = "CI_RUNS_ON"


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


def self_hosted_steps(path: Path) -> list[dict[str, yaml.Node]]:
    """Steps of every job whose runs-on reads a CI_RUNS_ON_* variable."""
    root = yaml.compose(path.read_text(encoding="utf-8"))
    steps: list[dict[str, yaml.Node]] = []
    for job in yaml_mapping(yaml_mapping(root).get("jobs")).values():
        fields = yaml_mapping(job)
        if SELF_HOSTED_MARKER in yaml_scalar(fields.get("runs-on")):
            seq = fields.get("steps")
            steps += [
                yaml_mapping(s) for s in (seq.value if isinstance(seq, yaml.SequenceNode) else [])
            ]
    return steps


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
    header = re.compile(r"^@?([A-Za-z_][\w.-]*)([^:=]*?):(?!=)(.*)$")
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
