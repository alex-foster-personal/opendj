"""Acceptance tests for the real microphone daemon contract (VOICE-01).

Requirements:
  ✔︎ A transcript uses the same HTTP voice probe contract as typed commands.
  ✔︎ Probe transport and response mismatches fail explicitly.
  ✔︎ Utterance capture cancels before speech and cuts after a silence tail.
  ? The daemon emits machine-readable lifecycle events and fails preflight.
  → Real PortAudio, microphone, wake word, and whisper.cpp proof is Mac-local.

Acceptance tests:
  [if] the probe endpoint returns a validated intent [then] preserve it exactly.
  [if] the probe endpoint fails or changes shape [then ⛔️] raise a terminal error.
  [if] speech never begins or cancellation arrives [then ⛔️] do not transcribe.
"""

from __future__ import annotations

import json
import struct
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from apps.voice import mic_daemon, vad, wake

pytestmark = pytest.mark.requirement("VOICE-01")


def _pcm_frame(amplitude: int) -> bytes:
    samples = [amplitude if index % 2 == 0 else -amplitude for index in range(480)]
    return struct.pack(f"<{len(samples)}h", *samples)


class _ProbeHandler(BaseHTTPRequestHandler):
    response_status = 200
    response_body: dict[str, object] = {
        "transcript": "find daft punk",
        "intent": "SEARCH",
        "slots": {"query": "daft punk"},
        "blocked": False,
        "reason": None,
        "reply": "search:daft punk",
    }
    received_body: dict[str, object] | None = None

    def do_POST(self) -> None:
        content_length = int(self.headers["Content-Length"])
        type(self).received_body = json.loads(self.rfile.read(content_length))
        payload = json.dumps(type(self).response_body).encode("utf-8")
        self.send_response(type(self).response_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def probe_server() -> Iterator[str]:
    _ProbeHandler.response_status = 200
    _ProbeHandler.response_body = {
        "transcript": "find daft punk",
        "intent": "SEARCH",
        "slots": {"query": "daft punk"},
        "blocked": False,
        "reason": None,
        "reply": "search:daft punk",
    }
    _ProbeHandler.received_body = None
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ProbeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}/api/v1/voice/probe"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_probe_client_preserves_validated_voice_probe_response(
    probe_server: str,
) -> None:
    result = mic_daemon.VoiceProbeClient(probe_server).probe("find daft punk")

    assert _ProbeHandler.received_body == {"text": "find daft punk"}
    assert result.intent == "SEARCH"
    assert result.slots == {"query": "daft punk"}
    assert result.reply == "search:daft punk"


def test_probe_client_fails_on_http_error(probe_server: str) -> None:
    _ProbeHandler.response_status = 503

    with pytest.raises(RuntimeError, match="voice probe failed with HTTP 503"):
        mic_daemon.VoiceProbeClient(probe_server).probe("find daft punk")


def test_probe_client_fails_on_transcript_mismatch(probe_server: str) -> None:
    _ProbeHandler.response_body = {**_ProbeHandler.response_body, "transcript": "other"}

    with pytest.raises(RuntimeError, match="transcript mismatch"):
        mic_daemon.VoiceProbeClient(probe_server).probe("find daft punk")


def test_capture_utterance_cancels_when_speech_never_starts() -> None:
    silence = _pcm_frame(0)

    result = mic_daemon.capture_utterance(
        frames=iter([silence] * 5),
        detector=vad.AmplitudeVad(),
        stop_requested=lambda: False,
        speech_start_timeout_ms=150,
        silence_tail_ms=90,
        max_utterance_ms=1_000,
    )

    assert result.audio is None
    assert result.reason == "speech_start_timeout"


def test_capture_utterance_cuts_on_silence_tail() -> None:
    speech = _pcm_frame(5_000)
    silence = _pcm_frame(0)

    result = mic_daemon.capture_utterance(
        frames=iter([speech] * 3 + [silence] * 3 + [speech] * 3),
        detector=vad.AmplitudeVad(),
        stop_requested=lambda: False,
        speech_start_timeout_ms=300,
        silence_tail_ms=90,
        max_utterance_ms=1_000,
    )

    assert result.audio == b"".join([speech] * 3 + [silence] * 3)
    assert result.reason == "silence_tail"


def test_capture_utterance_stops_without_audio_success() -> None:
    speech = _pcm_frame(5_000)
    checks = iter([False, True])

    result = mic_daemon.capture_utterance(
        frames=iter([speech] * 10),
        detector=vad.AmplitudeVad(),
        stop_requested=lambda: next(checks),
        speech_start_timeout_ms=300,
        silence_tail_ms=90,
        max_utterance_ms=1_000,
    )

    assert result.audio is None
    assert result.reason == "cancelled"


def test_capture_utterance_drops_partial_audio_when_source_stops_for_shutdown() -> None:
    speech = _pcm_frame(5_000)
    checks = iter([False, True])

    result = mic_daemon.capture_utterance(
        frames=iter([speech]),
        detector=vad.AmplitudeVad(),
        stop_requested=lambda: next(checks),
        speech_start_timeout_ms=300,
        silence_tail_ms=90,
        max_utterance_ms=1_000,
    )

    assert result.audio is None
    assert result.reason == "cancelled"


def test_runtime_uses_native_wake_frames_and_vad_uses_configured_frames() -> None:
    assert mic_daemon._wake_frame_samples(wake.StubBackend()) == 480
    assert mic_daemon._vad_frame_samples(mic_daemon.Config(frame_ms=20)) == 320
