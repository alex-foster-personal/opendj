"""Key-change segmentation: bar-synchronous chroma, Viterbi, 8-bar floor.

`specs/native-analysis-v1.md` section 5, "Key changes": bar-synchronous chroma
on OWN downbeats, the shipping key producer's posterior as HMM emissions,
HMM + Viterbi over bars, minimum 8-bar segments.

Section 8's mutation test is the fixture every boundary assertion here uses:
"inject a transposition at bar N and expect a key segment boundary at N".
Synthetic chroma is the right instrument for that (the transposition is the
INPUT, not a stand-in for a dependency), and `tests/analysis_key/
test_backfill_write.py` is where the same code runs over real audio.

-Claude
"""
from __future__ import annotations

import numpy as np
import pytest

from apps.analysis_key import canon, segments
from apps.analysis_key.segments import BarGrid

C_MAJOR = canon.Key(0, False)
F_SHARP_MAJOR = canon.Key(6, False)
A_MINOR = canon.Key(9, True)


#-----------------------------------------------------------------------------
# fixtures
#-----------------------------------------------------------------------------

def _grid(n_bars: int, *, bar_s: float = 2.0) -> BarGrid:
    starts = tuple(i * bar_s for i in range(n_bars))
    return BarGrid(starts=starts, ends=(*starts[1:], n_bars * bar_s))


def _triad(key: canon.Key) -> np.ndarray:
    """A chroma column for one key's tonic triad: root, third, fifth."""
    third = 3 if key.is_minor else 4
    vector = np.zeros(12)
    for interval in (0, third, 7):
        vector[(key.pitch_class + interval) % 12] = 1.0
    return vector / np.linalg.norm(vector)


def _bar_chroma(keys: list[canon.Key], *, seed: int = 7) -> np.ndarray:
    """One chroma COLUMN per bar, in `keys` order."""
    rng = np.random.default_rng(seed)
    columns = []
    for key in keys:
        # Small positive jitter, not a stand-in for a dependency: real chroma
        # is never an exact triad, and a tie between two keys is a case the
        # segmenter is allowed to resolve either way.
        column = np.clip(_triad(key) + rng.uniform(0.0, 0.05, 12), 0.0, None)
        columns.append(column)
    return np.stack(columns, axis=1)


def _keys(*blocks: tuple[canon.Key, int]) -> list[canon.Key]:
    return [key for key, n in blocks for _ in range(n)]


#-----------------------------------------------------------------------------
# the bar grid
#-----------------------------------------------------------------------------

def _beat(t: float, n: int) -> dict[str, float]:
    return {"t": t, "n": n, "bpm": 120.0}


def test_bar_grid_takes_every_downbeat_as_a_bar_start() -> None:
    beats = [
        _beat(0.0, 1), _beat(0.5, 2), _beat(1.0, 3), _beat(1.5, 4),
        _beat(2.0, 1), _beat(2.5, 2), _beat(3.0, 3), _beat(3.5, 4),
    ]
    grid = segments.bar_grid_from_beats(beats, duration_s=4.0)
    assert grid is not None
    assert grid.starts == (0.0, 2.0)
    assert grid.ends == (2.0, 4.0)


def test_bar_grid_is_missing_without_two_downbeats() -> None:
    """`missing`, not `failed`: no own downbeats is a state, not a measurement
    the analyzer declined. The lane says so with the block's own status."""
    assert segments.bar_grid_from_beats([], duration_s=10.0) is None
    one_bar = [_beat(0.0, 1), _beat(0.5, 2), _beat(1.0, 3), _beat(1.5, 4)]
    assert segments.bar_grid_from_beats(one_bar, duration_s=2.0) is None
    # A grid with no beat numbered 1 at all has no downbeat either.
    no_downbeat = [_beat(0.0, 2), _beat(0.5, 3), _beat(1.0, 4), _beat(1.5, 2)]
    assert segments.bar_grid_from_beats(no_downbeat, duration_s=2.0) is None


def test_last_bar_ends_at_the_record_duration() -> None:
    """The write boundary checks `end_s <= duration_s`; the final bar has no
    following downbeat to close it, so the record's own length closes it."""
    beats = [_beat(t, (i % 4) + 1) for i, t in enumerate([0.0, 0.5, 1.0, 1.5, 2.0, 2.5])]
    grid = segments.bar_grid_from_beats(beats, duration_s=3.7)
    assert grid is not None
    assert grid.ends[-1] == 3.7


#-----------------------------------------------------------------------------
# segmentation
#-----------------------------------------------------------------------------

def test_a_stable_key_is_exactly_one_segment() -> None:
    grid = _grid(32)
    block = segments.segment_bars(_bar_chroma(_keys((C_MAJOR, 32))), grid, duration_s=64.0)
    assert block.status == "ok"
    assert block.reason is None
    assert len(block.segments) == 1
    only = block.segments[0]
    assert (only.start_bar, only.end_bar) == (0, 32)
    assert (only.start_s, only.end_s) == (0.0, 64.0)
    assert only.key == C_MAJOR


def test_a_transposition_puts_the_boundary_at_the_change_bar() -> None:
    """Spec section 8's mutation test, in the chroma domain."""
    grid = _grid(32)
    chroma = _bar_chroma(_keys((C_MAJOR, 16), (F_SHARP_MAJOR, 16)))
    block = segments.segment_bars(chroma, grid, duration_s=64.0)
    assert block.status == "ok"
    assert len(block.segments) == 2
    boundary = block.segments[1].start_bar
    assert abs(boundary - 16) <= 1, f"boundary at bar {boundary}, injected at 16"
    assert block.segments[0].key == C_MAJOR
    assert block.segments[1].key == F_SHARP_MAJOR


def test_a_change_reports_the_key_at_the_end_of_the_track() -> None:
    grid = _grid(24)
    chroma = _bar_chroma(_keys((C_MAJOR, 12), (A_MINOR, 12)))
    block = segments.segment_bars(chroma, grid, duration_s=48.0)
    assert block.status == "ok"
    assert block.segments[-1].key == A_MINOR


def test_segments_are_contiguous_and_meet_in_time() -> None:
    """The contract's own rule, checked here on the producer's output: adjacent
    segments share a boundary exactly, they do not merely avoid overlapping."""
    grid = _grid(32)
    chroma = _bar_chroma(_keys((C_MAJOR, 16), (F_SHARP_MAJOR, 16)))
    block = segments.segment_bars(chroma, grid, duration_s=64.0)
    assert len(block.segments) > 1
    for previous, following in zip(block.segments, block.segments[1:], strict=False):
        assert following.start_bar == previous.end_bar
        assert following.start_s == pytest.approx(previous.end_s, abs=1e-9)


def test_a_short_island_is_merged_not_published() -> None:
    """Minimum 8-bar segments: a 3-bar excursion is not a key change."""
    grid = _grid(24)
    chroma = _bar_chroma(_keys((C_MAJOR, 10), (F_SHARP_MAJOR, 3), (C_MAJOR, 11)))
    block = segments.segment_bars(chroma, grid, duration_s=48.0)
    assert block.status == "ok"
    assert len(block.segments) == 1


def test_two_real_changes_are_two_boundaries() -> None:
    grid = _grid(40)
    chroma = _bar_chroma(_keys((C_MAJOR, 16), (F_SHARP_MAJOR, 12), (A_MINOR, 12)))
    block = segments.segment_bars(chroma, grid, duration_s=80.0)
    assert block.status == "ok"
    assert len(block.segments) == 3
    assert [segment.key for segment in block.segments] == [
        C_MAJOR, F_SHARP_MAJOR, A_MINOR
    ]


def test_too_few_bars_to_hold_one_segment_fails_with_a_reason() -> None:
    """Under 8 bars there is no segment the minimum rule can publish, and a
    lane that cannot measure says `failed` plus a reason rather than returning
    an empty `ok` block (spec section 3)."""
    grid = _grid(4)
    block = segments.segment_bars(_bar_chroma(_keys((C_MAJOR, 4))), grid, duration_s=8.0)
    assert block.status == "failed"
    assert block.reason == segments.REASON_TOO_FEW_BARS
    assert block.segments == ()


def test_a_failed_block_is_never_ok_with_no_segments() -> None:
    """The one shape the contract refuses outright: `ok` with an empty array."""
    grid = _grid(3)
    block = segments.segment_bars(_bar_chroma(_keys((C_MAJOR, 3))), grid, duration_s=6.0)
    assert not (block.status == "ok" and not block.segments)


def test_segment_confidence_is_a_probability() -> None:
    grid = _grid(16)
    block = segments.segment_bars(_bar_chroma(_keys((C_MAJOR, 16))), grid, duration_s=32.0)
    assert block.status == "ok"
    assert 0.0 <= block.segments[0].confidence <= 1.0


def test_missing_block_carries_no_segments_and_a_reason() -> None:
    block = segments.missing_block(segments.REASON_NO_DOWNBEATS)
    assert block.status == "missing"
    assert block.reason == segments.REASON_NO_DOWNBEATS
    assert block.segments == ()


def test_segment_bars_refuses_a_chroma_matrix_of_the_wrong_width() -> None:
    """Fail fast rather than broadcasting a misaligned matrix into 12 rows."""
    grid = _grid(16)
    with pytest.raises(ValueError, match="pitch-class"):
        segments.segment_bars(np.zeros((11, 16)), grid, duration_s=32.0)


def test_segment_bars_refuses_a_bar_count_that_disagrees_with_the_grid() -> None:
    grid = _grid(16)
    with pytest.raises(ValueError, match="bars"):
        segments.segment_bars(np.zeros((12, 15)), grid, duration_s=32.0)


#-----------------------------------------------------------------------------
# the block as the record carries it
#-----------------------------------------------------------------------------

def test_block_to_payload_matches_the_contract_shape() -> None:
    grid = _grid(32)
    chroma = _bar_chroma(_keys((C_MAJOR, 16), (A_MINOR, 16)))
    block = segments.segment_bars(chroma, grid, duration_s=64.0)
    payload = block.to_payload()
    assert set(payload) == {"status", "reason", "segments"}
    assert payload["status"] == "ok"
    assert payload["reason"] is None
    first = payload["segments"][0]
    assert set(first) == {
        "start_bar", "end_bar", "start_s", "end_s",
        "key_camelot", "key_openkey", "confidence",
    }
    # Notations come from the ONE canonicalizer (apps.analysis_key.canon),
    # never a second table (A minor = 8A = 1m).
    assert first["key_camelot"] == canon.to_camelot(C_MAJOR)
    assert first["key_openkey"] == canon.to_open_key(C_MAJOR)
    assert first["key_camelot"] == "8B"


def test_block_payload_of_a_failed_block_carries_the_reason() -> None:
    block = segments.segment_bars(
        _bar_chroma(_keys((C_MAJOR, 4))), _grid(4), duration_s=8.0
    )
    payload = block.to_payload()
    assert payload["status"] == "failed"
    assert payload["reason"] == segments.REASON_TOO_FEW_BARS
    assert payload["segments"] == []


def test_payload_end_never_rounds_past_the_record_duration() -> None:
    """Live on demon-llama, Thu 1 Oct 2026: a last segment ending at the
    decode's length 266.5650793650794 was published as round(x, 5) = 266.56508,
    which the write boundary refused, and the refusal stopped the key lane."""
    from apps.analysis.lane_payloads import key_segments_within_duration

    duration_s = 266.5650793650794
    starts = tuple(i * 8.0 for i in range(32))
    grid = BarGrid(starts=starts, ends=(*starts[1:], duration_s))
    block = segments.segment_bars(_bar_chroma(_keys((C_MAJOR, 32))), grid, duration_s=duration_s)
    payload = block.to_payload()
    last_end = payload["segments"][-1]["end_s"]
    assert last_end <= duration_s, "if a published end rounds past duration_s then broken"
    assert duration_s - last_end < 1e-5, "if the end is clamped further than its 5 dp precision then broken"
    key_segments_within_duration({"segments": payload}, duration_s)
