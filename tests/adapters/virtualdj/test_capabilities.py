"""VirtualDJ capabilities snapshot test (OPEN-02, DJIMPORT-10056).

[if] capabilities() is stable [then] it matches the committed snapshot, [else stop].
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.adapters.virtualdj import capabilities
from apps.open_dj import serialize_jcs

SNAPSHOT_PATH = Path(__file__).resolve().parents[2] / "data" / "virtualdj_capabilities.snapshot.json"

pytestmark = [pytest.mark.requirement("OPEN-02"), pytest.mark.requirement("DJIMPORT-10056")]


@pytest.mark.requirement("OPEN-02d")
def test_virtualdj_capabilities_snapshot_matches() -> None:
    """[if] capabilities() is stable [then] it matches the committed snapshot, [else stop]."""
    assert SNAPSHOT_PATH.exists(), f"missing snapshot at {SNAPSHOT_PATH}. Regenerate and commit."
    actual = serialize_jcs(capabilities().to_dict())
    expected = SNAPSHOT_PATH.read_bytes()
    assert json.loads(actual) == json.loads(expected), "VirtualDJ capabilities descriptor drifted from snapshot."
