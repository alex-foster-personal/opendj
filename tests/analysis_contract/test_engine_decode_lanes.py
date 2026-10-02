"""The beatgrid and key lanes read an m4a through the engine (NAE-22).

Neither lane's own reader opens an m4a without ffmpeg (Beat This! reads via
torchaudio, soundfile and madmom; the key lane via librosa and audioread), and
the shipped app has no ffmpeg.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest

from apps.analysis.backends import own_beatgrid, own_key
from apps.analysis.backends.base import BackendNotAvailable, TrackUnreadable, TrackVanished
from apps.shared.engine_decode import BIN_ENV

_M4A = (
    Path(__file__).resolve().parents[2]
    / "apps/audio-engine/tests/fixtures/audio/click-250ms-aac.m4a"
)


# Skips as UNAVAILABLE on a host with no odj-audio build; ci.yml runs this
# file where the engine was just built, where a missing build fails instead.
pytestmark = pytest.mark.requires_canonical_decode


def test_the_runner_reads_a_wav_at_the_same_path_every_run(tmp_path: Path) -> None:
    soundfile = pytest.importorskip("soundfile")
    track = tmp_path / "Song.m4a"
    shutil.copy(_M4A, track)
    seen = []
    for _ in range(2):
        with own_beatgrid._runner_input(track, tmp_path / "decode") as wav:
            assert soundfile.info(str(wav)).frames == 44100
            seen.append(wav)
    # The runner names its activation file after this path, so it must not move.
    assert seen[0] == seen[1] and seen[0].name == "Song.wav"
    # Nothing is left behind: no WAV, no lock.
    assert not [p for p in (tmp_path / "decode").rglob("*") if p.is_file()]


def test_the_runner_reads_an_mp3_as_itself(tmp_path: Path) -> None:
    track = tmp_path / "Song.mp3"
    with own_beatgrid._runner_input(track, tmp_path) as runner_input:
        assert runner_input == track


def test_a_second_run_of_the_same_track_waits_rather_than_clobbering(tmp_path: Path) -> None:
    track = tmp_path / "Song.m4a"
    shutil.copy(_M4A, track)
    with own_beatgrid._runner_input(track, tmp_path / "decode") as wav:
        before = wav.read_bytes()
        with (
            pytest.raises(TrackVanished, match="another run"),
            own_beatgrid._runner_input(track, tmp_path / "decode"),
        ):
            pass
        # The first run's WAV and lock are untouched by the refused second one.
        assert wav.read_bytes() == before
        assert (wav.parent / ".lock").exists()


def test_a_lock_left_by_a_crashed_run_is_taken_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    track = tmp_path / "Song.m4a"
    shutil.copy(_M4A, track)
    with own_beatgrid._runner_input(track, tmp_path / "decode") as wav:
        lock = wav.parent / ".lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.touch()
    old = time.time() - own_beatgrid._stale_after_s() - 60
    os.utime(lock, (old, old))
    with own_beatgrid._runner_input(track, tmp_path / "decode") as wav:
        assert wav.is_file()


def test_the_runner_wav_is_deleted_when_the_runner_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Path] = []

    def _fake_run(runner_input: Path, *_args: object, **_kwargs: object) -> dict:
        assert runner_input.is_file()
        seen.append(runner_input)
        raise RuntimeError("runner fault")

    monkeypatch.setattr(own_beatgrid, "_engine_decode_dir", lambda: tmp_path / "decode")
    monkeypatch.setattr(own_beatgrid, "_run_runner_on", _fake_run)
    with pytest.raises(RuntimeError, match="runner fault"):
        own_beatgrid.run_runner(_M4A, tmp_path / "ckpt", device="cpu")
    assert len(seen) == 1 and not seen[0].exists()
    assert not (seen[0].parent / ".lock").exists()


def test_the_key_lane_decodes_an_m4a_without_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    import librosa

    def _no_ffmpeg(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("librosa (and so ffmpeg) was asked to read an m4a")

    monkeypatch.setattr(librosa, "load", _no_ffmpeg)
    samples, rate = own_key._decode(_M4A)
    assert (rate, samples.shape, str(samples.dtype)) == (44100, (44100,), "float32")
    assert abs(int(abs(samples).argmax()) - 11025) < 64


@pytest.mark.parametrize("lane", ["beatgrid", "key"])
def test_an_unreadable_m4a_is_one_bad_track(tmp_path: Path, lane: str) -> None:
    junk = tmp_path / "junk.m4a"
    junk.write_bytes(b"not audio" * 256)
    with pytest.raises(TrackUnreadable, match="could not decode"):
        if lane == "beatgrid":
            with own_beatgrid._runner_input(junk, tmp_path / "decode"):
                pass
        else:
            own_key._decode(junk)
    if lane == "beatgrid":
        assert not [p for p in (tmp_path / "decode").rglob("*") if p.is_file()]


@pytest.mark.parametrize("lane", ["beatgrid", "key"])
def test_a_missing_engine_stops_the_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lane: str
) -> None:
    monkeypatch.setenv(BIN_ENV, str(tmp_path / "absent"))
    with pytest.raises(BackendNotAvailable):
        if lane == "beatgrid":
            with own_beatgrid._runner_input(_M4A, tmp_path / "decode"):
                pass
        else:
            own_key._decode(_M4A)
