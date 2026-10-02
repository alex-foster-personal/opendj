"""Own waveforms without ffmpeg: the engine decoder and how one is chosen.

``odj-audio waveform`` (apps/audio-engine/src/waveform.rs) is the default
producer of the own tri-band peaks; ffmpeg is the fallback. The choice is a
pure function of ``MDT_WAVEFORM_DECODER``, what is installed and the file's
suffix, and it is part of the local cache key.

  - [if] the engine and ffmpeg disagree by more than 1 of 255 on a 44.1 kHz stereo file [then] fail, [else stop].
  - [if] a misspelled decoder setting picks a decoder anyway [then] fail, [else stop].
  - [if] a forced decoder that is missing silently falls back to the other [then] fail, [else stop].
  - [if] an AAC file goes to the engine while ffmpeg is present [then] fail, [else stop].
  - [if] an engine build without the waveform command is chosen [then] fail, [else stop].
"""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

import numpy as np
import pytest

from apps.analysis_waveform import decode

pytestmark = pytest.mark.requirement("NATIVE-21")

RATE = 44_100


def _engine_or_skip() -> None:
    try:
        decode.resolve_engine()
    except decode.LocalDecodeUnavailable as exc:
        pytest.skip(f"no odj-audio build here: {exc.reason}")


def _write_stereo_mix(path: Path, seconds: float = 4.0) -> None:
    """Different content per side and per band, so a wrong downmix gain, a
    swapped band or a dropped channel all move the peaks."""
    frames = bytearray()
    for i in range(int(RATE * seconds)):
        t = i / RATE
        swell = 0.5 + 0.5 * math.sin(2 * math.pi * 0.7 * t)
        left = 0.45 * math.sin(2 * math.pi * 55 * t) + 0.2 * swell * math.sin(2 * math.pi * 9_000 * t)
        right = 0.35 * swell * math.sin(2 * math.pi * 880 * t) + 0.1 * math.sin(2 * math.pi * 3_000 * t)
        frames += struct.pack("<hh", int(32767 * left), int(32767 * right))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(bytes(frames))


@pytest.mark.requires_ffmpeg
def test_the_engine_matches_ffmpeg_to_one_step_on_a_stereo_file(tmp_path: Path) -> None:
    _engine_or_skip()
    source = tmp_path / "mix.wav"
    _write_stereo_mix(source)
    engine = decode.decode_peaks_from("engine", source).astype(int)
    ffmpeg = decode.decode_peaks_from("ffmpeg", source).astype(int)
    assert engine.shape == ffmpeg.shape
    # Positive control: every band carries real signal, so agreement is not
    # two decoders agreeing on silence.
    assert (ffmpeg.max(axis=0) > 30).all(), ffmpeg.max(axis=0)
    worst = np.abs(engine - ffmpeg).max(axis=0)
    assert (worst <= 1).all(), f"per-band worst |engine - ffmpeg|: {worst}"


def test_the_engine_reports_the_rate_it_filtered_at(tmp_path: Path) -> None:
    _engine_or_skip()
    source = tmp_path / "mix.wav"
    _write_stereo_mix(source, seconds=1.0)
    peaks, rate = decode.decode_peaks_measured("engine", source)
    assert rate == RATE
    assert peaks.shape == (150, 3)


def test_a_misspelled_setting_is_refused_not_guessed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(decode.DECODER_ENV, "rust")
    with pytest.raises(decode.LocalDecodeUnavailable, match="not one of"):
        decode.select_decoder(tmp_path / "a.wav")


def test_a_forced_engine_that_is_missing_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(decode.DECODER_ENV, "engine")
    monkeypatch.setenv("ODJ_AUDIO_BIN", str(tmp_path / "no-such-odj-audio"))
    with pytest.raises(decode.LocalDecodeUnavailable, match="odj-audio unavailable"):
        decode.select_decoder(tmp_path / "a.wav")


def test_an_engine_build_without_the_waveform_command_is_not_chosen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale cargo build (seen in a reused CI workspace) prints a version
    with no ``commands``; ``auto`` must not pick it, and a forced engine names
    why."""
    stale = tmp_path / "odj-audio"
    stale.write_text('#!/bin/sh\necho \'{"engine":"odj-audio 0.1.0","protocol":1}\'\n')
    stale.chmod(0o755)
    monkeypatch.setenv("ODJ_AUDIO_BIN", str(stale))
    monkeypatch.setenv(decode.DECODER_ENV, "engine")
    with pytest.raises(decode.LocalDecodeUnavailable, match="no waveform command"):
        decode.select_decoder(tmp_path / "a.wav")
    # Control: the same script listing the command is accepted.
    capable = tmp_path / "capable" / "odj-audio"
    capable.parent.mkdir()
    capable.write_text('#!/bin/sh\necho \'{"engine":"odj-audio 0.1.0","protocol":1,"commands":["waveform"]}\'\n')
    capable.chmod(0o755)
    monkeypatch.setenv("ODJ_AUDIO_BIN", str(capable))
    assert decode.select_decoder(tmp_path / "a.wav") == "engine"


@pytest.mark.requires_ffmpeg
def test_auto_sends_aac_to_ffmpeg_and_the_rest_to_the_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _engine_or_skip()
    monkeypatch.delenv(decode.DECODER_ENV, raising=False)
    assert decode.select_decoder(tmp_path / "a.m4a") == "ffmpeg"
    assert decode.select_decoder(tmp_path / "a.flac") == "engine"
    # With the engine gone, auto still decodes the rest through ffmpeg.
    monkeypatch.setenv("ODJ_AUDIO_BIN", str(tmp_path / "no-such-odj-audio"))
    assert decode.select_decoder(tmp_path / "a.flac") == "ffmpeg"
