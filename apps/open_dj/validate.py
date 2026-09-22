"""Validate open-dj documents against the JSON Schema."""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from jsonschema.validators import Draft202012Validator

from apps.open_dj.schema_loader import load_schema


def validate_document(
    doc: dict[str, Any],
    *,
    schema: dict[str, Any] | None = None,
) -> list[str]:
    """Return a list of human-readable error strings (empty = valid)."""
    validator = _validator_for(schema)
    errors = sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path))
    return [_format_error(err) for err in errors]


def _validator_for(schema: dict[str, Any] | None) -> Draft202012Validator:
    return Draft202012Validator(schema if schema is not None else load_schema())


def _format_error(err: Any) -> str:
    path = "/".join(str(p) for p in err.absolute_path) or "<root>"
    return f"{path}: {err.message}"


def iter_validation_errors(
    doc: dict[str, Any],
    *,
    schema: dict[str, Any] | None = None,
) -> Iterable[Any]:
    """Yield raw ``jsonschema.ValidationError`` objects for ``doc``."""
    return _validator_for(schema).iter_errors(doc)
