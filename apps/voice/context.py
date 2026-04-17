"""Runtime context carried between handlers.

Holds mute state, debounce timers, the active event bus + TTS engine,
and the destructive-mode flag. Instantiated at daemon startup and
threaded through every handler call.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

from apps.voice import bus as bus_mod
from apps.voice import tts as tts_mod


DEFAULT_DEBOUNCE_S: float = 2.0
DEFAULT_MUTE_DURATION_S: float = 30 * 60  # 30 minutes


@dataclass
class VoiceContext:
    """State threaded through the daemon's action loop."""

    event_bus: Any
    tts_engine: Any
    destructive: bool = False
    mute_until: float | None = None
    last_dispatch_at: float | None = None
    last_transcript: str = ""
    debounce_s: float = DEFAULT_DEBOUNCE_S
    mute_duration_s: float = DEFAULT_MUTE_DURATION_S
    settings: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_env(
        cls,
        env: dict[str, str] | None = None,
        event_bus: Any | None = None,
        tts_engine: Any | None = None,
    ) -> "VoiceContext":
        env = env if env is not None else dict(os.environ)
        bus_impl = event_bus if event_bus is not None else bus_mod.make_bus()
        tts_impl = tts_engine if tts_engine is not None else tts_mod.make_tts(env)
        try:
            debounce = float(env.get("VOICE_DEBOUNCE_S", DEFAULT_DEBOUNCE_S))
        except ValueError:
            debounce = DEFAULT_DEBOUNCE_S
        try:
            mute_duration = float(
                env.get("VOICE_MUTE_DURATION_S", DEFAULT_MUTE_DURATION_S)
            )
        except ValueError:
            mute_duration = DEFAULT_MUTE_DURATION_S
        destructive_flag = env.get("VOICE_ENABLE_DESTRUCTIVE", "").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        return cls(
            event_bus=bus_impl,
            tts_engine=tts_impl,
            destructive=destructive_flag,
            debounce_s=debounce,
            mute_duration_s=mute_duration,
        )

    def is_muted(self, now: float | None = None) -> bool:
        if self.mute_until is None:
            return False
        now = now if now is not None else time.time()
        return now < self.mute_until

    def mute(self, now: float | None = None) -> float:
        now = now if now is not None else time.time()
        self.mute_until = now + self.mute_duration_s
        return self.mute_until

    def unmute(self) -> None:
        self.mute_until = None

    def debounced(self, now: float | None = None) -> bool:
        if self.last_dispatch_at is None:
            return False
        now = now if now is not None else time.time()
        return (now - self.last_dispatch_at) < self.debounce_s

    def mark_dispatch(self, now: float | None = None) -> None:
        self.last_dispatch_at = now if now is not None else time.time()
