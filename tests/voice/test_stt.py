"""Unit tests for apps.voice.stt (VOICE-01)."""
from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from typing import Any

import pytest

from apps.voice import stt


pytestmark = pytest.mark.requirement("VOICE-01")


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self.status = status

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class TestPcmToWav:
    def test_wav_header_size(self):
        pcm = b"\x00\x01" * 100  # 200 bytes
        wav = stt.pcm_to_wav_bytes(pcm, 16_000, 1)
        assert wav[:4] == b"RIFF"
        assert wav[8:12] == b"WAVE"
        # RIFF size = total - 8
        import struct
        riff_size = struct.unpack("<I", wav[4:8])[0]
        assert riff_size == len(wav) - 8

    def test_data_chunk_present(self):
        pcm = b"\xaa\xbb\xcc\xdd"
        wav = stt.pcm_to_wav_bytes(pcm)
        assert b"data" in wav
        assert wav.endswith(pcm)


class TestBuildMultipart:
    def test_includes_file_and_prompt(self):
        body, ct = stt.build_multipart(b"WAVDATA", prompt="hello")
        assert b'name="file"' in body
        assert b'name="prompt"' in body
        assert b"hello" in body
        assert b"WAVDATA" in body
        assert "multipart/form-data" in ct


class TestWhisperCppClient:
    def test_transcribe_happy_path(self):
        body = json.dumps({"text": "what's this bpm"}).encode("utf-8")
        captured: dict[str, Any] = {}

        def fake_opener(req: urllib.request.Request, timeout: float):
            captured["url"] = req.full_url
            captured["timeout"] = timeout
            captured["body_len"] = len(req.data or b"")
            return FakeResponse(body)

        client = stt.WhisperCppClient(opener=fake_opener)
        result = client.transcribe(b"\x00\x00" * 100)
        assert result.text == "what's this bpm"
        assert result.backend == "whisper_cpp"
        assert captured["url"].endswith("/inference")

    def test_url_error_wrapped(self):
        def fake_opener(req, timeout):
            raise urllib.error.URLError("connection refused")

        client = stt.WhisperCppClient(opener=fake_opener)
        with pytest.raises(RuntimeError, match="whisper server unreachable"):
            client.transcribe(b"\x00" * 10)

    def test_non_json_body_falls_through(self):
        def fake_opener(req, timeout):
            return FakeResponse(b"not json here")

        client = stt.WhisperCppClient(opener=fake_opener)
        result = client.transcribe(b"\x00" * 10)
        assert "not json here" in result.text

    def test_health_true_on_200(self):
        client = stt.WhisperCppClient(opener=lambda req, t: FakeResponse(b"ok"))
        assert client.health() is True

    def test_health_true_on_http_error_still_up(self):
        def fake_opener(req, timeout):
            raise urllib.error.HTTPError(
                req.full_url, 404, "not found", {}, io.BytesIO(b"")
            )

        client = stt.WhisperCppClient(opener=fake_opener)
        assert client.health() is True

    def test_health_false_on_url_error(self):
        def fake_opener(req, timeout):
            raise urllib.error.URLError("server down")

        client = stt.WhisperCppClient(opener=fake_opener)
        assert client.health() is False


class TestMakeClient:
    def test_default_is_whisper_cpp(self):
        client = stt.make_client(env={})
        assert isinstance(client, stt.WhisperCppClient)

    def test_groq_without_key_raises(self):
        with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
            stt.make_client(env={"STT_BACKEND": "groq", "GROQ_API_KEY": ""})

    def test_unknown_raises(self):
        with pytest.raises(ValueError):
            stt.make_client(env={"STT_BACKEND": "wizard"})

    def test_env_url_override(self):
        client = stt.make_client(env={"VOICE_WHISPER_URL": "http://localhost:9999/x"})
        assert isinstance(client, stt.WhisperCppClient)
        assert client.url == "http://localhost:9999/x"
