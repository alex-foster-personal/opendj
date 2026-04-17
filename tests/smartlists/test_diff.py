"""SMART-02 -- diff_sets helper."""
from __future__ import annotations

import pytest

from apps.smartlists.diff import diff_sets


pytestmark = pytest.mark.requirement("SMART-02")


def test_disjoint_sets() -> None:
    added, removed = diff_sets(["a", "b"], ["c", "d"])
    assert added == ["c", "d"]
    assert removed == ["a", "b"]


def test_identical_sets_empty_diff() -> None:
    assert diff_sets(["a", "b"], ["a", "b"]) == ([], [])


def test_preserves_order_from_new() -> None:
    added, _ = diff_sets([], ["b", "a", "c"])
    assert added == ["b", "a", "c"]


def test_preserves_order_from_old_for_removed() -> None:
    _, removed = diff_sets(["x", "y", "z"], ["y"])
    assert removed == ["x", "z"]


def test_mixed() -> None:
    added, removed = diff_sets(["a", "b", "c"], ["b", "c", "d"])
    assert added == ["d"]
    assert removed == ["a"]
