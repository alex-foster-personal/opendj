"""Own-fast sections from 3-band peaks and a downbeat grid (STRUCT-06).

Acceptance lines:
  [if] a track's bands change at its phrase starts [then] own-fast puts a section
       start on each of those bars and labels the arc ⛔️ boundaries off the bar grid,
       or labels guessed without energy contrast
  [if] the track opens with a 2-bar pickup [then] the phrase grid follows the
       music, not bar 1 ⛔️ every boundary 2 bars early
  [if] nothing changes across the track [then] own-fast abstains with
       no_energy_contrast ⛔️ a guessed section span
  [if] the grid is too short or the peaks do not cover it [then] status failed
       with the reason ⛔️ sections cut on a partial track
"""

from __future__ import annotations

import numpy as np
import pytest

from apps.analysis_structure.dj_labels import NO_ENERGY_CONTRAST
from apps.analysis_structure.fast_segment import MIN_BARS, PRODUCER_VERSION, band_envelopes, segment

FPS = 150
BAR_S = 60.0 / 128.0 * 4.0

# (bars, low, mid, high) peak amplitude 0-255; low and high pulse on the beat.
INTRO = (16, 60, 60, 40)
BUILD = (16, 60, 120, 200)
DROP = (32, 230, 160, 200)
BREAK = (16, 15, 120, 10)
RISE = (16, 20, 140, 120)
OUTRO = (16, 60, 60, 40)
CLASSIC = [INTRO, BUILD, DROP, BREAK, RISE, DROP, OUTRO]


def _peaks(plan: list[tuple[int, int, int, int]], seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    cols = []
    for bars, low, mid, high in plan:
        n = round(bars * BAR_S * FPS)
        t = np.arange(n) / FPS
        pulse = 0.6 + 0.4 * (np.mod(t, BAR_S / 4) < 0.08)
        block = np.stack([low * pulse, mid * np.ones(n), high * pulse], axis=1)
        cols.append(block * rng.uniform(0.9, 1.0, (n, 1)))
    return np.clip(np.concatenate(cols), 0, 255).astype(np.uint8)


def _grid(peaks: np.ndarray, offset_s: float = 0.0) -> list[float]:
    duration = peaks.shape[0] / FPS
    return [offset_s + i * BAR_S for i in range(int((duration - offset_s) / BAR_S) + 1)]


def _starts(result: dict) -> list[int | None]:
    return [s["start_bar"] for s in result["sections"]]


def test_classic_arc_gets_a_section_at_each_phrase_change_and_dj_labels():
    peaks = _peaks(CLASSIC)
    result = segment(peaks, _grid(peaks), peaks.shape[0] / FPS)
    assert result["status"] == "ok"
    assert _starts(result) == [0, 16, 32, 64, 80, 96, 128]
    assert [s["dj_label"] for s in result["sections"]] == [
        "intro", "buildup", "drop", "breakdown", "buildup", "drop", "outro",
    ]  # fmt: skip
    assert result["dj_labels"]["status"] == "ok"


def test_section_times_are_the_grid_downbeats():
    peaks = _peaks(CLASSIC)
    grid = _grid(peaks)
    result = segment(peaks, grid, peaks.shape[0] / FPS)
    for section in result["sections"]:
        assert section["start_s"] == round(grid[section["start_bar"]], 3)


def test_a_two_bar_pickup_moves_the_phrase_grid_with_the_music():
    peaks = _peaks([(2, 60, 60, 40), *CLASSIC])
    result = segment(peaks, _grid(peaks), peaks.shape[0] / FPS)
    assert _starts(result)[1:] == [18, 34, 66, 82, 98, 130]
    assert result["phrase_offset"] == 2


@pytest.mark.requirement("STRUCT-12785")
def test_a_flat_track_abstains_with_no_energy_contrast():
    """[if] flat energy is segmented [then] abstain with no_energy_contrast, [else stop]."""
    peaks = _peaks([(128, 200, 150, 180)])
    result = segment(peaks, _grid(peaks), peaks.shape[0] / FPS)
    assert result["status"] == "abstain"
    assert result["abstain_reason"] == NO_ENERGY_CONTRAST
    assert result["sections"] == [] and result["moments"] == []
    assert result["producer_version"] == PRODUCER_VERSION


@pytest.mark.requirement("STRUCT-12785")
def test_a_classic_contrasty_track_keeps_phrase_boundaries():
    """[if] CLASSIC contrasty fixture is segmented [then] phrase starts stay, [else stop]."""
    peaks = _peaks(CLASSIC)
    result = segment(peaks, _grid(peaks), peaks.shape[0] / FPS)
    assert result["status"] == "ok"
    assert _starts(result) == [0, 16, 32, 64, 80, 96, 128]


@pytest.mark.requirement("STRUCT-12785")
def test_a_failed_grid_stays_failed_not_abstain():
    """[if] too few bars [then] status failed not abstain, [else stop]."""
    peaks = _peaks([(MIN_BARS - 1, 200, 150, 180)])
    result = segment(peaks, _grid(peaks)[: MIN_BARS - 1], peaks.shape[0] / FPS)
    assert result["status"] == "failed"
    assert result.get("abstain_reason") is None


def test_too_short_a_grid_fails_with_the_count():
    peaks = _peaks([(MIN_BARS - 1, 200, 150, 180)])
    result = segment(peaks, _grid(peaks)[: MIN_BARS - 1], peaks.shape[0] / FPS)
    assert result == {"status": "failed", "reason": f"too_few_bars:{MIN_BARS - 1}"}


def test_a_track_of_exactly_min_bars_is_segmented():
    """MIN_BARS downbeats, the last bar ending at the file end, is MIN_BARS bars: accepted."""
    half = MIN_BARS // 2
    peaks = _peaks([(half, 60, 60, 40), (MIN_BARS - half, 230, 160, 200)])
    grid = _grid(peaks)[:MIN_BARS]
    result = segment(peaks, grid, peaks.shape[0] / FPS)
    assert result["status"] == "ok"
    assert result["sections"][0]["start_s"] == 0.0 and result["sections"][-1]["end_s"] == round(peaks.shape[0] / FPS, 3)
    # Control: a last bar under half a bar long is a tail, so MIN_BARS downbeats there are one bar short.
    clipped = peaks[: round((MIN_BARS - 1 + 0.3) * BAR_S * FPS)]
    short = segment(clipped, grid, clipped.shape[0] / FPS)
    assert short == {"status": "failed", "reason": f"too_few_bars:{MIN_BARS - 1}"}


def test_the_first_section_starts_on_the_first_downbeat_not_at_zero():
    """A grid that starts late (a pickup before bar 1): no section line off the drawn grid."""
    offset = BAR_S / 2
    peaks = _peaks(CLASSIC)
    grid = _grid(peaks, offset_s=offset)
    result = segment(peaks, grid, peaks.shape[0] / FPS)
    assert result["status"] == "ok"
    first = result["sections"][0]
    assert first["start_bar"] == 0 and first["start_s"] == round(offset, 3)
    drawn = {round(t, 3) for t in grid}
    assert all(s["start_s"] in drawn for s in result["sections"])


def test_peaks_shorter_than_the_grid_fail():
    peaks = _peaks(CLASSIC)
    grid = _grid(peaks)
    half = peaks[: peaks.shape[0] // 2]
    result = segment(half, grid, peaks.shape[0] / FPS)
    assert result == {"status": "failed", "reason": "peaks_shorter_than_grid"}


def test_band_envelopes_map_low_to_bass_and_high_to_drums():
    peaks = _peaks([(16, 200, 10, 10), (16, 10, 10, 200)])
    env = band_envelopes(peaks)
    half = len(env["bass"]) // 2
    assert np.mean(env["bass"][:half]) > np.mean(env["bass"][half:]) + 20
    assert np.mean(env["drums"][half:]) > np.mean(env["drums"][:half]) + 20
    assert len(env["mix"]) == len(env["bass"]) == len(env["drums"])


def test_truncated_peaks_fail_even_when_the_duration_is_the_peaks_own():
    """`compute` passes the peaks' length as the duration; a cut-short decode must not pass as the whole track."""
    peaks = _peaks(CLASSIC)
    grid = _grid(peaks)
    half = peaks[: peaks.shape[0] // 2]
    assert segment(half, grid, half.shape[0] / FPS) == {"status": "failed", "reason": "peaks_shorter_than_grid"}
    # Control: a grid whose last downbeat is at or before the peaks' end is a whole track.
    assert segment(peaks, grid, peaks.shape[0] / FPS)["status"] == "ok"


@pytest.mark.parametrize("cut_bars", [1, 2])
def test_peaks_cut_short_by_a_bar_or_two_fail(cut_bars: int):
    """A decode missing only its last bar or two is still not the whole track: the tail tolerance is one beat."""
    peaks = _peaks(CLASSIC)
    grid = _grid(peaks)
    short = peaks[: peaks.shape[0] - round(cut_bars * BAR_S * FPS)]
    assert segment(short, grid, short.shape[0] / FPS) == {"status": "failed", "reason": "peaks_shorter_than_grid"}
    # Control: a grid whose last downbeat sits within a beat of the peaks' end is a whole track.
    within_a_beat = peaks[: peaks.shape[0] - round(BAR_S / 8 * FPS)]
    assert segment(within_a_beat, _grid(within_a_beat), within_a_beat.shape[0] / FPS)["status"] == "ok"


def test_unlabeled_own_fast_does_not_fake_niche_suggestions():
    """[if] own-fast abstains on flat energy [then] niche suggestions stay empty, else stop."""
    from apps.analysis_structure.dj_labels import structure_niche_suggestions

    peaks = _peaks([(128, 200, 150, 180)])
    result = segment(peaks, _grid(peaks), peaks.shape[0] / FPS)
    assert result["status"] == "abstain"
    assert structure_niche_suggestions(result["sections"], result["dj_labels"]) == []
