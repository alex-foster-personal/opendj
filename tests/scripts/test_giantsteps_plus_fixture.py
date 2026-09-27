"""Contract tests for the GiantSteps+ fixture fetch script."""

from __future__ import annotations

import subprocess
from pathlib import Path

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
