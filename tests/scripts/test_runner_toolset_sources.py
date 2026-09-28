"""scripts/runner_toolset_sources.py reads every recipe the scan may reach.

A recipe the parser drops is a recipe the completeness scan never reads, so a
tool it runs could stay undeclared while the test passes. `just` itself is the
reference: its `--dump` names every recipe and its dependencies.

Regression lines:
  - if a justfile recipe with a default-valued parameter (`out="a:b"`, `N='2'`)
    is not parsed, or is parsed under another name or with other dependencies,
    then broken
  - if a `:=` assignment, a Makefile `X = http://h:1` variable or a quoted `:`
    inside a recipe body parses as a recipe header then broken
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts import runner_toolset_sources as sources
from scripts.runner_toolset_scan import REPO_ROOT

JUSTFILE = REPO_ROOT / "justfile"


def _just_dump() -> dict[str, dict]:
    """Every recipe as `just` itself parses the repo justfile, or UNAVAILABLE."""
    try:
        proc = subprocess.run(
            ["just", "--justfile", str(JUSTFILE), "--dump", "--dump-format", "json"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except FileNotFoundError:
        pytest.skip("UNAVAILABLE: no `just` here to parse the justfile as the reference")
    assert proc.returncode == 0, f"`just --dump` failed: {proc.stderr}"
    return json.loads(proc.stdout)["recipes"]


def test_every_just_recipe_is_parsed_with_its_dependencies() -> None:
    """Parity with `just`: every recipe, including each one with a default-valued
    parameter, is parsed under its own name with the same dependencies."""
    reference = _just_dump()
    defaulted = sorted(
        name
        for name, recipe in reference.items()
        if any(param.get("default") is not None for param in recipe["parameters"])
    )
    assert defaulted, "no default-valued recipe in the justfile: the test checks nothing"
    parsed = sources.parse_recipes(JUSTFILE)
    missing = [name for name in defaulted if name not in parsed]
    assert not missing, f"{len(missing)} of {len(defaulted)} default-valued recipes dropped"
    assert sorted(parsed) == sorted(reference)
    deps = {
        name: [d["recipe"] for d in recipe["dependencies"]] for name, recipe in reference.items()
    }
    drift = {
        name: (parsed[name].deps, want) for name, want in deps.items() if parsed[name].deps != want
    }
    assert not drift, drift


JUSTFILE_SHAPES = """\
url := "http://h:1"
export TOKEN := "a:b"
build out="apps/x.json" base='http://127.0.0.1:8685' n="2": dep
    echo "c:d"
    url="http://e:f"
dep:
    true
"""

MAKEFILE_SHAPES = """\
URL = http://h:1
PY ?= "a:b"
test: dep
\tcurl http://h:1
dep:
\ttrue
"""


@pytest.mark.parametrize(
    ("filename", "text", "expected"),
    [
        ("justfile", JUSTFILE_SHAPES, {"build": ["dep"], "dep": []}),
        ("Makefile", MAKEFILE_SHAPES, {"test": ["dep"], "dep": []}),
    ],
)
def test_assignments_and_quoted_colons_are_not_recipe_headers(
    tmp_path: Path, filename: str, text: str, expected: dict[str, list[str]]
) -> None:
    """Overshoot control: accepting quoted defaults must not turn a `:=` or `=`
    assignment, or a body line with a quoted `:`, into a recipe."""
    path = tmp_path / filename
    path.write_text(text, encoding="utf-8")
    parsed = sources.parse_recipes(path)
    assert {name: recipe.deps for name, recipe in parsed.items()} == expected, parsed
    first = next(iter(expected))
    assert any(":" in line for line in parsed[first].body), parsed[first]
