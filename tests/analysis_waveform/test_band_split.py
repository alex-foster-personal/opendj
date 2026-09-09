"""The three bands are three MEASURED envelopes, in the right columns (NATIVE-06).

The subject is a real ffmpeg decode of a real three-tone WAV written here, not a
patched decoder: the defect this guards against is a filter graph whose channels
come back in the wrong order or whose crossovers do not separate anything, and
neither is visible to anything that does not run ffmpeg.

The tones are chosen so each one is unambiguously inside ONE band and far from
both crossovers (60 Hz, 1 kHz, 12 kHz against edges at 200 Hz and 4 kHz), and
they are written into SEPARATE, non-overlapping seconds of the file. Overlapping
them would make every band loud at every column and the assertion below would
pass no matter which channel carried which tone - the exact "check that cannot
fail" this file exists to avoid.

  - [if] a band-split tone lands in a band that is not its own [then] fail, [else stop].
  - [if] the decoder emits fewer or more than three bands [then] fail, [else stop].
  - [if] a garbage file yields a waveform instead of not_decoded [then] fail, [else stop].
"""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

import numpy as np
import pytest

from apps.analysis_waveform import decode

pytestmark = pytest.mark.requirement("NATIVE-06")

SAMPLE_RATE_HZ = 44_100
TONE_S = 2.0
# One tone per band, each at least 1.7 octaves clear of the nearest crossover.
BAND_TONES_HZ: tuple[float, float, float] = (60.0, 1_000.0, 12_000.0)
# 24 dB/octave, so the nearest leak (12 kHz into the mid band's 4 kHz lowpass,
# 1.58 octaves up) sits near -38 dB = 0.013 of full scale. 0.10 is a wide,
# deliberately un-tuned bar: it is far above the predicted leak and far below
# the >= 0.5 an in-band tone reaches, so a swapped channel cannot squeak past.
IN_BAND_MIN = 0.5
OUT_OF_BAND_MAX = 0.10


def _write_sequential_tones(path: Path, tones_hz: tuple[float, ...]) -> None:
    """A real WAV: each tone alone for TONE_S seconds, in order, near full scale.

    30000/32767 = 0.916, so an in-band tone has room to clear IN_BAND_MIN even
    after a filter's passband droop; Butterworth sections have no gain above 1,
    so nothing here can clip on the way out.
    """
    frames = bytearray()
    for hz in tones_hz:
        for i in range(int(SAMPLE_RATE_HZ * TONE_S)):
            frames += struct.pack(
                "<h", int(30000 * math.sin(2 * math.pi * hz * i / SAMPLE_RATE_HZ))
            )
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE_HZ)
        handle.writeframes(bytes(frames))


def _segment_peak(peaks: np.ndarray, index: int) -> np.ndarray:
    """Peak per band over the middle half of tone ``index``'s own seconds.

    The middle half only: a filter rings across a tone boundary, and a column
    that straddles two tones carries both.
    """
    columns_per_tone = int(TONE_S * decode.DETAIL_COLUMNS_PER_S)
    start = index * columns_per_tone + columns_per_tone // 4
    stop = (index + 1) * columns_per_tone - columns_per_tone // 4
    return peaks[start:stop].max(axis=0) / 255.0


@pytest.mark.requires_ffmpeg
def test_each_tone_shows_in_its_own_band_only(tmp_path: Path) -> None:
    source = tmp_path / "three tones.wav"
    _write_sequential_tones(source, BAND_TONES_HZ)

    peaks = decode.decode_peaks(source)
    assert peaks.ndim == 2 and peaks.shape[1] == decode.BAND_COUNT, (
        f"expected (n, 3) tri-band columns, got {peaks.shape}"
    )
    expected_columns = int(len(BAND_TONES_HZ) * TONE_S * decode.DETAIL_COLUMNS_PER_S)
    assert abs(peaks.shape[0] - expected_columns) <= 2, (
        f"{peaks.shape[0]} columns for {len(BAND_TONES_HZ) * TONE_S:.0f}s at "
        f"{decode.DETAIL_COLUMNS_PER_S} columns/s (expected ~{expected_columns})"
    )

    for index, (band, hz) in enumerate(zip(decode.BAND_NAMES, BAND_TONES_HZ, strict=True)):
        levels = _segment_peak(peaks, index)
        assert levels[index] >= IN_BAND_MIN, (
            f"{hz:.0f} Hz reached only {levels[index]:.3f} in its own '{band}' band; "
            f"all bands were {dict(zip(decode.BAND_NAMES, levels.round(3), strict=True))}"
        )
        for other in range(decode.BAND_COUNT):
            if other == index:
                continue
            assert levels[other] <= OUT_OF_BAND_MAX, (
                f"{hz:.0f} Hz leaked {levels[other]:.3f} into "
                f"'{decode.BAND_NAMES[other]}', which is not its band; "
                f"all bands were {dict(zip(decode.BAND_NAMES, levels.round(3), strict=True))}"
            )


@pytest.mark.requires_ffmpeg
def test_a_silent_band_is_measured_as_silent_not_copied_from_a_loud_one(
    tmp_path: Path,
) -> None:
    """The control the previous test cannot supply on its own.

    A decoder that duplicated ONE envelope across three columns would pass every
    in-band assertion above for whichever tone it happened to carry. A file with
    only a low tone must therefore leave mid and high at the floor - if they
    track the low band, the bands are copied, not measured.
    """
    source = tmp_path / "low only.wav"
    _write_sequential_tones(source, (BAND_TONES_HZ[0],))

    levels = decode.decode_peaks(source).max(axis=0) / 255.0
    assert levels[0] >= IN_BAND_MIN, f"the low tone itself only reached {levels[0]:.3f}"
    assert levels[1] <= OUT_OF_BAND_MAX and levels[2] <= OUT_OF_BAND_MAX, (
        "mid and high must be measured silence for a low-only file, not a copy "
        f"of the low band; got {dict(zip(decode.BAND_NAMES, levels.round(3), strict=True))}"
    )


@pytest.mark.requires_ffmpeg
def test_a_garbage_file_is_not_decoded_with_a_stated_reason(tmp_path: Path) -> None:
    source = tmp_path / "not really audio.wav"
    source.write_bytes(b"this is not a RIFF header" * 4096)
    with pytest.raises(decode.LocalDecodeUnavailable) as raised:
        decode.decode_peaks(source)
    assert raised.value.reason, "not_decoded must name a reason, never an empty string"
    assert raised.value.retryable is False, (
        "undecodable bytes are a fact about the file, not a transient queue state"
    )


@pytest.mark.requires_ffmpeg
def test_a_decode_deadline_miss_is_retryable_not_a_verdict_on_the_file(
    tmp_path: Path,
) -> None:
    """The opposite direction to the test above, and the one that is easy to get
    backwards: a timeout marked permanent lets the route cache `not_decoded` for
    an hour over one busy moment.

    A REAL ``DecodeProfile`` with a 1 ms deadline, not a patched constant and
    not a faked decoder: a real ffmpeg is started and really killed.
    """
    source = tmp_path / "long enough.wav"
    _write_sequential_tones(source, BAND_TONES_HZ)
    impatient = decode.DecodeProfile(timeout_s=0.001)
    with pytest.raises(decode.LocalDecodeUnavailable) as raised:
        decode.decode_peaks(source, impatient)
    assert "within" in raised.value.reason, raised.value.reason
    assert raised.value.retryable is True, (
        "a deadline miss describes the host at that moment, not the file"
    )


def test_the_filter_graph_states_the_crossovers_it_was_measured_under() -> None:
    """Every scored number has to carry the graph that produced it (section 4)."""
    graph = decode.band_filter_graph()
    assert f"lowpass=f={decode.CROSSOVER_LOW_HZ}" in graph
    assert f"highpass=f={decode.CROSSOVER_HIGH_HZ}" in graph
    assert graph.count(f"lowpass=f={decode.CROSSOVER_LOW_HZ}") == decode.FILTER_SECTIONS
    # A different real profile produces a different real graph, which is how an
    # experiment round changes the producer without patching anything.
    twelve_db = decode.band_filter_graph(decode.DecodeProfile(filter_sections=1))
    assert twelve_db.count(f"lowpass=f={decode.CROSSOVER_LOW_HZ}") == 1
    assert "amerge=inputs=3" in graph


def test_peak_columns_follow_the_interleaved_pcm_they_reduce() -> None:
    """Band-major arithmetic, with no ffmpeg in sight.

    Column 0 full scale, column 1 silent, column 2 half scale in EVERY frame, so
    a reduction that mixed the channels could not produce this answer.
    """
    frames = struct.pack("<hhh", 32767, 0, 16384) * decode.SAMPLES_PER_COLUMN * 2
    peaks = decode._peak_columns(frames)
    assert peaks.shape == (2, decode.BAND_COUNT)
    assert peaks[:, 0].tolist() == [255, 255]
    assert peaks[:, 1].tolist() == [0, 0]
    assert peaks[:, 2].tolist() == [128, 128]


def test_int16_floor_sample_does_not_wrap_a_column_to_silence() -> None:
    """-32768 has no positive int16 twin; an unclamped abs() would wrap it."""
    frames = struct.pack("<hhh", -32768, -32768, -32768) * 4000
    assert decode._peak_columns(frames).max() == 255


def test_a_short_tail_becomes_one_real_column_never_a_padded_one() -> None:
    whole = struct.pack("<hhh", 32767, 0, 0) * decode.SAMPLES_PER_COLUMN
    tail = struct.pack("<hhh", 0, 32767, 0) * 10
    peaks = decode._peak_columns(whole + tail)
    assert peaks.shape == (2, decode.BAND_COUNT)
    assert peaks[1].tolist() == [0, 255, 0], (
        "the tail column must carry the tail's own samples, not a pad"
    )
