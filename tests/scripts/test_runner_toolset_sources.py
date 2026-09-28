"""The runner toolset scan reads every workflow and recipe CI may reach.

A workflow or recipe the scan drops is one it never reads, so a tool it runs
could stay undeclared while the completeness test passes. For recipes, `just`
itself is the reference: its `--dump` names every recipe and its dependencies.

Regression lines:
  - if a justfile recipe with a default-valued parameter (`out="a:b"`, `N='2'`)
    is not parsed, or is parsed under another name or with other dependencies,
    then broken
  - if a `:=` assignment, a Makefile `X = http://h:1` variable or a quoted `:`
    inside a recipe body parses as a recipe header then broken
  - if a self-hosted workflow saved as `.yaml` is not scanned, or a `.yaml`
    elsewhere under `.github/` is scanned as a workflow, then broken
  - if a self-hosted step's working directory (its own, else its job's, else the
    workflow's `defaults.run`) is not the base its relative repo paths resolve
    from, or a root-relative path is still resolved from the root under one,
    or a `${{ }}` working directory is scanned from a guessed base, then broken
  - if a step's working directory outlives the workflow walk and re-bases the
    queued scripts drained after it then broken
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts import runner_toolset_scan as scan
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


WORKFLOW = """\
on: push
jobs:
  build:
    runs-on: ${{{{ vars.CI_RUNS_ON_LINUX }}}}
    steps:
      - run: {tool} --version
"""


def test_a_yaml_workflow_is_scanned_and_other_yaml_under_github_is_not(tmp_path: Path) -> None:
    """GitHub runs `.github/workflows/*.yml` and `*.yaml` alike; a `.yaml` file
    elsewhere under `.github/` (dependabot, a template) is not a workflow."""
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "build.yml").write_text(WORKFLOW.format(tool="unzip"), encoding="utf-8")
    (workflows / "lint.yaml").write_text(WORKFLOW.format(tool="zipinfo"), encoding="utf-8")
    (tmp_path / ".github" / "dependabot.yaml").write_text(
        WORKFLOW.format(tool="xmllint"), encoding="utf-8"
    )
    ctx = scan._Ctx(usage=scan.Usage())
    scan.scan_workflows(ctx, tmp_path)
    assert {"unzip", "zipinfo"} <= set(ctx.usage.executables), ctx.usage.executables
    assert "xmllint" not in ctx.usage.executables, ctx.usage.executables
    assert ctx.usage.scanned_files == {".github/workflows/build.yml", ".github/workflows/lint.yaml"}


WORKING_DIRS = """\
defaults:
  run:
    working-directory: wf-dir
jobs:
  job-default:
    runs-on: ${{ vars.CI_RUNS_ON_LINUX }}
    defaults:
      run:
        working-directory: job-dir
    steps:
      - run: a
      - working-directory: step-dir
        run: b
  workflow-default:
    runs-on: ${{ vars.CI_RUNS_ON_LINUX }}
    steps:
      - run: c
"""


def test_a_step_working_directory_falls_back_to_its_job_then_its_workflow(tmp_path: Path) -> None:
    """GitHub's precedence: the step's own, else the job's, else the workflow's default."""
    workflow = tmp_path / "w.yml"
    workflow.write_text(WORKING_DIRS, encoding="utf-8")
    steps = sources.self_hosted_steps(workflow)
    assert [(sources.yaml_scalar(s["run"]), d) for s, d in steps] == [
        ("a", "job-dir"),
        ("b", "step-dir"),
        ("c", "wf-dir"),
    ]


def test_an_expression_working_directory_fails_loud(tmp_path: Path) -> None:
    workflow = tmp_path / "w.yml"
    workflow.write_text(WORKING_DIRS.replace("step-dir", "${{ env.DIR }}"), encoding="utf-8")
    with pytest.raises(ValueError, match="working-directory"):
        sources.self_hosted_steps(workflow)


FRONTEND = "apps/webui/frontend"


@pytest.mark.parametrize(
    ("cwd", "tok", "queued"),
    [
        (FRONTEND, "scripts/check-bundle-size.sh", f"{FRONTEND}/scripts/check-bundle-size.sh"),
        ("", "scripts/ci_runner_preflight.sh", "scripts/ci_runner_preflight.sh"),
        (FRONTEND, "scripts/ci_runner_preflight.sh", None),
        (FRONTEND, "../../../scripts/ci_runner_preflight.sh", "scripts/ci_runner_preflight.sh"),
    ],
)
def test_a_repo_path_resolves_from_the_step_working_directory(
    cwd: str, tok: str, queued: str | None
) -> None:
    """Bash resolves `bash scripts/x.sh` from the working directory, so under one a
    root-relative path is a different (here absent) file: the overshoot control."""
    ctx = scan._Ctx(usage=scan.Usage(), cwd=cwd)
    scan.scan_shell(f"bash {tok}", "w.yml", 0, ctx)
    assert ctx.shell_queue == ([REPO_ROOT / queued] if queued else []), ctx.shell_queue


def test_a_working_directory_does_not_outlive_the_workflow_walk(tmp_path: Path) -> None:
    """Queued scripts are drained after every workflow is walked; if the last step's
    working directory stayed set, their root-relative paths would resolve under it."""
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "w.yml").write_text(WORKING_DIRS, encoding="utf-8")
    ctx = scan._Ctx(usage=scan.Usage())
    scan.scan_workflows(ctx, tmp_path)
    scan.scan_shell("bash scripts/ci_runner_preflight.sh", "q.sh", 0, ctx)
    assert ctx.shell_queue == [REPO_ROOT / "scripts/ci_runner_preflight.sh"], ctx.shell_queue
