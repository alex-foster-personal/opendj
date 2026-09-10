"""Shared fixtures for the NATIVE-12 guard suites.

The registry and its record documents are the same objects for both halves of
the guard (the runtime refusal and the static sweep), so they are built in one
place. Nothing here is a replacement implementation: a throwaway registry is a
real JSON file in a real shape, consumed through the production loader.
"""

from __future__ import annotations

import json
from pathlib import Path

from apps.mik import reference_guard


def corpus_path() -> str:
    """The corpus location as the registry states it, slash-joined."""
    return "/".join(reference_guard.corpus_segments())


def corpus_tail() -> tuple[str, str]:
    """The last two segments of the corpus path, from the registry that owns it."""
    first, second = reference_guard.corpus_segments()[-2:]
    return first, second


def write_registry(path: Path, decisions: list[dict[str, str]]) -> Path:
    """Write a throwaway registry, taking corpus_path from the real one."""
    path.write_text(
        json.dumps({"version": 1, "corpus_path": corpus_path(), "decisions": decisions}),
        encoding="utf-8",
    )
    return path


def write_record(tmp_path: Path, decision_id: str) -> Path:
    """Write the decision document an entry must be backed by."""
    record = tmp_path / "decision.md"
    record.write_text(f"Decision {decision_id} records why this import was approved.\n", "utf-8")
    return record


def registry_entry(module: str, decision_id: str, action: str = "import") -> dict[str, str]:
    """A registry entry naming the record document ``write_record`` wrote."""
    return {"module": module, "action": action, "decision": decision_id, "record": "decision.md"}
