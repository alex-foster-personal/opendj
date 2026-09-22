"""Mic capture + device selection for the voice pipeline.

Thin wrapper over ``sounddevice`` so the rest of the pipeline only ever
sees 16 kHz mono int16 PCM frames. ``sounddevice`` is imported lazily so
the module can be imported (and most of its helpers unit-tested) on a
machine where PortAudio / the wheel is not installed.

Design notes
------------
* 16 kHz mono: required by both openWakeWord and whisper.cpp (and
  handled by sounddevice via internal resampling if the device sample
  rate differs).
* Built-in mic warning: voice-feasibility.md section 10.1 calls mic
  quality the #1 reliability factor. We warn loudly when the selected
  device looks like a built-in laptop mic.
* Device selection: ``$VOICE_INPUT_DEVICE`` (int index) wins; else
  fall back to ``sd.default.device[0]``.
* Output: we do not route TTS here. ``tts.py`` owns that.
"""
from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

SAMPLE_RATE_HZ: int = 16_000
CHANNELS: int = 1
# 30 ms frame at 16 kHz = 480 samples; matches webrtcvad + openwakeword
# expectations.
FRAME_SAMPLES: int = 480

BUILTIN_MIC_HINTS: tuple[str, ...] = (
    "macbook",
    "built-in",
    "builtin",
    "internal",
)


@dataclass(frozen=True)
class DeviceInfo:
    """Minimal, serialisable device description."""

    index: int
    name: str
    sample_rate_hz: float
    max_input_channels: int


def _sounddevice():
    """Lazy import so module load works without PortAudio installed."""
    try:
        import sounddevice as sd  # type: ignore[import-not-found]
    except Exception as exc:  # pragma: no cover - env-specific
        raise RuntimeError(
            "sounddevice not importable; install PortAudio + the "
            "`sounddevice` wheel or re-run without mic capture"
        ) from exc
    return sd


def is_builtin_mic(device_name: str) -> bool:
    """Heuristic: does this look like a Mac laptop built-in mic?"""
    lowered = device_name.lower()
    return any(hint in lowered for hint in BUILTIN_MIC_HINTS)


def resolve_input_device(
    env: dict[str, str] | None = None,
    query_devices: Callable[[int | None, str | None], Any] | None = None,
    default_device_getter: Callable[[], tuple[int | None, int | None]] | None = None,
) -> DeviceInfo:
    """Resolve the input device from env / sounddevice defaults.

    All sounddevice entry points are injectable so tests can run without
    PortAudio. Returns a ``DeviceInfo`` with at least ``index`` + ``name``
    populated.
    """
    env = env if env is not None else dict(os.environ)

    if query_devices is None or default_device_getter is None:
        sd = _sounddevice()
        if query_devices is None:
            query_devices = sd.query_devices  # type: ignore[assignment]
        if default_device_getter is None:
            default_device_getter = lambda: tuple(sd.default.device)  # type: ignore[misc, assignment]

    explicit = env.get("VOICE_INPUT_DEVICE")
    if explicit is not None and explicit.strip() != "":
        try:
            index: int | None = int(explicit)
        except ValueError as exc:
            raise ValueError(
                f"VOICE_INPUT_DEVICE must be an int device index, got {explicit!r}"
            ) from exc
    else:
        default_in, _default_out = default_device_getter()
        index = default_in

    if index is None:
        raise RuntimeError(
            "No input device resolvable: set VOICE_INPUT_DEVICE or configure a "
            "system default input device"
        )

    info: Any = query_devices(index, "input")  # type: ignore[misc]
    # sounddevice returns a dict-like for a single device.
    name = str(info.get("name", "unknown"))
    sample_rate = float(info.get("default_samplerate", SAMPLE_RATE_HZ))
    max_in = int(info.get("max_input_channels", 1))
    return DeviceInfo(
        index=index,
        name=name,
        sample_rate_hz=sample_rate,
        max_input_channels=max_in,
    )


def warn_if_builtin(device: DeviceInfo, warn: Callable[[str], None]) -> None:
    """Emit a warning if the device looks like a laptop built-in mic."""
    if is_builtin_mic(device.name):
        warn(
            f"[voice] selected input device '{device.name}' looks like a "
            "built-in laptop mic; voice-feasibility.md 10.1 flags this as "
            "the #1 reliability risk. Consider a close-talk headset "
            "(Shure WH20, AKG C520)."
        )


class RingBuffer:
    """Fixed-capacity ring buffer of int16 PCM bytes.

    Used by the capture thread to hold ~N seconds of audio frames before
    the wake-word / VAD consumes them. We keep this pure-Python + stdlib
    so it is portable to 3.14 and trivially unit-testable.
    """

    def __init__(self, capacity_frames: int) -> None:
        if capacity_frames <= 0:
            raise ValueError("capacity_frames must be > 0")
        self.capacity = capacity_frames
        self._frames: list[bytes] = []

    def push(self, frame: bytes) -> None:
        self._frames.append(frame)
        if len(self._frames) > self.capacity:
            # Drop oldest frames to make room.
            drop = len(self._frames) - self.capacity
            del self._frames[:drop]

    def pop_all(self) -> list[bytes]:
        out = self._frames
        self._frames = []
        return out

    def __len__(self) -> int:
        return len(self._frames)
