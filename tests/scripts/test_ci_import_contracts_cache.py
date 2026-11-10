"""import-contracts caches its pinned lint-imports tool env (issue #10884)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import scripts.import_contracts_tool as ict
import scripts.quality_gate as qg

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
TOOL_REQS = REPO / "ops" / "quality" / "requirements.txt"
CACHE_STEP = "Keep the lint-imports tool env across jobs on this runner"
LINT_STEP = "Import contracts (lint-imports over .importlinter)"


def _import_contracts_job() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["import-contracts"]


def _lint_step_run() -> str:
    steps = _import_contracts_job()["steps"]
    return next(step["run"] for step in steps if step.get("name") == LINT_STEP)


def _expected_eval_arch_argv() -> list[str]:
    return [
        "uv",
        "run",
        "--no-project",
        "--quiet",
        "--with-requirements",
        str(qg.TOOL_REQS),
        "lint-imports",
        "--config",
        ".importlinter",
    ]


@pytest.mark.requirement("CIBUDGET-11114")
def test_import_contracts_job_matches_eval_arch_manifest_and_command() -> None:
    """[if] CI uses another manifest or lint-imports argv than _eval_arch [then] fail, [else stop]."""
    steps = _import_contracts_job()["steps"]
    cache_run = next(step["run"] for step in steps if step.get("name") == CACHE_STEP)
    assert "scripts.import_contracts_tool run" in _lint_step_run()
    assert "scripts.import_contracts_tool provision" in cache_run
    assert ict.TOOL_REQS == TOOL_REQS
    assert ict.eval_arch_lint_imports_argv() == _expected_eval_arch_argv()


@pytest.mark.requirement("CIBUDGET-11114")
def test_import_contracts_cache_step_runs_before_lint_imports() -> None:
    """[if] warm import-contracts still builds a fresh throwaway env each run [then] fail, [else stop]."""
    steps = _import_contracts_job()["steps"]
    names = [step.get("name") for step in steps]
    assert names.index(CACHE_STEP) < names.index(LINT_STEP)
    assert "--isolated" not in _lint_step_run()
    cache_run = next(step["run"] for step in steps if step.get("name") == CACHE_STEP)
    assert "mdt-import-contracts" in cache_run
    assert "MDT_IMPORT_CONTRACTS_VENV" in cache_run


@pytest.mark.requirement("CIBUDGET-11114")
def test_import_contracts_cache_identity_changes_when_requirements_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[if] ops/quality/requirements.txt changes but the cache key does not [then] fail, [else stop]."""
    first = tmp_path / "requirements-a.txt"
    second = tmp_path / "requirements-b.txt"
    first.write_text("import-linter==2.15\n", encoding="utf-8")
    second.write_text("import-linter==2.16\n", encoding="utf-8")
    monkeypatch.setattr(ict, "TOOL_REQS", first)
    before = ict.config_identity("3.11")
    monkeypatch.setattr(ict, "TOOL_REQS", second)
    after = ict.config_identity("3.11")
    assert before != after


@pytest.mark.requirement("CIBUDGET-11114")
def test_provision_replaces_stale_import_contracts_cache_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] a requirements bump reuses the previous import-contracts venv [then] fail, [else stop]."""
    req = tmp_path / "requirements.txt"
    req.write_text("markdown-it-py==4.2.0\n", encoding="utf-8")
    monkeypatch.setattr(ict, "TOOL_REQS", req)
    root = tmp_path / "venv-root"
    ict.provision_cache_root(root, python_minor="3.11")
    key1 = (root / "cache-key.txt").read_text(encoding="utf-8")
    stale = root / ".venv" / "STALE-MARKER"
    stale.write_text("old", encoding="utf-8")
    req.write_text("markdown-it-py==3.0.0\n", encoding="utf-8")
    ict.provision_cache_root(root, python_minor="3.11")
    key2 = (root / "cache-key.txt").read_text(encoding="utf-8")
    assert key1 != key2
    assert not stale.exists()
