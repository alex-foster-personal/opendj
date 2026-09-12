"""Schema validation for ops/quality/rubric/v1.yaml."""

from __future__ import annotations

import copy
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest
import yaml

from scripts.quality_rubric_model import (
    DEFAULT_RUBRIC,
    DEFAULT_SCHEMA,
    load_rubric,
    validate_rubric_data,
)

REPO = Path(__file__).resolve().parents[2]


def test_v1_yaml_passes_schema() -> None:
    load_rubric(DEFAULT_RUBRIC)


def test_dimension_missing_evidence_fails_validation() -> None:
    data = yaml.safe_load(DEFAULT_RUBRIC.read_text(encoding="utf-8"))
    data["dimensions"][0] = copy.deepcopy(data["dimensions"][0])
    del data["dimensions"][0]["evidence"]
    with pytest.raises(jsonschema.ValidationError):
        validate_rubric_data(data, DEFAULT_SCHEMA)


def test_dimension_missing_probe_fails_validation() -> None:
    data = yaml.safe_load(DEFAULT_RUBRIC.read_text(encoding="utf-8"))
    data["dimensions"][0] = copy.deepcopy(data["dimensions"][0])
    del data["dimensions"][0]["probe"]
    with pytest.raises(jsonschema.ValidationError):
        validate_rubric_data(data, DEFAULT_SCHEMA)


def test_unknown_surface_id_fails_validation() -> None:
    data = yaml.safe_load(DEFAULT_RUBRIC.read_text(encoding="utf-8"))
    data["dimensions"][0] = copy.deepcopy(data["dimensions"][0])
    data["dimensions"][0]["surfaces"] = ["not_a_surface"]
    with pytest.raises(jsonschema.ValidationError):
        validate_rubric_data(data, DEFAULT_SCHEMA)


def test_validate_rubric_subprocess_exits_zero() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "scripts.quality_rubric", "validate-rubric"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
