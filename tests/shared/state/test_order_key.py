"""Fractional order_key tests (LIBM-20 / LIBM-22).

[if] a key is allocated between two neighbors [then] it sorts uniquely between them, [else stop].
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from apps.shared.state.order_key import (
    KEY_BASE,
    PrecisionExhausted,
    allocate_keys,
    between,
    from_index,
    renumbered_keys,
)

pytestmark = pytest.mark.requirement("LIBM-20")


# REQ: LIBM-02
def test_from_index_matches_printf() -> None:
    assert from_index(0) == "00000000"
    assert from_index(1) == "00000001"
    assert from_index(9) == "00000009"
    assert from_index(10) == "00000010"
    assert from_index(99) == "00000099"


def test_from_index_sorts() -> None:
    assert from_index(0) < from_index(1) < from_index(10)


def test_between_adjacent_indices() -> None:
    mid = between("00000000", "00000001")
    assert "00000000" < mid < "00000001"


def test_between_append_after_last() -> None:
    key = between("00000009", None)
    assert key > "00000009"


def test_between_insert_before_first() -> None:
    key = between(None, "00000001")
    assert "" < key < "00000001"


@pytest.mark.requirement("LIBM-132")
@pytest.mark.parametrize("floor", ["00000000", "0", ""])
def test_no_key_is_minted_below_a_floor_key(floor: str) -> None:
    """[if] nothing sorts below the right bound [then] it raises, never mints '', [else stop]."""
    with pytest.raises(PrecisionExhausted):
        between(None, floor)


@pytest.mark.requirement("LIBM-132")
@pytest.mark.parametrize(("left", "right"), [("00000004", "00000004"), ("00000005", "00000004")])
def test_neighbors_sharing_or_inverting_a_key_raise_precision_exhausted(
    left: str, right: str,
) -> None:
    """[if] the neighbors leave no gap [then] callers get PrecisionExhausted to renumber, [else stop]."""
    with pytest.raises(PrecisionExhausted):
        allocate_keys(left, right, 1)


@pytest.mark.requirement("LIBM-132")
def test_ten_thousand_appends_keep_keys_at_eight_characters() -> None:
    """[if] 10,000 rows are appended one by one [then] no key grows past 8 chars, [else stop]."""
    keys = allocate_keys(None, None, 10_000)
    assert keys[0] == from_index(KEY_BASE)
    assert all(a < b for a, b in pairwise(keys))
    assert max(len(k) for k in keys) == 8, max(len(k) for k in keys)


@pytest.mark.requirement("LIBM-132")
def test_ten_thousand_head_inserts_keep_keys_at_eight_characters() -> None:
    """[if] 10,000 rows are inserted at the head [then] no key grows past 8 chars, [else stop]."""
    keys = [between(None, None)]
    for _ in range(10_000):
        keys.append(between(None, keys[-1]))
    assert all(a > b for a, b in pairwise(keys))
    assert {len(k) for k in keys} == {8}


@pytest.mark.requirement("LIBM-132")
@pytest.mark.parametrize(
    ("left", "right"), [("0100001", "01000011"), ("", "01000000"), ("01", "01V")],
)
def test_a_key_between_a_prefix_and_its_extension_sorts_between_them(
    left: str, right: str,
) -> None:
    """[if] left is a prefix of right [then] the new key sorts strictly between, [else stop]."""
    key = between(left, right)
    assert left < key < right, (left, key, right)


@pytest.mark.requirement("LIBM-132")
def test_renumbered_keys_leave_room_before_the_first() -> None:
    """[if] a playlist is renumbered [then] a key still fits before its first row, [else stop]."""
    keys = renumbered_keys(3)
    assert keys == [from_index(KEY_BASE + i) for i in range(3)]
    assert between(None, keys[0]) < keys[0]


def test_fifty_inserts_at_same_point_stay_unique_and_sorted() -> None:
    prev = "00000000"
    right = "00000001"
    keys: list[str] = []
    for _ in range(50):
        k = between(prev, right)
        keys.append(k)
        prev = k
    assert len(set(keys)) == 50
    assert all(keys[i] < keys[i + 1] for i in range(len(keys) - 1))
    assert all(k < right for k in keys)


@pytest.mark.requirement("LIBM-22")
def test_allocate_keys_three_between_neighbors() -> None:
    (
        """[if] three keys are allocated between neighbors [then] they stay ordered """
        """inside, [else stop]."""
    )
    keys = allocate_keys("00000000", "00000010", 3)
    assert len(keys) == 3
    assert len(set(keys)) == 3
    assert "00000000" < keys[0] < keys[1] < keys[2] < "00000010"


@pytest.mark.requirement("LIBM-22")
def test_allocate_keys_precision_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the key space is too narrow [then] PrecisionExhausted is raised, [else stop]."""
    import apps.shared.state.order_key as order_key_mod

    monkeypatch.setattr(order_key_mod, "MAX_ORDER_KEY_LEN", 8)
    with pytest.raises(PrecisionExhausted):
        allocate_keys("00000000", "00000001", 1)
