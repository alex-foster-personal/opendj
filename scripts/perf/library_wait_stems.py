"""Stems-side remaining work for PERFBATCH-01. Read-only over existing artifacts."""

from __future__ import annotations

from pathlib import Path


def bundle_ids(data_dir: Path) -> frozenset[str]:
    stems = data_dir / "state" / "stems"
    if not stems.is_dir():
        return frozenset()
    return frozenset(
        path.parent.name
        for path in stems.glob("*/manifest.json")
        if path.is_file()
    )


def remaining_present(present_ids: frozenset[str], data_dir: Path) -> frozenset[str]:
    return present_ids - bundle_ids(data_dir)
