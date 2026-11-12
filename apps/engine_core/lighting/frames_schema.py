"""Validate composed lighting traces without I/O, normalization or input mutation.

Traces contain ``interval_ms`` and non-empty ``frames`` with ``t_ns`` and fixture
states (``id``, three RGB bytes, unit ``intensity``). Patch IDs are supplied by
the caller, never inferred from trace metadata. The inclusive 33 ms floor is
the declared-interval contract, not a measurement of websocket delivery rate.
"""

from __future__ import annotations

import math
from collections.abc import Collection
from copy import deepcopy
from typing import cast

from jsonschema import Draft202012Validator, TypeChecker, ValidationError, validators

MIN_INTERVAL_MS = 33

FRAMES_SCHEMA: dict[str, object] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["interval_ms", "frames"],
    "properties": {
        "interval_ms": {"type": "integer", "minimum": MIN_INTERVAL_MS},
        "frames": {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/frame"}},
    },
    "$defs": {
        "frame": {
            "type": "object",
            "required": ["t_ns", "fixtures"],
            "properties": {
                "t_ns": {"type": "integer", "minimum": 0},
                "patch_version": {"type": ["string", "number", "null"]},
                "fixtures": {"$ref": "#/$defs/fixtures"},
            },
        },
        "fixtures": {"type": "array", "items": {"$ref": "#/$defs/fixture"}},
        "fixture": {
            "type": "object",
            "required": ["id", "rgb", "intensity"],
            "properties": {
                "id": {"type": "string", "minLength": 1},
                "rgb": {
                    "type": "array",
                    "minItems": 3,
                    "maxItems": 3,
                    "items": {"type": "integer", "minimum": 0, "maximum": 255},
                },
                "intensity": {"type": "number", "minimum": 0, "maximum": 1},
                "pan_deg": {"type": ["number", "null"]},
                "tilt_deg": {"type": ["number", "null"]},
                "strobe_hz": {"type": "number", "minimum": 0},
            },
        },
    },
}


def schema_for_patch(required_fixture_ids: Collection[str]) -> dict[str, object]:
    """Return an independent schema requiring every patched ID in every frame."""
    schema = deepcopy(FRAMES_SCHEMA)
    definitions = cast(dict[str, object], schema["$defs"])
    fixtures = cast(dict[str, object], definitions["fixtures"])
    if required_fixture_ids:
        fixtures["allOf"] = [
            {"contains": {"type": "object", "required": ["id"], "properties": {"id": {"const": fixture_id}}}}
            for fixture_id in sorted(set(required_fixture_ids))
        ]
    return schema


def _is_finite_number(_checker: TypeChecker, value: object) -> bool:
    # Python JSON readers can accept NaN/Infinity even though JSON cannot.
    return (isinstance(value, int) and not isinstance(value, bool)) or (
        isinstance(value, float) and math.isfinite(value)
    )


_FiniteValidator = validators.extend(
    Draft202012Validator,
    type_checker=Draft202012Validator.TYPE_CHECKER.redefine("number", _is_finite_number),
)

_FAILURE_PRIORITY = (
    "lighting_frames_invalid",
    "lighting_frames_too_fast",
    "lighting_frames_missing_fixture",
    "lighting_frames_bad_channel",
)


def _error_code(error: ValidationError) -> str:
    path = tuple(error.absolute_path)
    code = "lighting_frames_invalid"
    if path == ("interval_ms",) and error.validator == "minimum":
        code = "lighting_frames_too_fast"
    elif error.validator == "contains":
        code = "lighting_frames_missing_fixture"
    elif "rgb" in path:
        code = "lighting_frames_bad_channel"
    elif error.validator == "required" and len(path) == 4 and path[2] == "fixtures":
        missing = set(error.validator_value) - error.instance.keys()
        if missing == {"rgb"}:
            code = "lighting_frames_bad_channel"
    return code


def validate(document: object, *, required_fixture_ids: Collection[str]) -> str:
    """Return ``ok`` or a stable failure code, prioritizing malformed structure.

    After structural failures, priority is interval, missing fixtures, then RGB.
    Use ``schema_for_patch`` for consumers needing the JSON Schema itself.
    """
    errors = {
        _error_code(error) for error in _FiniteValidator(schema_for_patch(required_fixture_ids)).iter_errors(document)
    }
    return next((code for code in _FAILURE_PRIORITY if code in errors), "ok")
