"""Shared JSON field parsing helpers for on-disk lyrics artifacts."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def non_empty_string(entry: dict[str, Any], field: str, path: Path, *, entry_label: str) -> str:
    value = entry.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{entry_label} {path} has invalid {field}")
    return value


__all__ = ["non_empty_string"]
