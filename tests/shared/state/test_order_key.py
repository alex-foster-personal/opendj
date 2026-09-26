"""Fractional order_key tests (LIBM-20 / LIBM-22).

[if] a key is allocated between two neighbors [then] it sorts uniquely between them, [else stop].
"""

from __future__ import annotations

import pytest

from apps.shared.state.order_key import (
    PrecisionExhausted,
    allocate_keys,
    between,
    from_index,
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
    key = between(None, "00000000")
    assert key < "00000000"


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
    """[if] three keys are allocated between neighbors [then] they stay ordered inside, [else stop]."""
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
