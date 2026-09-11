"""Load and validate the scored code-quality rubric YAML."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema
import yaml

REPO = Path(__file__).resolve().parent.parent
DEFAULT_RUBRIC = REPO / "ops" / "quality" / "rubric" / "v1.yaml"
DEFAULT_SCHEMA = REPO / "ops" / "quality" / "rubric" / "schema.json"

SURFACE_IDS = ("spec", "python_cli", "vendor_adapter", "svelte_ui")


@dataclass(frozen=True)
class Deduction:
    finding: str
    points: int


@dataclass(frozen=True)
class Evidence:
    role_model_repo: str
    artefact: str
    full_marks: str


@dataclass(frozen=True)
class Dimension:
    id: str
    surfaces: tuple[str, ...]
    probe: str
    evidence: Evidence
    anchors: dict[str, str]
    deductions: tuple[Deduction, ...]


@dataclass(frozen=True)
class Rubric:
    version: int
    surfaces: dict[str, str]
    dimensions: tuple[Dimension, ...]
    path: Path

    def dimensions_for_surface(self, surface_id: str) -> tuple[Dimension, ...]:
        return tuple(d for d in self.dimensions if surface_id in d.surfaces)


def _load_schema(schema_path: Path) -> dict[str, Any]:
    return json.loads(schema_path.read_text(encoding="utf-8"))


def _normalize_rubric_data(data: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(data)
    dimensions: list[dict[str, Any]] = []
    for raw in data.get("dimensions", []):
        dim = dict(raw)
        if "anchors" in dim:
            dim["anchors"] = {str(k): str(v) for k, v in dim["anchors"].items()}
        dimensions.append(dim)
    normalized["dimensions"] = dimensions
    return normalized


def validate_rubric_data(data: dict[str, Any], schema_path: Path = DEFAULT_SCHEMA) -> None:
    schema = _load_schema(schema_path)
    jsonschema.validate(instance=_normalize_rubric_data(data), schema=schema)


def _parse_dimension(raw: dict[str, Any]) -> Dimension:
    evidence_raw = raw["evidence"]
    deductions = tuple(
        Deduction(finding=d["finding"], points=int(d["points"])) for d in raw["deductions"]
    )
    return Dimension(
        id=str(raw["id"]),
        surfaces=tuple(raw["surfaces"]),
        probe=str(raw["probe"]),
        evidence=Evidence(
            role_model_repo=str(evidence_raw["role_model_repo"]),
            artefact=str(evidence_raw["artefact"]),
            full_marks=str(evidence_raw["full_marks"]),
        ),
        anchors={str(k): str(v) for k, v in raw["anchors"].items()},
        deductions=deductions,
    )


def load_rubric(path: Path = DEFAULT_RUBRIC, *, validate: bool = True) -> Rubric:
    rubric_path = path.resolve()
    data = yaml.safe_load(rubric_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"rubric root must be a mapping: {rubric_path}")
    if validate:
        schema_path = rubric_path.parent / "schema.json"
        validate_rubric_data(data, schema_path)
    surfaces = {str(k): str(v["root"]) for k, v in data["surfaces"].items()}
    dimensions = tuple(_parse_dimension(d) for d in _normalize_rubric_data(data)["dimensions"])
    return Rubric(
        version=int(data["version"]),
        surfaces=surfaces,
        dimensions=dimensions,
        path=rubric_path,
    )
