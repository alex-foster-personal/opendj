"""Unit tests for ``apps.adapters.rekordbox.cue_kind`` (issue #4069)."""
from __future__ import annotations

import pytest

from apps.adapters.rekordbox.cue_kind import (
    KIND_BY_SLOT,
    HotCueSlotError,
    is_mapped_kind,
    kind_to_slot,
    slot_to_kind,
)


def test_slot_to_kind_a_through_h():
    assert slot_to_kind("A") == 1
    assert slot_to_kind("B") == 2
    assert slot_to_kind("C") == 3
    # issue #4069: do not flip until the rekordbox-UI experiment
    assert slot_to_kind("D") == 4
    assert slot_to_kind("E") == 5
    assert slot_to_kind("F") == 6
    assert slot_to_kind("G") == 7
    assert slot_to_kind("H") == 8


def test_kind_to_slot_boundaries():
    assert kind_to_slot(4) == "D"
    assert kind_to_slot(9) is None
    assert kind_to_slot(0) is None


def test_is_mapped_kind():
    assert is_mapped_kind(0)
    assert is_mapped_kind(8)
    assert not is_mapped_kind(9)


def test_kind_by_slot_is_single_source():
    for slot in "ABCDEFGH":
        assert slot_to_kind(slot) == KIND_BY_SLOT[slot]


@pytest.mark.parametrize("bad", ["I", "9", ""])
def test_hot_cue_slot_error(bad: str):
    with pytest.raises(HotCueSlotError):
        slot_to_kind(bad)
