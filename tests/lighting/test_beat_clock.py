"""LIGHT-02 beat clock acceptance tests (issue #9859, TESTINFRA-14104).

[if] the beat clock predicts a beat, downbeat or host time wrongly [then] fail, [else stop].
[if] predict_beats misses a beat, mistimes at_host_ns, ignores rate, emits on a stopped deck,
fails to extrapolate past the grid or mislabels downbeats [then] fail, [else stop].
[if] invalid samples or grids are not refused or a grid boundary beat doubles [then] fail, [else stop].
"""

from __future__ import annotations

import ast
import itertools
import math
import sys
from pathlib import Path

import pytest

from apps.lighting.beat_clock import DeckSample, predict_beats

pytestmark = [pytest.mark.requirement("LIGHT-9938"), pytest.mark.requirement("TESTINFRA-14104")]

HOST_NS = 5_000_000_000


def _grid(bpm: float, count: int) -> list[dict[str, float | int]]:
    step = 60_000.0 / bpm
    return [{"n": i % 4 + 1, "time_ms": i * step, "bpm": bpm} for i in range(count)]


def _sample(position_ms: float = 1000.0, rate: float = 1.0, playing: bool = True) -> DeckSample:
    return DeckSample(deck="A", position_ms=position_ms, rate=rate, playing=playing, host_time_ns=HOST_NS)


def test_120_bpm_two_second_lookahead_yields_four_beats_timed_to_a_microsecond() -> None:
    """[if] 120 BPM, position 1000 ms, lookahead 2000 ms [then] 4 events at +0..2000 ms."""
    events = predict_beats(_grid(120, 64), _sample(), 2000)
    assert len(events) == 4
    for k, event in enumerate(events):
        expected_ns = HOST_NS + k * 500 * 1_000_000
        assert abs(event.at_host_ns - expected_ns) <= 1_000
        assert event.bpm == 120
        assert not event.extrapolated


def test_rate_scales_host_spacing_and_bpm() -> None:
    """[if] rate is 1.08 [then] host spacing is 500 / 1.08 ms and bpm is 129.6."""
    events = predict_beats(_grid(120, 64), _sample(rate=1.08), 2000)
    assert len(events) >= 2
    gaps_ms = [(b.at_host_ns - a.at_host_ns) / 1_000_000 for a, b in itertools.pairwise(events)]
    for gap in gaps_ms:
        assert gap == pytest.approx(500 / 1.08, abs=1e-3)
    assert all(event.bpm == pytest.approx(129.6) for event in events)


def test_stopped_deck_yields_nothing() -> None:
    """[if] playing is false [then] the result is empty."""
    assert predict_beats(_grid(120, 64), _sample(playing=False), 2000) == []


def test_lookahead_past_the_grid_extrapolates_at_the_last_bpm() -> None:
    """[if] the lookahead runs past the last grid beat [then] later events are extrapolated."""
    grid = _grid(120, 4)  # beats at 0, 500, 1000, 1500
    events = predict_beats(grid, _sample(position_ms=1000.0), 2000)
    assert [e.extrapolated for e in events] == [False, False, True, True]
    assert [e.beat_index for e in events] == [2, 3, 4, 5]
    assert [e.bar_beat for e in events] == [3, 4, 1, 2]
    assert events[2].at_host_ns == HOST_NS + 1000 * 1_000_000
    assert all(e.bpm == 120 for e in events)


def test_position_well_past_the_grid_still_extrapolates() -> None:
    """[if] the deck is far past the last grid beat [then] beats keep coming on the last BPM."""
    events = predict_beats(_grid(120, 4), _sample(position_ms=10_000.0), 1000)
    assert [round((e.at_host_ns - HOST_NS) / 1_000_000) for e in events] == [0, 500]
    assert all(e.extrapolated for e in events)
    assert events[0].bar_beat == 1  # beat 20 from zero is a downbeat


def test_grid_n_one_is_a_downbeat() -> None:
    """[if] the grid has n == 1 on a beat [then] that event is a downbeat with bar_beat 1."""
    events = predict_beats(_grid(120, 64), _sample(position_ms=0.0), 2000)
    assert events[0].is_downbeat and events[0].bar_beat == 1
    assert [e.is_downbeat for e in events] == [True, False, False, False]


def test_module_imports_only_stdlib_and_apps_lighting() -> None:
    """[if] beat_clock imports outside stdlib and apps.lighting [then] fail."""
    import apps.lighting.beat_clock as module

    tree = ast.parse(Path(module.__file__).read_text())
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module)
    for name in roots:
        top = name.split(".")[0]
        assert top in sys.stdlib_module_names or name.startswith("apps.lighting"), name


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("position_ms", math.nan),
        ("position_ms", math.inf),
        ("rate", math.nan),
        ("rate", math.inf),
    ],
)
def test_non_finite_sample_fields_raise_named_validation_error(field: str, value: float) -> None:
    """[if] sample position or rate is non-finite [then] predict_beats raises ValueError naming the field."""
    with pytest.raises(ValueError, match=f"sample {field}"):
        predict_beats(_grid(120, 8), _sample(**{field: value}), 2000)


@pytest.mark.parametrize(
    "grid",
    [
        [
            {"n": 1, "time_ms": 0, "bpm": 120},
            {"n": 2, "time_ms": 500, "bpm": 120},
            {"n": 3, "time_ms": 400, "bpm": 120},
        ],
        [
            {"n": 1, "time_ms": 0, "bpm": 120},
            {"n": 2, "time_ms": 500, "bpm": 120},
            {"n": 2, "time_ms": 500, "bpm": 120},
        ],
    ],
    ids=["unsorted", "duplicate"],
)
def test_unsorted_or_duplicate_grid_times_are_refused(grid: list[dict[str, float | int]]) -> None:
    """[if] grid times are unsorted or duplicate [then] predict_beats refuses with a named validation error."""
    pattern = "duplicate grid beat time" if grid[-1]["time_ms"] == grid[-2]["time_ms"] else "strictly increase"
    with pytest.raises(ValueError, match=pattern):
        predict_beats(grid, _sample(position_ms=0), 2000)


@pytest.mark.parametrize("bpm", [math.nan, math.inf, 0.0, -1.0])
def test_non_finite_or_nonpositive_grid_bpm_raises(bpm: float) -> None:
    """[if] a grid beat bpm is non-finite or non-positive [then] predict_beats raises ValueError."""
    grid = _grid(120, 4)
    grid[1]["bpm"] = bpm
    pattern = "bpm must be positive" if bpm <= 0 else "bpm must be finite"
    with pytest.raises(ValueError, match=pattern):
        predict_beats(grid, _sample(position_ms=0), 2000)


def test_sample_on_exact_grid_boundary_emits_that_beat_once_in_order() -> None:
    """[if] position sits on a grid beat time [then] that beat appears once and later beats stay ordered."""
    grid = _grid(120, 16)
    events = predict_beats(grid, _sample(position_ms=500.0), 2000)
    beat_indices = [event.beat_index for event in events]
    assert beat_indices[0] == 1
    assert beat_indices == sorted(beat_indices)
    assert len(beat_indices) == len(set(beat_indices))
    assert events[0].at_host_ns == HOST_NS
    for earlier, later in itertools.pairwise(events):
        assert later.at_host_ns > earlier.at_host_ns
