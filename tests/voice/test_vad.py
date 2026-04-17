"""Unit tests for apps.voice.vad (VOICE-01)."""
from __future__ import annotations

import struct

import pytest

from apps.voice import vad


pytestmark = pytest.mark.requirement("VOICE-01")


def _frame(samples: list[int]) -> bytes:
    return struct.pack(f"<{len(samples)}h", *samples)


def silence_frame(n_samples: int = 480) -> bytes:
    return _frame([0] * n_samples)


def speech_frame(amp: int = 5_000, n_samples: int = 480) -> bytes:
    # Square-ish alternating signal gives a predictable RMS well above
    # the default 300 threshold.
    return _frame([amp if i % 2 == 0 else -amp for i in range(n_samples)])


class TestAmplitudeVad:
    def test_speech_detected(self):
        v = vad.AmplitudeVad(rms_threshold=300)
        assert v.is_speech(speech_frame(), 16_000) is True

    def test_silence_rejected(self):
        v = vad.AmplitudeVad(rms_threshold=300)
        assert v.is_speech(silence_frame(), 16_000) is False

    def test_empty_frame(self):
        v = vad.AmplitudeVad()
        assert v.is_speech(b"", 16_000) is False


class TestCollectUtterance:
    def test_cuts_on_silence_tail(self):
        v = vad.AmplitudeVad(rms_threshold=300)
        # 5 speech frames, then 10 silence frames at 30 ms = 300 ms tail.
        stream = [speech_frame()] * 5 + [silence_frame()] * 10
        result = vad.collect_utterance(
            stream, v, sample_rate_hz=16_000, tail_ms=300, frame_ms=30
        )
        assert result.ended_by == "silence"
        # 5 speech + at least 10 silence frames accumulated before break.
        assert len(result.frames) >= 15
        assert result.silence_tail_ms >= 300

    def test_hits_max_length(self):
        v = vad.AmplitudeVad()
        # Continuous speech 1 s = ~33 frames of 30 ms at 16 kHz.
        stream = [speech_frame()] * 200
        result = vad.collect_utterance(
            stream, v, sample_rate_hz=16_000, tail_ms=300, max_utterance_ms=500, frame_ms=30
        )
        assert result.ended_by == "max_length"
        assert result.duration_ms >= 500

    def test_stream_ends_naturally(self):
        v = vad.AmplitudeVad()
        # Iterable of speech frames that ends before silence tail.
        stream = [speech_frame()] * 3
        result = vad.collect_utterance(
            stream, v, sample_rate_hz=16_000, tail_ms=300, frame_ms=30
        )
        assert result.ended_by == "stream_end"

    def test_silence_before_speech_does_not_trigger_cut(self):
        v = vad.AmplitudeVad(rms_threshold=300)
        stream = [silence_frame()] * 20 + [speech_frame()] * 3
        result = vad.collect_utterance(
            stream, v, sample_rate_hz=16_000, tail_ms=300, frame_ms=30
        )
        assert result.ended_by == "stream_end"


class TestMakeVad:
    def test_amplitude_env(self):
        v = vad.make_vad(env={"VAD_BACKEND": "amplitude", "VOICE_VAD_RMS_THRESHOLD": "500"})
        assert isinstance(v, vad.AmplitudeVad)
        assert v.rms_threshold == 500

    def test_unknown_backend(self):
        with pytest.raises(ValueError):
            vad.make_vad(env={"VAD_BACKEND": "random"})
