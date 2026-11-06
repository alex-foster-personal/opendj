"""Shared JSON field parsing helpers for on-disk lyrics artifacts."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def non_empty_string(entry: dict[str, Any], field: str, path: Path, *, entry_label: str) -> str:
    value = entry.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{entry_label} {path} has invalid {field}")
    return value


def require_object(value: Any, path: Path, *, entry_label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{entry_label} {path} must be a JSON object")
    return value


def require_schema(entry: dict[str, Any], path: Path, schema: int, *, entry_label: str) -> None:
    if entry.get("schema") != schema:
        raise ValueError(f"{entry_label} {path} has unsupported schema")


def write_json_compact_atomically(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as temporary:
        json.dump(payload, temporary, ensure_ascii=False, separators=(",", ":"))
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    try:
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


__all__ = [
    "non_empty_string",
    "require_object",
    "require_schema",
    "write_json_compact_atomically",
]
