"""Contract tests for the GiantSteps+ fixture fetch script."""

from __future__ import annotations

import subprocess
import urllib.request
from pathlib import Path

import pytest

import scripts.giantsteps_plus_fixture as fixture_mod


def test_decode_verified_mp3_overwrites_stale_wav(tmp_path: Path, monkeypatch) -> None:
    """Regression: fetch must not trust an existing wav_path without re-decoding."""
    mp3_path = tmp_path / "123.mp3"
    wav_path = tmp_path / "123.wav"
    mp3_path.write_bytes(b"verified-mp3-bytes")
    wav_path.write_bytes(b"STALE-WAV-FROM-PRIOR-RUN")

    calls = 0

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        out_wav = Path(cmd[-1])
        out_wav.write_bytes(f"fresh-decode-{calls}".encode())
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    fixture_mod._decode_verified_mp3(mp3_path, wav_path)

    assert calls == 1
    assert wav_path.read_bytes() == b"fresh-decode-1"


class _FakeRangeResponse:
    def __init__(
        self,
        status: int,
        body: bytes,
        *,
        content_range: str | None = None,
    ) -> None:
        self.status = status
        self._body = body
        self.headers = {}
        if content_range is not None:
            self.headers["Content-Range"] = content_range

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeRangeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def test_http_range_read_rejects_200_full_file_response(monkeypatch: pytest.MonkeyPatch) -> None:
    rf = fixture_mod._HTTPRangeFile.__new__(fixture_mod._HTTPRangeFile)
    rf._url = "https://example.test/archive.zip"
    rf._size = 1000
    rf._pos = 0

    def fake_urlopen(req: urllib.request.Request, timeout: int = 0) -> _FakeRangeResponse:
        return _FakeRangeResponse(200, b"x" * 1000)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(SystemExit, match="expected HTTP 206"):
        rf.read(10)


def test_http_range_read_rejects_mismatched_content_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rf = fixture_mod._HTTPRangeFile.__new__(fixture_mod._HTTPRangeFile)
    rf._url = "https://example.test/archive.zip"
    rf._size = 1000
    rf._pos = 100

    def fake_urlopen(req: urllib.request.Request, timeout: int = 0) -> _FakeRangeResponse:
        return _FakeRangeResponse(
            206,
            b"0123456789",
            content_range="bytes 0-9/1000",
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(SystemExit, match="does not match requested"):
        rf.read(10)


def test_http_range_read_rejects_short_206_body(monkeypatch: pytest.MonkeyPatch) -> None:
    rf = fixture_mod._HTTPRangeFile.__new__(fixture_mod._HTTPRangeFile)
    rf._url = "https://example.test/archive.zip"
    rf._size = 1000
    rf._pos = 50

    def fake_urlopen(req: urllib.request.Request, timeout: int = 0) -> _FakeRangeResponse:
        return _FakeRangeResponse(
            206,
            b"short",
            content_range="bytes 50-59/1000",
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(SystemExit, match="body length"):
        rf.read(10)


def test_parse_content_range_accepts_bytes_spec() -> None:
    parsed = fixture_mod._parse_content_range("bytes 50-59/1000")
    assert parsed == (50, 59, 1000)
