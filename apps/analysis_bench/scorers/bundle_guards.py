"""Refuse broken analysis-bench bundle joins before scoring."""
from __future__ import annotations

from pathlib import Path
from typing import Any


def refuse_malformed_join(
    bundle: Path, truth_name: str, truth: dict[str, Any], stable_ids: list[str]
) -> None:
    """Refuse a bundle whose manifest/truth join is broken."""
    duplicates = sorted({sid for sid in stable_ids if stable_ids.count(sid) > 1})
    if duplicates:
        raise ValueError(
            f"{bundle}/manifest.json lists {len(duplicates)} duplicate fixture "
            f"stable_id(s), first {duplicates[0]!r}: a malformed join must not "
            "silently collapse into a smaller scored set."
        )
    missing = sorted(sid for sid in stable_ids if sid not in truth)
    if missing:
        raise ValueError(
            f"{bundle}/{truth_name} has no truth row at all for {len(missing)} "
            f"fixture(s) the manifest declares, first {missing[0]!r}: a broken "
            "join must not silently become ordinary missing references."
        )


def refuse_unknown(results: dict[str, Any], stable_ids: list[str]) -> None:
    unknown = sorted(set(results) - set(stable_ids))
    if unknown:
        raise ValueError(
            f"this arm answered {len(unknown)} fixture(s) the bundle does not contain, "
            f"first {unknown[0]!r}. It was run against a different fixture set; "
            "scoring it here would attribute one bundle's numbers to another."
        )


__all__ = ["refuse_malformed_join", "refuse_unknown"]
