"""The fixture's own-beatgrid seeding fails closed and its audio is real noise (round 2).

The Beat This! run itself needs the verified checkpoint (MDT_BEATGRID_WEIGHTS)
and torch, so it is exercised by the performance e2e config, not here. These
pin the parts that must refuse loudly without it. -Claude
"""
from __future__ import annotations

from pathlib import Path

import pytest

from apps.webui.frontend.tests.e2e.support import deckload_fixture as fx


def test_seed_own_beatgrid_requires_rescue_playback(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="requires --seed-rescue-playback"):
        fx.main(["--data-dir", str(tmp_path / "d"), "--seed-own-beatgrid"])


@pytest.mark.parametrize("weights", ["", "/nonexistent/beat_this.ckpt"])
def test_own_beatgrid_without_weights_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, weights: str
) -> None:
    monkeypatch.setenv(fx.OWN_BEATGRID_WEIGHTS_ENV, weights)
    with pytest.raises(SystemExit, match=fx.OWN_BEATGRID_WEIGHTS_ENV):
        fx._run_own_beatgrid_analysis(tmp_path, [], fx.RESCUE_PLAYBACK_TRACKS, "test")


def test_noise_is_not_a_sawtooth() -> None:
    values = [fx._noise(i) for i in range(4096)]
    assert all(-1.0 <= v < 1.0 for v in values)
    assert abs(sum(values) / len(values)) < 0.05
    # A linear generator of the index has a constant step; real noise does not.
    steps = {round(values[i + 1] - values[i], 9) for i in range(len(values) - 1)}
    assert len(steps) > 4000
    assert fx._noise(123) == fx._noise(123)


def test_backbeat_kicks_on_one_and_three_snares_on_two_and_four() -> None:
    period = fx.SAMPLE_RATE_HZ * 60.0 / 120.0
    kick_len = int(fx.SAMPLE_RATE_HZ * fx.KICK_MS / 1000.0)

    def energy(beat: int) -> float:
        start = int(beat * period)
        return sum(abs(fx._backbeat_value(start + i, period)) for i in range(kick_len))

    one, two, three, four = (energy(b) for b in range(4))
    assert one > three > 0.0, "beat 1 kick is the accent, beat 3 kick quieter"
    assert two > 0.0 and four > 0.0
    # Silence between hits: the tail of beat 1 is empty.
    assert fx._backbeat_value(int(period * 0.9), period) == 0.0


def test_fold_track_outlives_its_last_shifted_beat() -> None:
    # The own_beatgrid producer refuses a beat past duration (a 60.0 s file
    # crashed the backfill with exit 5), so the fold track is 61 s.
    assert fx.PERFORMANCE_FOLD_TRACK.pattern == fx.PATTERN_BACKBEAT
    assert fx.PERFORMANCE_FOLD_TRACK.seconds > 60.0
