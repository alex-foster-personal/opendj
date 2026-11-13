from __future__ import annotations

import array
import math
import sys
import wave
from pathlib import Path

from apps.webui.frontend.tests.e2e.support.deckload_fixture_model import (
    BAR_DOWNBEAT_PULSE_MULTIPLIER,
    BAR_ORDINARY_PULSE_MULTIPLIER,
    CHANNELS,
    FIXTURE_TRACKS,
    FixtureTrack,
    KICK_MS,
    PATTERN_BACKBEAT,
    PULSE_HZ,
    PULSE_MS,
    PULSE_PEAK,
    SAMPLE_RATE_HZ,
    SAMPLE_WIDTH_BYTES,
    SNARE_MS,
    SNARE_PEAK,
    TONE_HZ,
    TONE_PEAK,
)

# ----- audio generation ------------------------------------------------------
def _pulse_envelope(sample_index: int, beat_period_samples: float) -> float:
    """Linear-decay envelope for the transient that starts on each beat."""
    pulse_len = SAMPLE_RATE_HZ * PULSE_MS / 1000.0
    since_beat = sample_index % beat_period_samples
    if since_beat >= pulse_len:
        return 0.0
    return 1.0 - (since_beat / pulse_len)


def _noise(sample_index: int) -> float:
    """Deterministic white noise in [-1, 1): the same bytes on every host."""
    # splitmix32-style integer hash of the index (a plain LCG of the index is
    # linear in it, which is a sawtooth, not noise).
    state = (sample_index + 0x9E37_79B9) & 0xFFFF_FFFF
    state = ((state ^ (state >> 16)) * 0x85EB_CA6B) & 0xFFFF_FFFF
    state = ((state ^ (state >> 13)) * 0xC2B2_AE35) & 0xFFFF_FFFF
    state ^= state >> 16
    return state / float(0x8000_0000) - 1.0


def _backbeat_value(sample_index: int, beat_period_samples: float) -> float:
    """Kick on beats 1 and 3 (beat 1 louder), noise snare on 2 and 4."""
    beat_index = int(sample_index / beat_period_samples)
    since_beat = sample_index - beat_index * beat_period_samples
    position = beat_index % 4
    if position in (0, 2):
        length = SAMPLE_RATE_HZ * KICK_MS / 1000.0
        if since_beat >= length:
            return 0.0
        env = 1.0 - since_beat / length
        sweep_hz = PULSE_HZ * 0.6 * (1.0 + env)
        peak = 0.9 if position == 0 else 0.6
        return peak * env * math.sin(2.0 * math.pi * sweep_hz * since_beat / SAMPLE_RATE_HZ)
    length = SAMPLE_RATE_HZ * SNARE_MS / 1000.0
    if since_beat >= length:
        return 0.0
    return SNARE_PEAK * (1.0 - since_beat / length) * _noise(sample_index)


def write_tone_and_pulse_wav(path: Path, track: FixtureTrack) -> int:
    """Write one stereo 16-bit fixture file. Returns the PCM frame count.

    A continuous TONE_HZ sine (the thing the spectral assertions measure) plus
    a PULSE_HZ transient on every beat (so the file behaves like a track with
    onsets rather than a test tone).
    """
    frame_count = round(track.seconds * SAMPLE_RATE_HZ)
    beat_period_samples = SAMPLE_RATE_HZ * 60.0 / track.bpm
    peak = float(2**15 - 1)
    tone_step = 2.0 * math.pi * TONE_HZ / SAMPLE_RATE_HZ
    samples = array.array("h")
    for n in range(frame_count):
        value = TONE_PEAK * math.sin(tone_step * n)
        if track.pattern == PATTERN_BACKBEAT:
            value += _backbeat_value(n, beat_period_samples)
            sample = int(max(-1.0, min(1.0, value)) * peak)
            samples.append(sample)
            samples.append(sample)
            continue
        env = _pulse_envelope(n, beat_period_samples)
        if env > 0.0:
            beat_index = int(n / beat_period_samples)
            if beat_index % 4 == 0:
                pulse_peak = PULSE_PEAK * BAR_DOWNBEAT_PULSE_MULTIPLIER
                pulse_hz = PULSE_HZ * 1.8
            else:
                pulse_peak = PULSE_PEAK * BAR_ORDINARY_PULSE_MULTIPLIER
                pulse_hz = PULSE_HZ
            pulse_step = 2.0 * math.pi * pulse_hz / SAMPLE_RATE_HZ
            value += pulse_peak * env * math.sin(pulse_step * n)
        sample = int(max(-1.0, min(1.0, value)) * peak)
        samples.append(sample)
        samples.append(sample)
    if sys.byteorder != "little":
        samples.byteswap()
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(CHANNELS)
        handle.setsampwidth(SAMPLE_WIDTH_BYTES)
        handle.setframerate(SAMPLE_RATE_HZ)
        handle.writeframes(samples.tobytes())
    return frame_count


def _wav_frame_count(path: Path) -> int:
    with wave.open(str(path), "rb") as handle:
        return handle.getnframes()


def ensure_audio(
    audio_dir: Path, tracks: tuple[FixtureTrack, ...] = FIXTURE_TRACKS
) -> list[Path]:
    """Generate every missing/short fixture wav. Returns the file list.

    ``tracks`` defaults to the 2-track library the five other e2e configs
    share; ``build_autoplay_chain`` passes its own longer tuple so both
    callers ensure audio through exactly this one code path.
    """
    written: list[Path] = []
    for track in tracks:
        path = audio_dir / track.filename
        expected = round(track.seconds * SAMPLE_RATE_HZ)
        if path.is_file() and _wav_frame_count(path) == expected:
            written.append(path)
            continue
        actual = write_tone_and_pulse_wav(path, track)
        if actual != expected:
            raise SystemExit(
                f"[ERROR] {path} wrote {actual} frames, expected {expected}"
            )
        written.append(path)
    for path in written:
        if _wav_frame_count(path) <= 0:
            raise SystemExit(f"[ERROR] {path} has no PCM frames")
    return written
