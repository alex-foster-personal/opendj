"""Selection played-length estimate (LIBUX played length): the one backend function.

- [if] a track carries mix-in and mix-out points [then] it counts mix_out - mix_in, [else stop].
- [if] a track has a BPM and no mix points [then] it counts duration minus 32 bars, clamped to half, [else stop].
- [if] a track has no BPM [then] it counts 75% of its duration, [else stop].
- [if] 500 tracks are estimated [then] it takes well under 50 ms, [else stop].
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from apps.shared.played_length import PlayedLengthInput, estimate_played_length, estimate_track

pytestmark = pytest.mark.requirement("LIBUX-8673")

_FIXTURE = json.loads((Path(__file__).parents[1] / "fixtures" / "played_length_cases.json").read_text())


def _input(raw: dict) -> PlayedLengthInput:
    return PlayedLengthInput(
        duration_s=raw["duration_s"], bpm=raw["bpm"], mix_in_s=raw.get("mix_in_s"), mix_out_s=raw.get("mix_out_s")
    )


@pytest.mark.parametrize("case", _FIXTURE["cases"], ids=[c["name"] for c in _FIXTURE["cases"]])
def test_each_fixture_case(case: dict) -> None:
    played_s, source = estimate_track(_input(case["track"]))
    assert played_s == pytest.approx(case["played_s"])
    assert source == case["source"]


def test_sum_over_the_fixture() -> None:
    result = estimate_played_length([_input(c["track"]) for c in _FIXTURE["cases"]])
    assert result.to_dict() == pytest.approx(_FIXTURE["total"])


def test_five_hundred_tracks_are_instant() -> None:
    tracks = [PlayedLengthInput(duration_s=240 + i % 60, bpm=120 + i % 10) for i in range(500)]
    started = time.perf_counter()
    result = estimate_played_length(tracks)
    assert time.perf_counter() - started < 0.05
    assert result.from_rule == 500
