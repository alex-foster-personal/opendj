"""Capabilities descriptor snapshot test (OPEN-02c)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.adapters.serato import capabilities
from apps.open_dj import serialize_jcs

SNAPSHOT_PATH = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "serato_capabilities.snapshot.json"
)

pytestmark = pytest.mark.requirement("OPEN-02")


@pytest.mark.requirement("OPEN-02c")
def test_capabilities_snapshot_matches() -> None:
    """Serialise capabilities via JCS and compare byte-for-byte."""
    assert SNAPSHOT_PATH.exists(), (
        f"missing snapshot at {SNAPSHOT_PATH}. "
        "Regenerate with: python -m scripts.update_serato_capabilities_snapshot"
    )
    actual = serialize_jcs(capabilities().to_dict())
    expected = SNAPSHOT_PATH.read_bytes()
    # Expected file is human-readable pretty-printed JSON; compare after
    # normalising both through json.loads for a deep-equality check AND keep
    # the byte-level comparison via JCS serialisation.
    assert json.loads(actual) == json.loads(expected), (
        "Serato capabilities descriptor drifted from snapshot. "
        "If this is intentional, regenerate the snapshot."
    )
