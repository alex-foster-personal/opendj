"""Speech-to-text client for the whisper.cpp HTTP daemon.

Default backend POSTs a WAV blob to a warm ``whisper-server`` running
on ``http://127.0.0.1:2022/inference`` and returns the transcript.
``STT_BACKEND=groq`` is wired but gated behind a doppler-sourced
``GROQ_API_KEY``; it is NOT the default (voice-feasibility.md 2.6).

All HTTP calls use ``urllib`` so this module has zero extra
dependencies. We include the DJ-vocab ``initial_prompt`` from
voice-feasibility.md 10.2 to bias the LM toward our domain words.
"""
from __future__ import annotations

import io
import json
import os
import struct
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

DEFAULT_WHISPER_URL: str = "http://127.0.0.1:2022/inference"
DEFAULT_WHISPER_TIMEOUT_S: float = 15.0

DJ_INITIAL_PROMPT: str = (
    "DJ commands: cue points, BPM, key, transition, deck, queue, "
    "Rekordbox, djay, Camelot"
)


@dataclass(frozen=True)
class Transcript:
    """STT response."""

    text: str
    raw: dict[str, Any]
    backend: str = "whisper_cpp"


def pcm_to_wav_bytes(pcm: bytes, sample_rate_hz: int = 16_000, channels: int = 1) -> bytes:
    """Wrap int16 mono PCM bytes in a RIFF/WAV container.

    Standalone + stdlib-only so tests do not need ``scipy`` or ``wave``
    outside ``io``. Saves us from having to write to disk for the HTTP
    multipart body.
    """
    bytes_per_sample = 2
    byte_rate = sample_rate_hz * channels * bytes_per_sample
    block_align = channels * bytes_per_sample
    data_size = len(pcm)
    fmt_chunk_size = 16
    riff_size = 4 + (8 + fmt_chunk_size) + (8 + data_size)

    buf = io.BytesIO()
    buf.write(b"RIFF")
    buf.write(struct.pack("<I", riff_size))
    buf.write(b"WAVE")
    buf.write(b"fmt ")
    buf.write(struct.pack("<I", fmt_chunk_size))
    buf.write(struct.pack("<H", 1))  # PCM format
    buf.write(struct.pack("<H", channels))
    buf.write(struct.pack("<I", sample_rate_hz))
    buf.write(struct.pack("<I", byte_rate))
    buf.write(struct.pack("<H", block_align))
    buf.write(struct.pack("<H", bytes_per_sample * 8))
    buf.write(b"data")
    buf.write(struct.pack("<I", data_size))
    buf.write(pcm)
    return buf.getvalue()


def build_multipart(
    wav_bytes: bytes,
    prompt: str = DJ_INITIAL_PROMPT,
    boundary: str = "---voicephase14boundary",
) -> tuple[bytes, str]:
    """Produce a minimal ``multipart/form-data`` body for whisper-server.

    whisper.cpp's ``whisper-server`` expects the file field named
    ``file`` and accepts ``prompt`` + ``response_format`` fields.
    """
    parts: list[bytes] = []
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(
        b'Content-Disposition: form-data; name="file"; filename="utt.wav"\r\n'
    )
    parts.append(b"Content-Type: audio/wav\r\n\r\n")
    parts.append(wav_bytes)
    parts.append(b"\r\n")
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(b'Content-Disposition: form-data; name="prompt"\r\n\r\n')
    parts.append(prompt.encode("utf-8"))
    parts.append(b"\r\n")
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(b'Content-Disposition: form-data; name="response_format"\r\n\r\n')
    parts.append(b"json\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)
    content_type = f"multipart/form-data; boundary={boundary}"
    return body, content_type


class WhisperCppClient:
    """Thin HTTP client for the whisper.cpp server (port 2022)."""

    def __init__(
        self,
        url: str = DEFAULT_WHISPER_URL,
        timeout_s: float = DEFAULT_WHISPER_TIMEOUT_S,
        prompt: str = DJ_INITIAL_PROMPT,
        opener: Callable[[urllib.request.Request, float], Any] | None = None,
    ) -> None:
        self.url = url
        self.timeout_s = timeout_s
        self.prompt = prompt
        # Injectable for tests (default = urllib.request.urlopen).
        self._opener = opener or (
            lambda req, timeout: urllib.request.urlopen(req, timeout=timeout)
        )

    def transcribe(self, pcm: bytes, sample_rate_hz: int = 16_000) -> Transcript:
        wav = pcm_to_wav_bytes(pcm, sample_rate_hz=sample_rate_hz)
        body, content_type = build_multipart(wav, prompt=self.prompt)
        req = urllib.request.Request(self.url, data=body, method="POST")
        req.add_header("Content-Type", content_type)
        req.add_header("Content-Length", str(len(body)))
        try:
            resp = self._opener(req, self.timeout_s)
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"whisper server unreachable at {self.url}: {exc}. "
                "Start it via `scripts/voice/start-whisper.sh` or set "
                "STT_BACKEND=groq (requires GROQ_API_KEY)"
            ) from exc
        with resp as r:
            raw_bytes = r.read()
        try:
            payload = json.loads(raw_bytes.decode("utf-8"))
        except json.JSONDecodeError:
            payload = {"text": raw_bytes.decode("utf-8", errors="replace")}
        text = str(payload.get("text", "")).strip()
        return Transcript(text=text, raw=payload, backend="whisper_cpp")

    def health(self) -> bool:
        """Return True if the daemon is reachable (no transcription).

        Hits the base URL and treats any response (even a 404) as "the
        server is up; it just did not like GET /inference".
        """
        base = self.url.rsplit("/", 1)[0] or self.url
        req = urllib.request.Request(base, method="GET")
        try:
            self._opener(req, self.timeout_s)
            return True
        except urllib.error.HTTPError:
            return True
        except urllib.error.URLError:
            return False


class GroqClient:
    """Opt-in cloud STT. Only importable when ``GROQ_API_KEY`` is set.

    Full implementation deferred. Phase 14 ships the hook + env gate so
    the CLI fails fast with a clear message when the backend is picked
    without a key.
    """

    def __init__(self, api_key: str | None = None) -> None:
        key = api_key or os.environ.get("GROQ_API_KEY")
        if not key:
            raise RuntimeError(
                "GROQ_API_KEY not set; configure via "
                "`doppler secrets set GROQ_API_KEY` or drop "
                "STT_BACKEND=groq (the default is whisper.cpp local)"
            )
        self._key = key

    def transcribe(self, pcm: bytes, sample_rate_hz: int = 16_000) -> Transcript:  # pragma: no cover
        raise NotImplementedError(
            "Groq STT hook exists but is not wired in Phase 14 (C4 "
            "default is local). Use STT_BACKEND=whisper_cpp."
        )


def make_client(env: dict[str, str] | None = None):
    """Factory -- pick STT backend per ``$STT_BACKEND``."""
    env = env if env is not None else dict(os.environ)
    backend = (env.get("STT_BACKEND") or "whisper_cpp").lower()
    if backend in ("whisper_cpp", "whisper-cpp", "whisper"):
        url = env.get("VOICE_WHISPER_URL", DEFAULT_WHISPER_URL)
        try:
            timeout = float(env.get("VOICE_WHISPER_TIMEOUT_S", DEFAULT_WHISPER_TIMEOUT_S))
        except ValueError:
            timeout = DEFAULT_WHISPER_TIMEOUT_S
        return WhisperCppClient(url=url, timeout_s=timeout)
    if backend == "groq":
        return GroqClient()
    raise ValueError(f"Unknown STT_BACKEND={backend!r}")
