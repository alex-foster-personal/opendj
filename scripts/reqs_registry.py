"""Shared requirement-id registry from reqs.json."""

from __future__ import annotations

import json
from pathlib import Path


def requirement_ids(repo_root: Path | None = None) -> frozenset[str]:
    """Every id in reqs.json v1, v1.1, v2 and v3 buckets (same rule as pytest_reqs_plugin)."""
    root = repo_root or Path(__file__).resolve().parents[1]
    path = root / "reqs.json"
    if not path.is_file():
        raise FileNotFoundError(f"reqs.json missing at {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"reqs.json unparseable at {path}: {exc}") from exc
    ids = {
        req["id"]
        for bucket in ("v1", "v1.1", "v2", "v3")
        for category in data.get(bucket, {}).values()
        for req in category.get("requirements", [])
    }
    if not ids:
        raise ValueError(f"reqs.json declares no requirement IDs at {path}")
    return frozenset(ids)
