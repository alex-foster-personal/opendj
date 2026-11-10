"""Mixxx capabilities snapshot test (DJIMPORT-10059).

[if] Mixxx capabilities are declared [then] the snapshot and fields stay truthful, [else stop].
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.adapters.mixxx.capabilities import capabilities
from apps.open_dj import serialize_jcs

SNAPSHOT_PATH = Path(__file__).resolve().parents[2] / "data" / "mixxx_capabilities.snapshot.json"

pytestmark = pytest.mark.requirement("DJIMPORT-10059")


def test_mixxx_capabilities_snapshot_matches() -> None:
    """[if] the Mixxx capability descriptor changes [then] the snapshot must be updated deliberately, [else stop]."""
    assert SNAPSHOT_PATH.exists(), f"missing snapshot at {SNAPSHOT_PATH}. Regenerate and commit."
    actual = serialize_jcs(capabilities().to_dict())
    expected = SNAPSHOT_PATH.read_bytes()
    assert json.loads(actual) == json.loads(expected), "Mixxx capabilities descriptor drifted from snapshot."


def test_mixxx_capabilities_fields_use_valid_support_levels() -> None:
    """[if] Mixxx capabilities are declared [then] every field names a valid support level and adapter mixxx, [else stop]."""
    cap = capabilities()
    assert cap.adapter == "mixxx"
    allowed = frozenset({"lossless", "lossy", "unsupported"})
    for field in cap.fields:
        assert field.supports in allowed, field.field
