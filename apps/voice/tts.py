"""Text-to-speech output.

Default backend: macOS ``say`` subprocess (zero install; 50 ms first
audio). Opt-in: Kokoro / Piper raise ``NotImplementedError`` with a
clear "enabled in a follow-up" message so the hook exists without the
dep (voice-feasibility.md 5.3, 5.2).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass

DEFAULT_SAY_VOICE: str = "the maintainer"


@dataclass(frozen=True)
class TtsResult:
    """Record of a TTS call (for tests + logging)."""

    text: str
    cmd: tuple[str, ...]
    backend: str
    returncode: int


def build_say_cmd(
    text: str,
    voice: str | None = None,
    rate_wpm: int | None = None,
) -> tuple[str, ...]:
    """Build the ``say`` argv. Keeps command construction testable."""
    cmd: list[str] = ["say"]
    if voice:
        cmd += ["-v", voice]
    if rate_wpm:
        cmd += ["-r", str(rate_wpm)]
    cmd.append(text)
    return tuple(cmd)


class SayTts:
    """macOS ``say`` subprocess wrapper."""

    backend_name = "say"

    def __init__(
        self,
        voice: str | None = None,
        rate_wpm: int | None = None,
        runner: Callable[[Sequence[str]], subprocess.CompletedProcess] | None = None,
    ) -> None:
        self.voice = voice or os.environ.get("VOICE_SAY_VOICE", DEFAULT_SAY_VOICE)
        self.rate_wpm = rate_wpm
        # Injectable for tests -- real default uses subprocess.run.
        self._runner = runner or (
            lambda argv: subprocess.run(
                list(argv),
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )

    def available(self) -> bool:
        return shutil.which("say") is not None

    def speak(self, text: str) -> TtsResult:
        cmd = build_say_cmd(text, voice=self.voice, rate_wpm=self.rate_wpm)
        proc = self._runner(cmd)
        return TtsResult(
            text=text,
            cmd=cmd,
            backend=self.backend_name,
            returncode=getattr(proc, "returncode", 0),
        )


class KokoroTts:
    """Kokoro-82M ONNX TTS hook -- raises until Phase 14.x lands."""

    backend_name = "kokoro"

    def __init__(self) -> None:  # pragma: no cover - hook only
        raise NotImplementedError(
            "Kokoro TTS hook exists but is not wired in Phase 14 (D5 "
            "default is `say`). Enabled in a follow-up plan."
        )

    def speak(self, text: str) -> TtsResult:  # pragma: no cover
        raise NotImplementedError


class PiperTts:
    """Piper TTS hook -- raises until Phase 14.x lands."""

    backend_name = "piper"

    def __init__(self) -> None:  # pragma: no cover - hook only
        raise NotImplementedError(
            "Piper TTS hook exists but is not wired in Phase 14. "
            "Enabled in a follow-up plan."
        )

    def speak(self, text: str) -> TtsResult:  # pragma: no cover
        raise NotImplementedError


class RecordingTts:
    """Test double: records every spoken text, runs no subprocess."""

    backend_name = "recording"

    def __init__(self) -> None:
        self.spoken: list[str] = []

    def available(self) -> bool:
        return True

    def speak(self, text: str) -> TtsResult:
        self.spoken.append(text)
        return TtsResult(
            text=text,
            cmd=("recording",),
            backend=self.backend_name,
            returncode=0,
        )


def make_tts(env: dict[str, str] | None = None):
    """Factory. ``$TTS_BACKEND`` picks backend; default ``say``."""
    env = env if env is not None else dict(os.environ)
    backend = (env.get("TTS_BACKEND") or "say").lower()
    if backend == "say":
        return SayTts()
    if backend == "kokoro":
        return KokoroTts()
    if backend == "piper":
        return PiperTts()
    if backend == "recording":
        return RecordingTts()
    raise ValueError(f"Unknown TTS_BACKEND={backend!r}")
