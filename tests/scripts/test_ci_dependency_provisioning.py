"""Regression contracts for isolated Python dependency provisioning in CI.

Requirements:
- Every dependency-installing Linux CI job builds an isolated environment with
  uv instead of writing into the runner's system Python.
- The fast pytest lane, its OpenAPI contract-drift sentinel, and docs build run
  from their provisioned environments.
- A child `uv run` spawned by the suite must never re-sync the job venv.

Acceptance tests:
- [if] a runner exposes an externally managed system Python [then ⛔️] no
  workflow may install a dependency through bare pip.
- [if] FastAPI is absent from runner site-packages [then ⛔️] contract drift
  must invoke the project venv interpreter after uv provisioned it.
- [if] docs dependencies are absent from runner site-packages [then ⛔️] the
  strict MkDocs build must invoke the docs venv executable.
- [if] a test spawns `uv run --with modal` against the repo root [then ⛔️]
  the pytest jobs must carry UV_NO_SYNC so `.venv` is not pruned to uv.lock.
- [if] a pytest job stops syncing pylock.ci.toml [then ⛔️] it loses the
  `observability` extra and modal (requirements-ci.in; the extra's verbatim
  presence there is tests/scripts/test_ci_lock.py's job).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"


# ----- helpers --------------------------------------------------------------


def _workflow(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


# ----- acceptance -----------------------------------------------------------


def test_dependency_jobs_provision_venvs_with_uv() -> None:
    """Dependency installs are targeted at a venv, never runner Python."""
    # Self-hosted jobs reuse a persistent .venv through scripts/ci_venv.sh and
    # install it exactly (tests/scripts/test_ci_workspace_reuse.py); the
    # hosted macOS job starts clean and keeps the plain form.
    # Both halves live in one command there, so the second item is the command itself.
    ci_lock = "scripts/ci_venv.sh 3.11 --lock pylock.ci.toml"
    release_lock = "scripts/ci_venv.sh 3.11 --lock pylock.release-check.toml"
    requirements_jobs = {
        "ci.yml": (ci_lock, ci_lock),
        "full-ci.yml": (ci_lock, ci_lock),
        "release-check.yml": (release_lock, release_lock),
        # The macOS pytest job moved out of release-check.yml into its own
        # workflow (#924). The contract follows the job, not the file it used
        # to live in - otherwise relocating a job silently drops its cover.
        "macos-native-companion.yml": (
            "uv venv --python 3.11 .venv",
            "uv pip install --python .venv/bin/python -r requirements.txt pytest",
        ),
    }

    for workflow, (venv_command, install_command) in requirements_jobs.items():
        contents = _workflow(workflow)
        assert venv_command in contents, workflow
        assert install_command in contents, workflow

    forbidden_installs = ("pip install", "python -m pip install", ".venv/bin/pip install")
    # Row 14 duplicate-writer (DEVOPS-10, #1580) is a cheap hosted periodic
    # job with no persistent workspace: it installs PyYAML once before
    # `python -m scripts.duplicate_writer_check`. scripts/ci_duplicate_writer_check.sh
    # is the uv-provisioned replacement; until periodic-checks.yml adopts it,
    # this one line is the only bare-pip install left in the workflow tree.
    allowed_bare_pip = {
        "periodic-checks.yml": frozenset({"pip install --quiet pyyaml"}),
    }
    for workflow_path in WORKFLOWS.glob("*.yml"):
        allowed = allowed_bare_pip.get(workflow_path.name, frozenset())
        lines = workflow_path.read_text(encoding="utf-8").splitlines()
        offenders = [
            line.lstrip()
            for line in lines
            if line.lstrip().startswith(forbidden_installs)
            and line.lstrip() not in allowed
        ]
        assert not offenders, (
            f"{workflow_path.name}: bare pip installs are forbidden outside an "
            f"explicit allowlist: {offenders}"
        )


def test_fast_lane_contract_drift_runs_in_project_venv() -> None:
    """The FastAPI-importing OpenAPI sentinel cannot borrow runner packages."""
    workflow = _workflow("ci.yml")

    assert ".venv/bin/pytest" in workflow
    assert '.venv/bin/python -c "import json, sys, tempfile;' in workflow
    assert ".venv/bin/python -m scripts.build_reqs_json --check" in workflow
    assert ".venv/bin/python -m scripts.build_reqs_json\n" in workflow


def test_provisioned_environments_precede_privileged_system_dependencies() -> None:
    """Post-failure sentinels retain their interpreter after a host setup error."""
    workflow_steps = {
        "ci.yml": (
            "Provision isolated Python test environment",
            "System deps for pyrekordbox (SQLCipher) and Mach-O classification",
        ),
        "full-ci.yml": (
            "Provision isolated Python test environment",
            "System deps for pyrekordbox (SQLCipher)",
        ),
        "release-check.yml": (
            "Create project venv",
            "System deps for pyrekordbox (SQLCipher)",
        ),
    }

    for workflow_name, (provision_step, system_step) in workflow_steps.items():
        workflow = _workflow(workflow_name)
        assert workflow.index(provision_step) < workflow.index(system_step)


def test_docs_build_uses_uv_provisioned_venv() -> None:
    """PEP 668 cannot block docs dependency installation."""
    workflow = _workflow("docs.yml")

    assert "astral-sh/setup-uv" in workflow
    assert "scripts/ci_venv.sh 3.11 --lock pylock.docs.toml" in workflow
    assert "run: .venv/bin/mkdocs build --strict" in workflow


def test_pytest_jobs_forbid_child_uv_runs_from_resyncing_the_venv() -> None:
    """`uv run` syncs `.venv` to uv.lock unless told not to.

    apps/stems/job.py spawns `uv run --with modal ...` from the repo root and
    tests/stems/test_stems_job_pipeline.py drives it for real. Once #1106 made
    `.venv` the job environment, that sync pruned pytest, librosa and soundfile
    mid-suite (trunk run 33884587140, Fri 4 Sep 2026). UV_NO_SYNC on the job
    is what keeps the provisioned environment intact.
    """
    for name in ("ci.yml", "full-ci.yml"):
        workflow = _workflow(name)
        assert "scripts/ci_venv.sh 3.11" in workflow, name
        assert 'UV_NO_SYNC: "1"' in workflow, (
            f"{name}: the pytest job must set UV_NO_SYNC so a child `uv run` "
            "cannot resync `.venv` out from under the running suite"
        )


def test_pytest_installs_carry_the_observability_extra() -> None:
    """requirements.txt is the shipped payload and omits sentry-sdk on purpose.

    The Sentry end-to-end tests import sentry_sdk, so every pytest job syncs
    pylock.ci.toml, whose source adds the `observability` extra's pins
    verbatim (held there by tests/scripts/test_ci_lock.py).
    """
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extra = pyproject["project"]["optional-dependencies"]["observability"]
    source = (REPO_ROOT / "requirements-ci.in").read_text(encoding="utf-8").splitlines()
    assert extra and set(extra) <= set(source), f"requirements-ci.in lacks {extra}"
    sync = "scripts/ci_venv.sh 3.11 --lock pylock.ci.toml\n"

    ci_jobs = "ci.yml: test, fast tier, contracts, fixture server"
    assert _workflow("ci.yml").count(sync) == 4, ci_jobs
    assert _workflow("full-ci.yml").count(sync) == 1, "full-ci.yml: the full suite"


def test_audio_stack_marker_uses_real_guarded_imports() -> None:
    """A locatable but broken audio package must skip its marked tests."""
    conftest = (REPO_ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")

    assert "def _can_import" in conftest
    assert "importlib.util.find_spec(\"soundfile\")" not in conftest
    assert "importlib.util.find_spec(\"librosa\")" not in conftest
