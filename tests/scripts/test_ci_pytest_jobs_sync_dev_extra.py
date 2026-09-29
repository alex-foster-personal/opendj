"""Every workflow job that runs pytest installs pytest: its `uv sync` carries the dev extra.

pytest lives only in the ``dev`` extra of pyproject.toml, and a bare ``uv sync`` is an EXACT
sync: it installs the base dependencies and uninstalls everything else, pytest included. The
``extended`` job in e2e.yml ran a bare ``uv sync`` and then ``.venv/bin/pytest``, so from
Mon 21 Sep to Mon 28 Sep 2026 the nightly failed 8 of 8 runs on agentbox with
``.venv/bin/pytest: No such file`` (PERF-UI-05 step) and ``No module named 'pytest'``
(library-wheel fixture). This pins the whole class, not that one line.

[if] a job runs pytest [and] any `uv sync` in that job lacks `--extra dev` or
`--all-extras` [then] broken, [else stop].

Regression lines:
  - if the e2e extended job's `uv sync` loses `--extra dev` then broken, naming e2e.yml
  - if a job with no pytest step and a bare `uv sync` is flagged then broken (overshoot)
  - if the scanner parses the real e2e extended job and finds no pytest step then broken
  - if the scanner reads a `uv sync` inside a quoted string as a command then broken
"""

from __future__ import annotations

import copy
import shlex
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.requirement("INFRA-03")

REPO = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = REPO / ".github" / "workflows"
E2E = WORKFLOW_DIR / "e2e.yml"
COMMAND_SEPARATORS = frozenset({";", "&&", "||", "|", "&", "(", ")"})

# -----------------------------------------------------------------------------
# Scanner
# -----------------------------------------------------------------------------


def _tokenize_line(line: str) -> list[str]:
    """Shell-ish tokens, quote-aware where the line parses on its own.

    A line inside a multi-line quoted string or heredoc may not parse alone; it is then split
    on whitespace, which errs toward MORE matches (a louder red), never fewer.
    """
    lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = "#"
    try:
        return list(lexer)
    except ValueError:
        return line.split()


def _commands(run: str) -> list[list[str]]:
    """Split a step's `run:` script into commands: one per line and per shell separator."""
    commands: list[list[str]] = []
    for line in run.replace("\\\n", " ").splitlines():
        current: list[str] = []
        for token in _tokenize_line(line):
            if token in COMMAND_SEPARATORS:
                commands.append(current)
                current = []
            else:
                current.append(token)
        commands.append(current)
    return [command for command in commands if command]


def _runs_pytest(command: list[str]) -> bool:
    """`pytest`, `.venv/bin/pytest`, `python -m pytest`, `uv run ... pytest`, `X=.../pytest`."""
    return any(token == "pytest" or token.endswith("/pytest") for token in command)


def _uv_sync_args(command: list[str]) -> list[str] | None:
    """The arguments after `uv sync` in this command, or None when it is not a uv sync."""
    for index in range(len(command) - 1):
        if command[index] == "uv" and command[index + 1] == "sync":
            return command[index + 2 :]
    return None


def _installs_dev_extra(sync_args: list[str]) -> bool:
    pairs = pairwise(sync_args)
    return (
        "--all-extras" in sync_args
        or "--extra=dev" in sync_args
        or any(flag == "--extra" and value == "dev" for flag, value in pairs)
    )


def _job_commands(job: dict[str, Any]) -> list[list[str]]:
    runs = [step["run"] for step in job.get("steps") or [] if step.get("run")]
    return [command for run in runs for command in _commands(run)]


def _violations(document: dict[str, Any], workflow_name: str) -> list[str]:
    """One line per `uv sync` that leaves pytest uninstalled in a job that runs pytest."""
    violations: list[str] = []
    for job_id, job in (document.get("jobs") or {}).items():
        commands = _job_commands(job)
        if not any(_runs_pytest(command) for command in commands):
            continue
        for command in commands:
            sync_args = _uv_sync_args(command)
            if sync_args is not None and not _installs_dev_extra(sync_args):
                sync = " ".join(["uv", "sync", *sync_args])
                violations.append(
                    f"{workflow_name} job {job_id!r}: runs pytest but `{sync}` lacks "
                    "--extra dev / --all-extras (an exact sync uninstalls pytest)"
                )
    return violations


def _load(workflow: Path) -> dict[str, Any]:
    return yaml.safe_load(workflow.read_text(encoding="utf-8"))


def _workflows() -> list[Path]:
    return sorted([*WORKFLOW_DIR.glob("*.yml"), *WORKFLOW_DIR.glob("*.yaml")])


def _extended_with_bare_sync() -> dict[str, Any]:
    """The real e2e.yml with the fix reverted: the extended job's `uv sync` made bare."""
    document = copy.deepcopy(_load(E2E))
    steps = document["jobs"]["extended"]["steps"]
    sync_steps = [
        step
        for step in steps
        if any(_uv_sync_args(c) is not None for c in _commands(step.get("run") or ""))
    ]
    assert len(sync_steps) == 1, f"expected one uv sync step in extended, got {len(sync_steps)}"
    sync_steps[0]["run"] = "uv sync"
    return document


# -----------------------------------------------------------------------------
# The guard
# -----------------------------------------------------------------------------


def test_every_job_that_runs_pytest_syncs_the_dev_extra() -> None:
    """[if] a pytest job's uv sync lacks the dev extra [then] pytest is uninstalled, [else stop]."""
    workflows = _workflows()
    assert len(workflows) >= 10, f"expected the repo's workflows, found {len(workflows)}"
    violations = [v for wf in workflows for v in _violations(_load(wf), wf.name)]
    assert not violations, "\n".join(violations)


# -----------------------------------------------------------------------------
# Controls: the scanner sees the real subject, fires when broken, and does not overshoot
# -----------------------------------------------------------------------------


def test_positive_control_finds_the_extended_jobs_pytest_step_and_uv_sync() -> None:
    """[if] the scanner finds no pytest step in the real extended job [then] it parses nothing."""
    commands = _job_commands(_load(E2E)["jobs"]["extended"])
    pytest_commands = [command for command in commands if _runs_pytest(command)]
    assert any(".venv/bin/pytest" in c for c in pytest_commands), "no pytest step found"
    syncs = [args for c in commands if (args := _uv_sync_args(c)) is not None]
    assert len(syncs) == 1, f"expected one uv sync in the extended job, got {syncs}"


def test_the_guard_goes_red_on_e2e_yml_with_the_fix_reverted() -> None:
    """[if] the extended job's bare uv sync goes unflagged [then] the guard is blind."""
    violations = _violations(_extended_with_bare_sync(), E2E.name)
    assert len(violations) == 1, violations
    assert "e2e.yml" in violations[0] and "'extended'" in violations[0], violations


def test_a_bare_uv_sync_in_a_job_without_pytest_stays_green() -> None:
    """[if] a job with no pytest is flagged for a bare sync [then] the guard overshoots."""
    unrelated = {"jobs": {"lint": {"steps": [{"run": "uv sync"}, {"run": "uv run ruff check ."}]}}}
    assert _violations(unrelated, "synthetic.yml") == []
    # Real negative control: e2e.yml's gate job syncs without dev and runs no pytest.
    real = _load(E2E)
    assert not any(_runs_pytest(c) for c in _job_commands(real["jobs"]["gate"]))
    assert _violations({"jobs": {"gate": real["jobs"]["gate"]}}, E2E.name) == []


@pytest.mark.parametrize(
    "run",
    [
        "pytest -q tests",
        "python -m pytest tests",
        ".venv/bin/pytest -q tests",
        "uv run --extra tags pytest tests",
        "uv run --no-sync pytest tests/quality -m slow",
        'PYTEST="$PWD/.venv/bin/pytest"',
    ],
)
def test_each_pytest_invocation_form_is_detected(run: str) -> None:
    """[if] a pytest form goes undetected [then] its job escapes the guard, [else stop]."""
    job = {"jobs": {"j": {"steps": [{"run": "uv sync --extra tags"}, {"run": run}]}}}
    assert len(_violations(job, "synthetic.yml")) == 1, run


@pytest.mark.parametrize(
    "sync",
    [
        "uv sync --extra dev",
        "uv sync --frozen --extra dev --python 3.11",
        "uv sync --extra tags --extra dev",
        "uv sync --extra=dev",
        "uv sync --all-extras",
        "uv sync \\\n  --extra dev",
    ],
)
def test_each_dev_installing_sync_form_is_accepted(sync: str) -> None:
    """[if] a sync that does install pytest is flagged [then] the guard overshoots, [else stop]."""
    job = {"jobs": {"j": {"steps": [{"run": sync}, {"run": ".venv/bin/pytest -q"}]}}}
    assert _violations(job, "synthetic.yml") == [], sync


def test_non_command_mentions_are_not_read_as_commands() -> None:
    """[if] quoted text, comments or a pytest-named path count as commands [then] false reds."""
    run = (
        'test -x .venv/bin/python || { echo "[ERROR] missing after uv sync"; exit 1; }\n'
        "# uv sync is run above; pytest lanes live in ci.yml\n"
        '--basetemp="$RUNNER_TEMP/pytest-basetemp-x"'
    )
    commands = _commands(run)
    assert not any(_runs_pytest(c) for c in commands), commands
    assert all(_uv_sync_args(c) is None for c in commands), commands


def test_args_do_not_leak_from_the_next_line_into_a_uv_sync() -> None:
    """[if] a later line's `--extra dev` satisfies an earlier bare sync [then] a miss."""
    run = "uv sync\nuv run --extra dev pytest tests"
    job = {"jobs": {"j": {"steps": [{"run": run}]}}}
    assert len(_violations(job, "synthetic.yml")) == 1
