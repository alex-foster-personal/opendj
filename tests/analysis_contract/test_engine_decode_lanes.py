"""The beatgrid and key lanes read an m4a through the engine (NAE-22).

Neither lane's own reader opens an m4a without ffmpeg (Beat This! reads via
torchaudio, soundfile and madmom; the key lane via librosa and audioread), and
the shipped app has no ffmpeg.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from contextlib import AbstractContextManager
from pathlib import Path

import pytest

from apps.analysis.backends import own_beatgrid, own_beatgrid_input, own_key
from apps.analysis.backends.base import BackendNotAvailable, TrackUnreadable, TrackVanished
from apps.shared.engine_decode import BIN_ENV

_M4A = (
    Path(__file__).resolve().parents[2]
    / "apps/audio-engine/tests/fixtures/audio/click-250ms-aac.m4a"
)


# Skips as UNAVAILABLE on a host with no odj-audio build; ci.yml runs this
# file where the engine was just built, where a missing build fails instead.
pytestmark = pytest.mark.requires_canonical_decode



def _input(track: Path, decode_dir: Path) -> AbstractContextManager[Path]:
    return own_beatgrid_input.runner_input(track, decode_dir=decode_dir)


def _wavs(folder: Path) -> list[Path]:
    return list(folder.rglob("*.wav"))


def test_the_runner_reads_a_wav_at_the_same_path_every_run(tmp_path: Path) -> None:
    soundfile = pytest.importorskip("soundfile")
    track = tmp_path / "Song.m4a"
    shutil.copy(_M4A, track)
    seen = []
    for _ in range(2):
        with _input(track, tmp_path / "decode") as wav:
            assert soundfile.info(str(wav)).frames == 44100
            seen.append(wav)
    # The runner names its activation file after this path, so it must not move.
    assert seen[0] == seen[1] and seen[0].name == "Song.wav"
    assert not _wavs(tmp_path / "decode")


def test_the_runner_reads_an_mp3_as_itself(tmp_path: Path) -> None:
    track = tmp_path / "Song.mp3"
    with _input(track, tmp_path) as runner_input:
        assert runner_input == track


def test_a_second_run_of_the_same_track_waits_rather_than_clobbering(tmp_path: Path) -> None:
    track = tmp_path / "Song.m4a"
    shutil.copy(_M4A, track)
    with _input(track, tmp_path / "decode") as wav:
        before = wav.read_bytes()
        with (
            pytest.raises(TrackVanished, match="another run"),
            _input(track, tmp_path / "decode"),
        ):
            pass
        # The refused second run left the first run's WAV untouched.
        assert wav.read_bytes() == before
    # ...and once the first run is done, the track is free again.
    with _input(track, tmp_path / "decode") as wav:
        assert wav.is_file()


def test_a_run_that_died_holding_the_lock_does_not_block_the_track(tmp_path: Path) -> None:
    track = tmp_path / "Song.m4a"
    shutil.copy(_M4A, track)
    with _input(track, tmp_path / "decode") as wav:
        lock = wav.parent / ".lock"
    holder = subprocess.Popen(  # a real second process holding the real lock
        [sys.executable, "-c", (
            "import os, sys, time\n"
            "from apps.analysis.backends.own_beatgrid_input import _try_lock\n"
            "fd = os.open(sys.argv[1], os.O_RDWR)\n"
            "assert _try_lock(fd)\n"
            "print('held', flush=True)\n"
            "time.sleep(600)\n"
        ), str(lock)],
        stdout=subprocess.PIPE, text=True, cwd=Path(__file__).resolve().parents[2],
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
        with pytest.raises(TrackVanished, match="another run"), _input(track, tmp_path / "decode"):
            pass
    finally:
        holder.kill()
        holder.wait()
    # The OS dropped the dead holder's lock; no stale-lock takeover was needed.
    with _input(track, tmp_path / "decode") as wav:
        assert wav.is_file()


def test_the_runner_wav_is_deleted_when_the_runner_cannot_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Configuration, not a fake: the runner interpreter really does not exist.
    monkeypatch.setenv("MDT_BEATGRID_ACTIVATIONS_DIR", str(tmp_path / "activations"))
    monkeypatch.setenv(own_beatgrid.RUNNER_PYTHON_ENV, str(tmp_path / "no-python"))
    with pytest.raises(BackendNotAvailable, match="could not launch"):
        own_beatgrid.run_runner(_M4A, tmp_path / "ckpt", device="cpu")
    decode_dir = tmp_path / "engine-decode"
    assert decode_dir.is_dir(), "the engine decode never ran, so this proves nothing"
    assert not _wavs(decode_dir)


def test_the_key_lane_reads_an_m4a_through_the_engine() -> None:
    from apps.shared import engine_decode

    samples, rate = own_key._decode(_M4A)
    pcm, engine_rate, _ = engine_decode.decode_f32(_M4A, mono=True)
    # Exactly the engine's samples: 44100 frames with the AAC priming dropped,
    # where librosa through ffmpeg returns 45056 with it kept.
    assert rate == engine_rate == 44100
    assert samples.tobytes() == pcm and samples.shape == (44100,)
    assert abs(int(abs(samples).argmax()) - 11025) < 64


@pytest.mark.parametrize("lane", ["beatgrid", "key"])
def test_an_unreadable_m4a_is_one_bad_track(tmp_path: Path, lane: str) -> None:
    junk = tmp_path / "junk.m4a"
    junk.write_bytes(b"not audio" * 256)
    with pytest.raises(TrackUnreadable, match="could not decode"):
        if lane == "beatgrid":
            with _input(junk, tmp_path / "decode"):
                pass
        else:
            own_key._decode(junk)
    if lane == "beatgrid":
        assert not _wavs(tmp_path / "decode")


@pytest.mark.parametrize("lane", ["beatgrid", "key"])
def test_a_missing_engine_stops_the_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lane: str
) -> None:
    monkeypatch.setenv(BIN_ENV, str(tmp_path / "absent"))
    with pytest.raises(BackendNotAvailable):
        if lane == "beatgrid":
            with _input(_M4A, tmp_path / "decode"):
                pass
        else:
            own_key._decode(_M4A)
