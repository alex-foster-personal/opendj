"""Unit tests for tools/deck-diagrams/scripts/_fig_sort.py."""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[2] / "tools" / "deck-diagrams" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from _fig_sort import fig_sort_key  # noqa: E402


def test_fig_sort_key_orders_section_before_letter() -> None:
    assert fig_sort_key("1-1") < fig_sort_key("D1")


def test_fig_sort_key_section_numeric_order() -> None:
    ordered = sorted(["3-2", "1-10", "1-2"], key=fig_sort_key)
    assert ordered == ["1-2", "1-10", "3-2"]


def test_fig_sort_key_letter_numeric_order() -> None:
    ordered = sorted(["D10", "D2", "B1"], key=fig_sort_key)
    assert ordered == ["B1", "D2", "D10"]
