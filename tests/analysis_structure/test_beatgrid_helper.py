"""`downbeats_from_beats` stays fail-fast on a malformed beat list; only fast_sidecar normalizes."""

from __future__ import annotations

import pytest

from apps.analysis_structure._beatgrid import downbeats_from_beats
from apps.analysis_structure.fast_sidecar import downbeats_of


def test_helper_keeps_only_downbeats() -> None:
    assert downbeats_from_beats([{"t": 1.5, "n": 1}, {"t": 2.0, "n": 2}, {"t": "3", "n": "1"}]) == [1.5, 3.0]


def test_helper_raises_on_a_null_beat_list() -> None:
    with pytest.raises(TypeError):
        downbeats_from_beats(None)  # type: ignore[arg-type]


def test_fast_sidecar_reads_a_null_beat_list_as_no_downbeats() -> None:
    assert downbeats_of({"beatgrid": {"beats": None}}) == []
