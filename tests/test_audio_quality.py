"""Venue-rung classifier boundaries -- apps/shared/audio_quality.py.

One line of intent per test. Sizes are computed from the target kbps so the
boundary tests sit exactly on the cutoff rather than near it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from apps.shared import audio_quality
from apps.shared.audio_quality import classify, ladder

DURATION_MS = 180_000  # a 3-minute track


def _bytes_for(kbps: float, duration_ms: int = DURATION_MS) -> int:
    """Exact byte count that yields `kbps` effective bitrate."""
    return round(kbps * 1000 * (duration_ms / 1000) / 8)


def _write(tmp_path: Path, name: str, size: int) -> str:
    path = tmp_path / name
    path.write_bytes(b"\0" * size)
    return str(path)


# ----- boundaries ------------------------------------------------------------
@pytest.mark.parametrize("kbps,expected", [
    (127, "naughty_step"), (128, "lounge"),
    (191, "lounge"), (192, "house_party"),
    (255, "house_party"), (256, "club"),
    (319, "club"), (320, "warehouse"),
])
def test_lossy_cutoffs_land_on_the_right_rung(kbps, expected):
    """If a bitrate sits on a cutoff then it takes the HIGHER rung."""
    q = classify("x.mp3", DURATION_MS, _bytes_for(kbps))
    assert q.venue is not None and q.venue.key == expected, (
        f"{kbps} kbps -> {q.venue.key if q.venue else None}, want {expected}"
    )


def test_lossless_beats_a_loud_lossy_file():
    """If a quiet flac is smaller than a 320 kbps mp3 then flac still wins."""
    quiet_flac = classify("quiet.flac", DURATION_MS, _bytes_for(200))
    loud_mp3 = classify("loud.mp3", DURATION_MS, _bytes_for(320))
    assert quiet_flac.venue.key == "stadium"
    assert quiet_flac.venue.rank > loud_mp3.venue.rank


def test_lossless_is_decided_by_container_not_bitrate():
    """If a wav reads far below 128 kbps then it is still Stadium."""
    q = classify("tiny.wav", DURATION_MS, _bytes_for(64))
    assert q.venue.key == "stadium" and q.lossless is True


# ----- honest unknowns -------------------------------------------------------
def test_zero_duration_is_unknown_not_a_guess():
    """If duration is 0 then venue is None and the reason names the duration."""
    q = classify("x.mp3", 0, _bytes_for(320))
    assert q.venue is None and "duration" in q.reason


def test_missing_duration_is_unknown():
    """If duration is None then no rung is invented."""
    q = classify("x.mp3", None, _bytes_for(320))
    assert q.venue is None and q.as_dict()["rank"] is None


def test_missing_file_is_unknown(tmp_path):
    """If the file is not on disk and no size is passed then reason is 'file missing'."""
    q = classify(str(tmp_path / "gone.mp3"), DURATION_MS)
    assert q.venue is None and q.reason == "file missing"


def test_no_file_path_is_unknown():
    """If there is no file path at all then it says so rather than guessing."""
    q = classify(None, DURATION_MS, 1000)
    assert q.venue is None and "no file path" in q.reason


def test_unrecognised_container_is_unknown():
    """If the extension is not a known audio container then it is UNKNOWN."""
    q = classify("notes.txt", DURATION_MS, _bytes_for(320))
    assert q.venue is None and ".txt" in q.reason


# ----- real stat path + payload shape ----------------------------------------
def test_size_is_stat_ed_when_not_supplied(tmp_path):
    """If size_bytes is omitted then the real file size drives the rung."""
    path = _write(tmp_path, "real.mp3", _bytes_for(320))
    assert classify(path, DURATION_MS).venue.key == "warehouse"


def test_as_dict_carries_everything_the_badge_needs():
    """If the UI renders a badge then venue/label/rank/of/blurb/kbps/container are present."""
    d = classify("x.mp3", DURATION_MS, _bytes_for(320)).as_dict()
    assert d["venue"] == "warehouse" and d["label"] == "Warehouse"
    assert d["rank"] == 4 and d["of"] == 6
    assert d["kbps"] == 320 and d["container"] == ".mp3"
    assert d["lossless"] is False and d["blurb"]


def test_ladder_is_the_six_rungs_ascending():
    """If the legend asks for the ladder then it gets 6 rungs, rank 0..5 in order."""
    rungs = ladder()
    assert [r["rank"] for r in rungs] == [0, 1, 2, 3, 4, 5]
    assert [r["key"] for r in rungs] == [
        "naughty_step", "lounge", "house_party", "club", "warehouse", "stadium",
    ]
    assert len(rungs) == len(audio_quality.VENUES)
