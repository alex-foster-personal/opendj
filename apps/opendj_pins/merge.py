"""Forward-compatible JSON merge for pin documents."""

from __future__ import annotations

from typing import Any


def keep_unknown_fields(raw: dict[str, Any], known: dict[str, Any]) -> dict[str, Any]:
    merged = dict(raw)
    for key, value in known.items():
        prior = raw.get(key)
        if isinstance(value, dict) and isinstance(prior, dict):
            merged[key] = keep_unknown_fields(prior, value)
        else:
            merged[key] = value
    return merged
