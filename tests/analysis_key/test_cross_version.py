"""Cross-version key-sequence consistency (synthetic blocks, no audio)."""

from __future__ import annotations

from apps.analysis_key import canon, cross_version, segments
from apps.analysis_key.segments import KeySegment, SegmentBlock

C_MAJOR = canon.Key(0, False)
F_SHARP_MAJOR = canon.Key(6, False)
G_MAJOR = canon.Key(7, False)


def _grid(n_bars: int, *, bar_s: float = 2.0) -> segments.BarGrid:
    starts = tuple(i * bar_s for i in range(n_bars))
    return segments.BarGrid(starts=starts, ends=(*starts[1:], n_bars * bar_s))


def _two_segment_block(
    grid: segments.BarGrid, *, boundary_bar: int = 16, second_key: canon.Key = F_SHARP_MAJOR
) -> SegmentBlock:
    segs = (
        KeySegment(
            start_bar=0,
            end_bar=boundary_bar,
            start_s=grid.starts[0],
            end_s=grid.ends[boundary_bar - 1],
            key=C_MAJOR,
            confidence=0.9,
        ),
        KeySegment(
            start_bar=boundary_bar,
            end_bar=grid.n_bars,
            start_s=grid.starts[boundary_bar],
            end_s=grid.ends[grid.n_bars - 1],
            key=second_key,
            confidence=0.85,
        ),
    )
    return SegmentBlock(status="ok", reason=None, segments=segs)


def test_extended_and_radio_pair_agrees() -> None:
    grid = _grid(32)
    a = cross_version.VersionTrack(
        stable_id="a",
        title="Song (Extended Mix)",
        artists=("Artist One",),
        segments=_two_segment_block(grid),
        grid=grid,
    )
    b = cross_version.VersionTrack(
        stable_id="b",
        title="Song (Radio Edit)",
        artists=("Artist One",),
        segments=_two_segment_block(grid),
        grid=grid,
    )
    score = cross_version.consistency_rate((a, b))
    assert score.n_pairs == 1
    assert score.n_agree == 1


def test_moved_boundary_disagrees() -> None:
    grid = _grid(32)
    a = cross_version.VersionTrack(
        stable_id="a",
        title="Song (Extended Mix)",
        artists=("Artist One",),
        segments=_two_segment_block(grid, boundary_bar=16),
        grid=grid,
    )
    b = cross_version.VersionTrack(
        stable_id="b",
        title="Song (Radio Edit)",
        artists=("Artist One",),
        segments=_two_segment_block(grid, boundary_bar=19),
        grid=grid,
    )
    assert cross_version.sequences_agree(a, b) is False


def test_different_second_key_disagrees() -> None:
    grid = _grid(32)
    a = cross_version.VersionTrack(
        stable_id="a",
        title="Song (Extended Mix)",
        artists=("Artist One",),
        segments=_two_segment_block(grid, second_key=F_SHARP_MAJOR),
        grid=grid,
    )
    b = cross_version.VersionTrack(
        stable_id="b",
        title="Song (Radio Edit)",
        artists=("Artist One",),
        segments=_two_segment_block(grid, second_key=G_MAJOR),
        grid=grid,
    )
    assert cross_version.sequences_agree(a, b) is False


def test_remix_and_vip_do_not_pair() -> None:
    grid = _grid(32)
    good = cross_version.VersionTrack(
        stable_id="g",
        title="Song",
        artists=("X",),
        segments=_two_segment_block(grid),
        grid=grid,
    )
    remix = cross_version.VersionTrack(
        stable_id="r",
        title="Song (Artist Remix)",
        artists=("X",),
        segments=_two_segment_block(grid),
        grid=grid,
    )
    vip = cross_version.VersionTrack(
        stable_id="v",
        title="Song (VIP Mix)",
        artists=("X",),
        segments=_two_segment_block(grid),
        grid=grid,
    )
    assert cross_version.pair_versions((good, remix, vip)) == ()


def test_clean_and_explicit_pair() -> None:
    grid = _grid(32)
    a = cross_version.VersionTrack(
        stable_id="c1",
        title="Song (Clean)",
        artists=("Y",),
        segments=_two_segment_block(grid),
        grid=grid,
    )
    b = cross_version.VersionTrack(
        stable_id="c2",
        title="Song (Explicit)",
        artists=("Y",),
        segments=_two_segment_block(grid),
        grid=grid,
    )
    score = cross_version.consistency_rate((a, b))
    assert score.n_pairs == 1
    assert score.n_agree == 1


def test_constant_predictor_two_single_key() -> None:
    grid = _grid(16)

    def single() -> SegmentBlock:
        seg = KeySegment(
            start_bar=0,
            end_bar=16,
            start_s=grid.starts[0],
            end_s=grid.ends[15],
            key=C_MAJOR,
            confidence=0.9,
        )
        return SegmentBlock(status="ok", reason=None, segments=(seg,))

    a = cross_version.VersionTrack(
        stable_id="s1",
        title="Same (Extended Mix)",
        artists=("Z",),
        segments=single(),
        grid=grid,
    )
    b = cross_version.VersionTrack(
        stable_id="s2",
        title="Same (Radio Edit)",
        artists=("Z",),
        segments=single(),
        grid=grid,
    )
    score = cross_version.consistency_rate((a, b))
    assert score.n_pairs == 1
    assert score.n_agree == 1
    assert score.n_constant_agree == 1


def test_constant_predictor_two_segment_zero() -> None:
    grid = _grid(32)
    a = cross_version.VersionTrack(
        stable_id="a",
        title="Work (Extended)",
        artists=("W",),
        segments=_two_segment_block(grid),
        grid=grid,
    )
    b = cross_version.VersionTrack(
        stable_id="b",
        title="Work (Radio)",
        artists=("W",),
        segments=_two_segment_block(grid),
        grid=grid,
    )
    score = cross_version.consistency_rate((a, b))
    assert score.n_constant_agree == 0


def test_three_versions_yield_three_pairs() -> None:
    grid = _grid(32)
    titles = ("Tune (Extended Mix)", "Tune (Radio Edit)", "Tune (Club Mix)")
    tracks = tuple(
        cross_version.VersionTrack(
            stable_id=f"t{i}",
            title=titles[i],
            artists=("T",),
            segments=_two_segment_block(grid),
            grid=grid,
        )
        for i in range(3)
    )
    assert len(cross_version.pair_versions(tracks)) == 3


def test_empty_title_does_not_pair() -> None:
    grid = _grid(16)
    seg = KeySegment(
        start_bar=0,
        end_bar=16,
        start_s=0.0,
        end_s=32.0,
        key=C_MAJOR,
        confidence=0.8,
    )
    block = SegmentBlock(status="ok", reason=None, segments=(seg,))
    a = cross_version.VersionTrack(
        stable_id="e1",
        title="",
        artists=("E",),
        segments=block,
        grid=grid,
    )
    b = cross_version.VersionTrack(
        stable_id="e2",
        title="X",
        artists=("E",),
        segments=block,
        grid=grid,
    )
    assert cross_version.pair_versions((a, b)) == ()
