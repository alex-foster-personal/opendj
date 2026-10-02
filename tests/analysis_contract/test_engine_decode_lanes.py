"""The beatgrid and key lanes read an m4a through the engine (NAE-22).

Neither lane's own reader opens an m4a without ffmpeg (Beat This! reads via
torchaudio, soundfile and madmom; the key lane via librosa and audioread), and
the shipped app has no ffmpeg.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from apps.analysis.backends import own_beatgrid, own_key
from apps.analysis.backends.base import BackendNotAvailable, TrackUnreadable
from apps.shared.engine_decode import (
    BIN_ENV,
    EngineDecoderUnavailable,
    resolve_engine_decoder,
)

_M4A = (
    Path(__file__).resolve().parents[2]
    / "apps/audio-engine/tests/fixtures/audio/click-250ms-aac.m4a"
)


@pytest.fixture(autouse=True)
def _engine() -> None:
    try:
        resolve_engine_decoder()
    except EngineDecoderUnavailable as exc:
        pytest.skip(f"no odj-audio build in this checkout: {exc}")


def test_the_runner_reads_a_wav_at_the_same_path_every_run(tmp_path: Path) -> None:
    soundfile = pytest.importorskip("soundfile")
    track = tmp_path / "Song.m4a"
    shutil.copy(_M4A, track)
    first = own_beatgrid._runner_input(track, tmp_path / "decode")
    first.unlink()
    second = own_beatgrid._runner_input(track, tmp_path / "decode")
    # The runner names its activation file after this path, so it must not move.
    assert first == second and second.name == "Song.wav"
    assert soundfile.info(str(second)).frames == 44100


def test_the_runner_wav_is_deleted_after_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Path] = []

    def _fake_run(runner_input: Path, *_args: object, **_kwargs: object) -> dict:
        assert runner_input.is_file()
        seen.append(runner_input)
        raise RuntimeError("runner fault")

    monkeypatch.setattr(own_beatgrid, "ENGINE_DECODE_DIR", tmp_path / "decode")
    monkeypatch.setattr(own_beatgrid, "_run_runner_on", _fake_run)
    with pytest.raises(RuntimeError, match="runner fault"):
        own_beatgrid.run_runner(_M4A, tmp_path / "ckpt", device="cpu")
    assert len(seen) == 1 and not seen[0].exists()


def test_the_runner_reads_an_mp3_as_itself(tmp_path: Path) -> None:
    track = tmp_path / "Song.mp3"
    assert own_beatgrid._runner_input(track, tmp_path) == track


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
            own_beatgrid._runner_input(junk, tmp_path)
        else:
            own_key._decode(junk)


@pytest.mark.parametrize("lane", ["beatgrid", "key"])
def test_a_missing_engine_stops_the_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lane: str
) -> None:
    monkeypatch.setenv(BIN_ENV, str(tmp_path / "absent"))
    with pytest.raises(BackendNotAvailable):
        if lane == "beatgrid":
            own_beatgrid._runner_input(_M4A, tmp_path)
        else:
            own_key._decode(_M4A)
