"""Voice Activity Detection (VAD) silence-tail cutter.

Thin, pure-Python wrapper that accepts a stream of 30 ms PCM frames and
returns the speech segment that ended because we saw ``tail_ms`` of
continuous silence (or hit ``max_utterance_ms``).

``webrtcvad`` is the real detector but is imported lazily. For unit
tests we provide an ``AmplitudeVad`` that treats any frame with RMS
above a threshold as speech -- adequate for deterministic tests with
fabricated PCM.
"""
from __future__ import annotations

import math
import os
import struct
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

FRAME_MS: int = 30
# Default silence tail: 300 ms (configurable via $VOICE_VAD_TAIL_MS).
DEFAULT_TAIL_MS: int = 300
DEFAULT_MAX_UTTERANCE_MS: int = 5_000


@runtime_checkable
class Vad(Protocol):
    """Protocol: ``is_speech(frame, sample_rate_hz) -> bool``."""

    def is_speech(self, frame: bytes, sample_rate_hz: int) -> bool:  # pragma: no cover
        ...


class WebRtcVad:
    """webrtcvad wrapper (mode 2 by default = moderately aggressive)."""

    def __init__(self, mode: int = 2) -> None:
        try:
            import webrtcvad  # type: ignore[import-not-found]
        except Exception as exc:  # pragma: no cover - env-specific
            raise RuntimeError(
                "webrtcvad not importable; `pip install webrtcvad`"
            ) from exc
        self._vad = webrtcvad.Vad(mode)

    def is_speech(self, frame: bytes, sample_rate_hz: int) -> bool:  # pragma: no cover
        return bool(self._vad.is_speech(frame, sample_rate_hz))


@dataclass
class AmplitudeVad:
    """Fallback detector for tests + environments without webrtcvad.

    Computes RMS over the int16 samples and compares to ``rms_threshold``.
    Not production-quality but perfectly deterministic for fixtures.
    """

    rms_threshold: float = 300.0

    def is_speech(self, frame: bytes, _sample_rate_hz: int) -> bool:
        n = len(frame) // 2
        if n == 0:
            return False
        samples = struct.unpack(f"<{n}h", frame[: n * 2])
        sq = sum(s * s for s in samples) / n
        rms = math.sqrt(sq)
        return rms >= self.rms_threshold


@dataclass
class VadResult:
    """Speech segment + how it ended."""

    audio: bytes
    ended_by: str  # "silence" | "max_length" | "stream_end"
    duration_ms: int
    silence_tail_ms: int = 0
    frames: list[bytes] = field(default_factory=list)


def collect_utterance(
    frames: Iterable[bytes],
    vad: Vad,
    sample_rate_hz: int = 16_000,
    tail_ms: int = DEFAULT_TAIL_MS,
    max_utterance_ms: int = DEFAULT_MAX_UTTERANCE_MS,
    frame_ms: int = FRAME_MS,
) -> VadResult:
    """Consume ``frames`` until we see ``tail_ms`` of silence or hit max.

    Parameters
    ----------
    frames
        Iterable of 30 ms PCM int16 frames (at ``sample_rate_hz``).
    vad
        Any object satisfying the ``Vad`` protocol.
    tail_ms
        Consecutive silence duration to end the utterance.
    max_utterance_ms
        Hard cap to avoid runaway recording.
    frame_ms
        Duration of each incoming frame. Default 30 ms.

    Returns
    -------
    VadResult with the accumulated speech bytes and why it ended.
    """
    accumulated: list[bytes] = []
    silence_run_ms = 0
    total_ms = 0
    saw_speech = False
    ended_by = "stream_end"

    for frame in frames:
        accumulated.append(frame)
        total_ms += frame_ms
        if vad.is_speech(frame, sample_rate_hz):
            silence_run_ms = 0
            saw_speech = True
        else:
            if saw_speech:
                silence_run_ms += frame_ms
                if silence_run_ms >= tail_ms:
                    ended_by = "silence"
                    break
        if total_ms >= max_utterance_ms:
            ended_by = "max_length"
            break

    audio = b"".join(accumulated)
    return VadResult(
        audio=audio,
        ended_by=ended_by,
        duration_ms=total_ms,
        silence_tail_ms=silence_run_ms,
        frames=accumulated,
    )


def make_vad(env: dict[str, str] | None = None) -> Vad:
    """Factory -- env ``VAD_BACKEND`` picks real vs amplitude stub."""
    env = env if env is not None else dict(os.environ)
    backend = (env.get("VAD_BACKEND") or "webrtc").lower()
    if backend == "amplitude":
        try:
            threshold = float(env.get("VOICE_VAD_RMS_THRESHOLD", 300.0))
        except ValueError:
            threshold = 300.0
        return AmplitudeVad(rms_threshold=threshold)
    if backend == "webrtc":
        try:
            mode = int(env.get("VOICE_VAD_MODE", 2))
        except ValueError:
            mode = 2
        return WebRtcVad(mode=mode)
    raise ValueError(f"Unknown VAD_BACKEND={backend!r}")
