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
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"


# ----- helpers --------------------------------------------------------------


def _workflow(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


# ----- acceptance -----------------------------------------------------------


def test_dependency_jobs_provision_venvs_with_uv() -> None:
    """Dependency installs are targeted at a venv, never runner Python."""
    requirements_jobs = {
        "ci.yml": ("uv pip install --python .venv/bin/python -r requirements.txt modal",),
        "full-ci.yml": ("uv pip install --python .venv/bin/python -r requirements.txt modal",),
        "release-check.yml": (
            "uv pip install --python .venv/bin/python -r requirements.txt build maturin",
            "uv pip install --python .venv/bin/python -r requirements.txt pytest",
        ),
    }

    for workflow, install_commands in requirements_jobs.items():
        contents = _workflow(workflow)
        assert "uv venv --python 3.11 .venv" in contents
        for install_command in install_commands:
            assert install_command in contents

    forbidden_installs = ("pip install", "python -m pip install", ".venv/bin/pip install")
    for workflow_path in WORKFLOWS.glob("*.yml"):
        lines = workflow_path.read_text(encoding="utf-8").splitlines()
        assert not any(
            line.lstrip().startswith(forbidden_installs)
            for line in lines
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
    assert "uv venv --python 3.11 .venv" in workflow
    assert "uv pip install --python .venv/bin/python -r requirements-docs.txt" in workflow
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
        assert "uv venv --python 3.11 .venv" in workflow, name
        assert 'UV_NO_SYNC: "1"' in workflow, (
            f"{name}: the pytest job must set UV_NO_SYNC so a child `uv run` "
            "cannot resync `.venv` out from under the running suite"
        )


def test_audio_stack_marker_uses_real_guarded_imports() -> None:
    """A locatable but broken audio package must skip its marked tests."""
    conftest = (REPO_ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")

    assert "def _can_import" in conftest
    assert "importlib.util.find_spec(\"soundfile\")" not in conftest
    assert "importlib.util.find_spec(\"librosa\")" not in conftest
