"""Strict trace validation using an explicitly synthetic, immutable source fixture.

[if] a trace violates rate, patch or channel bounds [then] return its code, [else stop].
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from apps.engine_core.lighting.frames_schema import FRAMES_SCHEMA, MIN_INTERVAL_MS, schema_for_patch, validate
from apps.lighting.patch import Patch, PatchedFixture, StagePosition

pytestmark = pytest.mark.requirement("LIGHT-11800")

SOURCE = Path(__file__).parent / "fixtures" / "frames-ok.json"
PATCH = Patch(
    "schema-test",
    tuple(
        PatchedFixture(fixture_id, "test-rgb-par-3", "3ch", 1, address, (), StagePosition(0.5, 0.5))
        for fixture_id, address in (("par-left", 1), ("par-right", 4))
    ),
)
REQUIRED_IDS = tuple(fixture.id for fixture in PATCH.fixtures)


@pytest.fixture
def document() -> dict[str, Any]:
    return json.loads(SOURCE.read_text(encoding="utf-8"))


def test_ok_fixture_is_valid_without_mutation(document: dict[str, Any]) -> None:
    original = deepcopy(document)
    source_bytes = SOURCE.read_bytes()
    assert MIN_INTERVAL_MS == 33
    assert validate(document, required_fixture_ids=REQUIRED_IDS) == "ok"
    assert document == original
    assert SOURCE.read_bytes() == source_bytes


@pytest.mark.parametrize(
    "interval,expected", [(20, "lighting_frames_too_fast"), (32, "lighting_frames_too_fast"), (33, "ok"), (34, "ok")]
)
def test_interval_floor(document: dict[str, Any], interval: int, expected: str) -> None:
    document["interval_ms"] = interval
    assert validate(document, required_fixture_ids=REQUIRED_IDS) == expected


def test_every_frame_requires_the_external_patch(document: dict[str, Any]) -> None:
    document["frames"][1]["fixtures"].pop()
    document["patch"] = {"fixtures": [{"id": "par-left"}]}
    assert validate(document, required_fixture_ids=REQUIRED_IDS) == "lighting_frames_missing_fixture"


@pytest.mark.parametrize("channel", [-1, 256, True, 0.5, "255", None, float("nan"), float("inf")])
def test_invalid_channel(document: dict[str, Any], channel: object) -> None:
    document["frames"][1]["fixtures"][0]["rgb"][1] = channel
    assert validate(document, required_fixture_ids=REQUIRED_IDS) == "lighting_frames_bad_channel"


@pytest.mark.parametrize("rgb", [[], [0, 255], [0, 0, 0, 0], "#ffffff", None])
def test_rgb_shape(document: dict[str, Any], rgb: object) -> None:
    document["frames"][0]["fixtures"][0]["rgb"] = rgb
    assert validate(document, required_fixture_ids=REQUIRED_IDS) == "lighting_frames_bad_channel"


def test_missing_rgb(document: dict[str, Any]) -> None:
    del document["frames"][0]["fixtures"][0]["rgb"]
    assert validate(document, required_fixture_ids=REQUIRED_IDS) == "lighting_frames_bad_channel"


@pytest.mark.parametrize("channel", [0, 255])
def test_byte_endpoints(document: dict[str, Any], channel: int) -> None:
    document["frames"][0]["fixtures"][0]["rgb"] = [channel] * 3
    assert validate(document, required_fixture_ids=REQUIRED_IDS) == "ok"


@pytest.mark.parametrize(
    "malformed", [None, [], {}, {"interval_ms": 33, "frames": []}, {"interval_ms": 33, "frames": [{}]}]
)
def test_malformed_envelope(malformed: object) -> None:
    assert validate(malformed, required_fixture_ids=REQUIRED_IDS) == "lighting_frames_invalid"


@pytest.mark.parametrize("interval", [True, 33.5, "33", None, float("nan"), float("inf")])
def test_invalid_interval_type(document: dict[str, Any], interval: object) -> None:
    document["interval_ms"] = interval
    assert validate(document, required_fixture_ids=REQUIRED_IDS) == "lighting_frames_invalid"


@pytest.mark.parametrize("field", ["intensity", "pan_deg", "tilt_deg", "strobe_hz"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_numeric_fields_must_be_finite(document: dict[str, Any], field: str, value: float) -> None:
    document["frames"][0]["fixtures"][0][field] = value
    assert validate(document, required_fixture_ids=REQUIRED_IDS) == "lighting_frames_invalid"


def test_optional_fields_and_metadata(document: dict[str, Any]) -> None:
    document["producer"] = "synthetic-test"
    document["frames"][0]["fixtures"][0].update(pan_deg=None, tilt_deg=-45.5, strobe_hz=2)
    assert validate(document, required_fixture_ids=REQUIRED_IDS) == "ok"


def test_schema_itself_enforces_patch_membership(document: dict[str, Any]) -> None:
    schema = schema_for_patch(REQUIRED_IDS)
    Draft202012Validator.check_schema(FRAMES_SCHEMA)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    assert validator.is_valid(document)
    document["frames"][1]["fixtures"].pop()
    assert not validator.is_valid(document)


def test_schema_generation_is_isolated(document: dict[str, Any]) -> None:
    original = deepcopy(FRAMES_SCHEMA)
    missing_schema = schema_for_patch(("absent",))
    assert not Draft202012Validator(missing_schema).is_valid(document)
    assert Draft202012Validator(schema_for_patch(REQUIRED_IDS)).is_valid(document)
    assert Draft202012Validator(schema_for_patch(())).is_valid(document)
    assert original == FRAMES_SCHEMA


def test_failure_priority(document: dict[str, Any]) -> None:
    document["frames"][0]["fixtures"][0]["rgb"][0] = 256
    document["frames"][1]["fixtures"].pop()
    assert validate(document, required_fixture_ids=REQUIRED_IDS) == "lighting_frames_missing_fixture"
    document["interval_ms"] = 20
    assert validate(document, required_fixture_ids=REQUIRED_IDS) == "lighting_frames_too_fast"
    del document["frames"][0]["fixtures"]
    assert validate(document, required_fixture_ids=REQUIRED_IDS) == "lighting_frames_invalid"
