"""Traktor capabilities snapshot test (OPEN-02d)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.adapters._shim import serialize_jcs
from apps.adapters.traktor import capabilities

SNAPSHOT_PATH = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "traktor_capabilities.snapshot.json"
)


@pytest.mark.requirement("OPEN-02d")
def test_traktor_capabilities_snapshot_matches() -> None:
    assert SNAPSHOT_PATH.exists(), (
        f"missing snapshot at {SNAPSHOT_PATH}. Regenerate and commit."
    )
    actual = serialize_jcs(capabilities().to_dict())
    expected = SNAPSHOT_PATH.read_bytes()
    assert json.loads(actual) == json.loads(expected), (
        "Traktor capabilities descriptor drifted from snapshot."
    )
