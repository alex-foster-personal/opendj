"""CLI and determinism tests for scripts.quality_rubric."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests" / "quality" / "fixtures" / "open-dj-a4adf880"
BASELINES = REPO / "ops" / "quality" / "rubric" / "baselines" / "v1.json"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "scripts.quality_rubric", *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )


def _strip_volatile(report: dict) -> dict:
    cleaned = json.loads(json.dumps(report))
    cleaned.pop("scored_at", None)
    cleaned.pop("commit", None)
    return cleaned


def test_module_help_exits_zero() -> None:
    result = _run("--help")
    assert result.returncode == 0, result.stderr


def test_score_all_json_contains_four_surfaces() -> None:
    result = _run("score", "--all", "--json")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    surface_ids = {surface["id"] for surface in report["surfaces"]}
    assert surface_ids == {"spec", "python_cli", "vendor_adapter", "svelte_ui"}


def test_score_spec_is_deterministic_modulo_volatile_fields() -> None:
    first = _run("score", "--surface", "spec", "--json")
    second = _run("score", "--surface", "spec", "--json")
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert _strip_volatile(json.loads(first.stdout)) == _strip_volatile(json.loads(second.stdout))


def test_unknown_surface_exits_two() -> None:
    from scripts.quality_rubric import score_surface
    from scripts.quality_rubric_model import load_rubric

    rubric = load_rubric()
    with pytest.raises(ValueError, match="unknown surface"):
        score_surface(rubric, "not_a_surface")


def test_missing_root_directory_exits_two() -> None:
    result = _run("score", "--surface", "spec", "--root", "/no/such/path/for-rubric")
    assert result.returncode == 2, result.stderr


def test_baselines_file_is_well_formed() -> None:
    assert BASELINES.is_file(), "run score --all --json --out to generate baselines"
    report = json.loads(BASELINES.read_text(encoding="utf-8"))
    assert report["rubric_version"] == 1
    surface_ids = {surface["id"] for surface in report["surfaces"]}
    assert surface_ids == {"spec", "python_cli", "vendor_adapter", "svelte_ui"}
    rubric = __import__("scripts.quality_rubric_model", fromlist=["load_rubric"]).load_rubric()
    for surface in report["surfaces"]:
        expected = {d.id for d in rubric.dimensions_for_surface(surface["id"])}
        scored = {dimension["id"] for dimension in surface["dimensions"]}
        assert scored == expected
        for dimension in surface["dimensions"]:
            assert 0 <= dimension["score"] <= 5
