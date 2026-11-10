"""Unit tests for ``apps.rescue.validate`` index parsing."""

from __future__ import annotations

import pytest

from apps.rescue.contract import RING_SIZE, RescueSnapshotInvalidError
from apps.rescue.validate import parse_index, parse_slot_meta


def test_parse_slot_meta_rejects_out_of_range_slot() -> None:
    with pytest.raises(RescueSnapshotInvalidError, match="index slot must be"):
        parse_slot_meta({"slot": RING_SIZE, "captured_at_ms": 0, "bytes": 1})


def test_parse_index_requires_exact_slot_count() -> None:
    with pytest.raises(RescueSnapshotInvalidError, match=f"index slots must list {RING_SIZE}"):
        parse_index({"schema": 1, "newest_slot": 0, "slots": []})
