"""Real microphone daemon and agent-readable CLI for issue #204.

Requirements:
  ✔︎ Use a real PortAudio input, wake backend, VAD, and local whisper.cpp.
  ✔︎ Submit transcripts to the same HTTP voice probe used by typed commands.
  ✔︎ Emit stable JSONL lifecycle, command, cancellation, and error state.
  ✔︎ Fail before `ready` when any required local service or device is absent.
  → Mac-local microphone, wake model, PortAudio, and whisper.cpp proof.

Acceptance tests:
  [if] preflight cannot open every real dependency [then ⛔️] never emit ready.
  [if] wake is followed by speech and silence [then] submit one bounded transcript.
  [if] speech never starts or shutdown is requested [then ⛔️] do not transcribe.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from types import FrameType
from typing import Any, Protocol
from urllib.parse import urlparse

from apps.voice import audio, stt, vad, wake


DEFAULT_API_URL = "http://127.0.0.1:9415/api/v1/voice/probe"
DEFAULT_FRAME_MS = 30
DEFAULT_SPEECH_START_TIMEOUT_MS = 2_000
DEFAULT_SILENCE_TAIL_MS = 300
DEFAULT_MAX_UTTERANCE_MS = 5_000
DEFAULT_HTTP_TIMEOUT_S = 5.0


@dataclass(frozen=True)
class Config:
    """All mutable mic daemon decisions in one serialisable contract."""

    api_url: str = DEFAULT_API_URL
    frame_ms: int = DEFAULT_FRAME_MS
    speech_start_timeout_ms: int = DEFAULT_SPEECH_START_TIMEOUT_MS
    silence_tail_ms: int = DEFAULT_SILENCE_TAIL_MS
    max_utterance_ms: int = DEFAULT_MAX_UTTERANCE_MS
    http_timeout_s: float = DEFAULT_HTTP_TIMEOUT_S
    max_commands: int | None = None

    def validate(self) -> None:
        parsed_url = urlparse(self.api_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ValueError(
                f"api_url must be an absolute HTTP URL, got {self.api_url!r}"
            )
        if self.frame_ms not in {10, 20, 30}:
            raise ValueError("frame_ms must be one of 10, 20, or 30")
        if self.speech_start_timeout_ms <= 0:
            raise ValueError("speech_start_timeout_ms must be > 0")
        if self.silence_tail_ms <= 0:
            raise ValueError("silence_tail_ms must be > 0")
        if self.max_utterance_ms <= 0:
            raise ValueError("max_utterance_ms must be > 0")
        if self.http_timeout_s <= 0:
            raise ValueError("http_timeout_s must be > 0")
        if self.max_commands is not None and self.max_commands <= 0:
            raise ValueError("max_commands must be > 0 when provided")


@dataclass(frozen=True)
class ProbeResult:
    transcript: str
    intent: str | None
    slots: dict[str, Any]
    blocked: bool
    reason: str | None
    reply: str | None


@dataclass(frozen=True)
class CaptureResult:
    audio: bytes | None
    reason: str
    duration_ms: int


class InputStream(Protocol):
    def read(self, frames: int) -> tuple[Any, bool]: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def __enter__(self) -> InputStream: ...

    def __exit__(self, *args: object) -> None: ...


class VoiceProbeClient:
    """Strict client for the existing text-command voice probe endpoint."""

    def __init__(self, url: str, timeout_s: float = DEFAULT_HTTP_TIMEOUT_S) -> None:
        self.url = url
        self.timeout_s = timeout_s

    def probe(self, transcript: str) -> ProbeResult:
        body = json.dumps({"text": transcript}).encode("utf-8")
        request = urllib.request.Request(self.url, data=body, method="POST")
        request.add_header("Content-Type", "application/json")
        request.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                status = int(response.status)
                raw_body = response.read()
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f"voice probe failed with HTTP {exc.code} at {self.url}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"voice probe unreachable at {self.url}: {exc.reason}"
            ) from exc
        if status != 200:
            raise RuntimeError(f"voice probe failed with HTTP {status} at {self.url}")
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("voice probe returned invalid JSON") from exc
        return _validate_probe_payload(transcript, payload)


@dataclass
class Runtime:
    """Preflight-validated real adapters held by the daemon."""

    config: Config
    sounddevice: Any
    device: audio.DeviceInfo
    wake_backend: wake.WakeWord
    detector: vad.Vad
    stt_client: stt.WhisperCppClient
    probe_client: VoiceProbeClient

    def open_stream(self) -> InputStream:
        return self.sounddevice.RawInputStream(
            device=self.device.index,
            samplerate=audio.SAMPLE_RATE_HZ,
            channels=audio.CHANNELS,
            dtype="int16",
            blocksize=0,
        )


class Jsonl:
    """Small JSONL emitter so agents can parse every daemon transition."""

    def __init__(self, write: Callable[[str], None] | None = None) -> None:
        self._write = write or (lambda line: print(line, flush=True))

    def emit(self, phase: str, **fields: Any) -> None:
        self._write(json.dumps({"phase": phase, **fields}, sort_keys=True))


def _validate_probe_payload(transcript: str, payload: Any) -> ProbeResult:
    if not isinstance(payload, dict):
        raise RuntimeError("voice probe response must be a JSON object")
    if payload.get("transcript") != transcript:
        raise RuntimeError("voice probe transcript mismatch")
    intent = payload.get("intent")
    slots = payload.get("slots")
    blocked = payload.get("blocked")
    reason = payload.get("reason")
    reply = payload.get("reply")
    if intent is not None and not isinstance(intent, str):
        raise RuntimeError("voice probe intent must be a string or null")
    if not isinstance(slots, dict):
        raise RuntimeError("voice probe slots must be an object")
    if not isinstance(blocked, bool):
        raise RuntimeError("voice probe blocked must be a boolean")
    if reason is not None and not isinstance(reason, str):
        raise RuntimeError("voice probe reason must be a string or null")
    if reply is not None and not isinstance(reply, str):
        raise RuntimeError("voice probe reply must be a string or null")
    return ProbeResult(
        transcript=transcript,
        intent=intent,
        slots=slots,
        blocked=blocked,
        reason=reason,
        reply=reply,
    )


def _validate_real_wake_config(env: dict[str, str]) -> None:
    backend = (env.get("WAKE_BACKEND") or "openwakeword").lower()
    if backend == "stub":
        raise RuntimeError(
            "WAKE_BACKEND=stub is test-only and cannot run the mic daemon"
        )
    if backend == "openwakeword":
        model_value = env.get("VOICE_WAKE_MODEL_PATH", "").strip()
        if not model_value:
            raise RuntimeError(
                "VOICE_WAKE_MODEL_PATH is required so the configured wake phrase is real"
            )
        model_path = Path(model_value).expanduser()
        if not model_path.is_file():
            raise RuntimeError(f"wake model not found: {model_path}")
    elif backend == "porcupine":
        keyword_value = env.get("VOICE_WAKE_KEYWORD_PATH", "").strip()
        if not keyword_value:
            raise RuntimeError(
                "VOICE_WAKE_KEYWORD_PATH is required for the real Porcupine wake phrase"
            )
        keyword_path = Path(keyword_value).expanduser()
        if not keyword_path.is_file():
            raise RuntimeError(f"wake keyword not found: {keyword_path}")


def preflight(config: Config, env: dict[str, str] | None = None) -> Runtime:
    """Resolve every dependency without claiming microphone readiness."""
    config.validate()
    resolved_env = dict(os.environ) if env is None else dict(env)
    _validate_real_wake_config(resolved_env)
    sounddevice = audio._sounddevice()
    device = audio.resolve_input_device(
        env=resolved_env,
        query_devices=sounddevice.query_devices,
        default_device_getter=lambda: tuple(sounddevice.default.device),
    )
    if device.max_input_channels < 1:
        raise RuntimeError(
            f"selected audio device has no input channels: {device.name}"
        )
    audio.warn_if_builtin(
        device, lambda message: print(message, file=sys.stderr, flush=True)
    )

    wake_backend = wake.make_backend(env=resolved_env)
    detector = vad.make_vad(env=resolved_env)
    if isinstance(detector, vad.AmplitudeVad):
        raise RuntimeError(
            "VAD_BACKEND=amplitude is test-only and cannot run the mic daemon"
        )
    stt_client = stt.make_client(env=resolved_env)
    if not isinstance(stt_client, stt.WhisperCppClient):
        raise RuntimeError("the mic daemon requires local STT_BACKEND=whisper_cpp")
    if not stt_client.health():
        raise RuntimeError(
            f"whisper server is not healthy at {stt_client.url}; "
            "start scripts/voice/start-whisper.sh"
        )
    probe_client = VoiceProbeClient(config.api_url, timeout_s=config.http_timeout_s)
    probe_result = probe_client.probe("")
    if probe_result.intent is not None or probe_result.reason != "grammar_miss":
        raise RuntimeError(
            "voice probe preflight did not preserve the grammar-miss contract"
        )
    return Runtime(
        config=config,
        sounddevice=sounddevice,
        device=device,
        wake_backend=wake_backend,
        detector=detector,
        stt_client=stt_client,
        probe_client=probe_client,
    )


def _frame_iterator(
    stream: InputStream,
    frame_samples: int,
    stop_requested: Callable[[], bool],
) -> Iterator[bytes]:
    while not stop_requested():
        frame, overflowed = stream.read(frame_samples)
        if overflowed:
            raise RuntimeError("microphone input overflowed; audio was dropped")
        yield bytes(frame)


def _wake_frame_samples(backend: wake.WakeWord) -> int:
    frame_samples = int(backend.frame_samples)
    if frame_samples <= 0:
        raise RuntimeError("wake backend frame_samples must be > 0")
    return frame_samples


def _vad_frame_samples(config: Config) -> int:
    return int(audio.SAMPLE_RATE_HZ * config.frame_ms / 1_000)


def capture_utterance(
    frames: Iterator[bytes],
    detector: vad.Vad,
    stop_requested: Callable[[], bool],
    speech_start_timeout_ms: int,
    silence_tail_ms: int,
    max_utterance_ms: int,
    frame_ms: int = DEFAULT_FRAME_MS,
) -> CaptureResult:
    """Capture post-wake speech without treating silence as a transcript."""
    captured: list[bytes] = []
    wait_ms = 0
    speech_ms = 0
    silence_ms = 0
    speech_started = False
    for frame in frames:
        if stop_requested():
            return CaptureResult(audio=None, reason="cancelled", duration_ms=speech_ms)
        is_speech = detector.is_speech(frame, audio.SAMPLE_RATE_HZ)
        if not speech_started:
            wait_ms += frame_ms
            if not is_speech:
                if wait_ms >= speech_start_timeout_ms:
                    return CaptureResult(
                        audio=None,
                        reason="speech_start_timeout",
                        duration_ms=wait_ms,
                    )
                continue
            speech_started = True

        captured.append(frame)
        speech_ms += frame_ms
        if is_speech:
            silence_ms = 0
        elif silence_ms + frame_ms >= silence_tail_ms:
            silence_ms += frame_ms
            return CaptureResult(
                audio=b"".join(captured),
                reason="silence_tail",
                duration_ms=speech_ms,
            )
        else:
            silence_ms += frame_ms
        if speech_ms >= max_utterance_ms:
            return CaptureResult(
                audio=b"".join(captured),
                reason="max_utterance",
                duration_ms=speech_ms,
            )
    if stop_requested():
        return CaptureResult(audio=None, reason="cancelled", duration_ms=speech_ms)
    return CaptureResult(
        audio=b"".join(captured) if speech_started else None,
        reason="stream_end",
        duration_ms=speech_ms if speech_started else wait_ms,
    )


def _ready_fields(runtime: Runtime) -> dict[str, Any]:
    return {
        "api_url": runtime.config.api_url,
        "device": asdict(runtime.device),
        "sample_rate_hz": audio.SAMPLE_RATE_HZ,
        "stt_backend": "whisper_cpp",
        "wake_backend": type(runtime.wake_backend).__name__,
        "vad_backend": type(runtime.detector).__name__,
    }


def check(config: Config, emitter: Jsonl) -> int:
    emitter.emit("starting", mode="check")
    try:
        runtime = preflight(config)
        with runtime.open_stream():
            emitter.emit("ready", **_ready_fields(runtime))
        emitter.emit("stopped", reason="check_complete")
        return 0
    except Exception as exc:
        emitter.emit("error", error_type=type(exc).__name__, message=str(exc))
        return 2


def run(config: Config, emitter: Jsonl) -> int:
    emitter.emit("starting", mode="run")
    shutdown_requested = False
    shutdown_signal: str | None = None

    def _request_stop(signum: int, frame: FrameType | None) -> None:
        nonlocal shutdown_requested, shutdown_signal
        del frame
        shutdown_requested = True
        shutdown_signal = signal.Signals(signum).name

    previous_handlers: dict[int, Any] = {}
    for signal_number in (signal.SIGINT, signal.SIGTERM):
        try:
            previous_handlers[signal_number] = signal.signal(
                signal_number, _request_stop
            )
        except (OSError, ValueError):
            continue

    try:
        runtime = preflight(config)
        wake_frame_samples = _wake_frame_samples(runtime.wake_backend)
        vad_frame_samples = _vad_frame_samples(config)
        submitted = 0
        with runtime.open_stream() as stream:
            emitter.emit("ready", **_ready_fields(runtime))
            while not shutdown_requested:
                wake_frame, overflowed = stream.read(wake_frame_samples)
                if overflowed:
                    raise RuntimeError("microphone input overflowed; audio was dropped")
                confidence = runtime.wake_backend.detect(bytes(wake_frame))
                if confidence < runtime.wake_backend.threshold:
                    continue
                emitter.emit("wake_detected", confidence=confidence)
                runtime.wake_backend.reset()
                emitter.emit("capturing")
                capture = capture_utterance(
                    frames=_frame_iterator(
                        stream,
                        vad_frame_samples,
                        lambda: shutdown_requested,
                    ),
                    detector=runtime.detector,
                    stop_requested=lambda: shutdown_requested,
                    speech_start_timeout_ms=config.speech_start_timeout_ms,
                    silence_tail_ms=config.silence_tail_ms,
                    max_utterance_ms=config.max_utterance_ms,
                    frame_ms=config.frame_ms,
                )
                if capture.audio is None:
                    emitter.emit("capture_cancelled", reason=capture.reason)
                    continue
                stream.stop()
                if shutdown_requested:
                    emitter.emit("capture_cancelled", reason="shutdown_requested")
                    break
                transcript = runtime.stt_client.transcribe(
                    capture.audio,
                    sample_rate_hz=audio.SAMPLE_RATE_HZ,
                ).text.strip()
                if shutdown_requested:
                    emitter.emit("transcript_rejected", reason="shutdown_requested")
                    break
                if not transcript:
                    emitter.emit("transcript_rejected", reason="empty_transcript")
                    stream.start()
                    continue
                emitter.emit(
                    "transcript",
                    text=transcript,
                    capture_reason=capture.reason,
                    duration_ms=capture.duration_ms,
                )
                if shutdown_requested:
                    emitter.emit("transcript_rejected", reason="shutdown_requested")
                    break
                probe_result = runtime.probe_client.probe(transcript)
                emitter.emit("submitted", **asdict(probe_result))
                submitted += 1
                if config.max_commands is not None and submitted >= config.max_commands:
                    break
                stream.start()
        reason = shutdown_signal or (
            "max_commands" if config.max_commands is not None else "stream_closed"
        )
        emitter.emit("stopped", reason=reason)
        return 0
    except Exception as exc:
        emitter.emit("error", error_type=type(exc).__name__, message=str(exc))
        return 2
    finally:
        for previous_signal_number, previous_handler in previous_handlers.items():
            try:
                signal.signal(previous_signal_number, previous_handler)
            except (OSError, ValueError):
                continue


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m apps.voice.mic_daemon",
        description="Real mic to whisper.cpp to voice-probe daemon with JSONL state.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("check", "run"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument(
            "--api-url",
            default=os.environ.get("VOICE_PROBE_URL", DEFAULT_API_URL),
        )
        command_parser.add_argument("--frame-ms", type=int, default=DEFAULT_FRAME_MS)
        command_parser.add_argument(
            "--speech-start-timeout-ms",
            type=int,
            default=DEFAULT_SPEECH_START_TIMEOUT_MS,
        )
        command_parser.add_argument(
            "--silence-tail-ms",
            type=int,
            default=DEFAULT_SILENCE_TAIL_MS,
        )
        command_parser.add_argument(
            "--max-utterance-ms",
            type=int,
            default=DEFAULT_MAX_UTTERANCE_MS,
        )
        command_parser.add_argument(
            "--http-timeout-s",
            type=float,
            default=DEFAULT_HTTP_TIMEOUT_S,
        )
        if command == "run":
            command_parser.add_argument("--max-commands", type=int, default=None)
    return parser


def _config_from_args(args: argparse.Namespace) -> Config:
    return Config(
        api_url=args.api_url,
        frame_ms=args.frame_ms,
        speech_start_timeout_ms=args.speech_start_timeout_ms,
        silence_tail_ms=args.silence_tail_ms,
        max_utterance_ms=args.max_utterance_ms,
        http_timeout_s=args.http_timeout_s,
        max_commands=getattr(args, "max_commands", None),
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    config = _config_from_args(args)
    emitter = Jsonl()
    if args.command == "check":
        return check(config, emitter)
    if args.command == "run":
        return run(config, emitter)
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
