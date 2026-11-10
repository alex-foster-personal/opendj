"""Unit checks for shared bundle join guards."""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.analysis_bench.scorers import bundle_guards


def test_sorted_duplicates_returns_sorted_unique_repeats() -> None:
    assert bundle_guards.sorted_duplicates(["b", "a", "b", "c", "a", "a"]) == ["a", "b"]


def test_refuse_duplicate_stable_ids_names_the_first_repeat(tmp_path: Path) -> None:
    bundle = tmp_path / "waveform-v1"
    bundle.mkdir()
    with pytest.raises(ValueError, match="duplicate track stable_id") as exc:
        bundle_guards.refuse_duplicate_stable_ids(bundle, ["x", "y", "x"], entity="track")
    assert "first 'x'" in str(exc.value)
