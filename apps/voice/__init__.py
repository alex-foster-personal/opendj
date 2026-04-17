"""Voice commands app (Phase 14 / VOICE-01).

Local wake-word + Whisper + grammar intent pipeline for mid-set voice
control of the DJ booth. See ``apps/voice/README.md`` for run-book, and
``.planning/phases/14-voice-commands/`` for design context.

Modules:
  audio     -- mic capture + device selection (sounddevice wrapper).
  wake      -- wake-word detection (openWakeWord primary; porcupine opt-in).
  vad       -- WebRTC VAD silence-tail detection.
  stt       -- whisper.cpp HTTP client (port 2022).
  tts       -- macOS ``say`` subprocess wrapper.
  timings   -- per-stage latency logger.
  grammar   -- regex/PEG intent parser.
  bus       -- EventBus adapter (StateBackedBus + JsonlStubBus).
  actions   -- intent dispatch handlers.
  context   -- mute state, debounce, runtime context.
  confirm   -- verbal yes/no confirmation loop (Plan 3).
  settings  -- persistent mute + backend settings (Plan 3).

All tests are tagged ``@pytest.mark.requirement("VOICE-01")``.
"""
from __future__ import annotations

__all__ = [
    "audio",
    "wake",
    "vad",
    "stt",
    "tts",
    "timings",
    "grammar",
    "bus",
    "actions",
    "context",
    "confirm",
    "settings",
]
