"""Smoke test: real S-KEY inference through the shipped PEP 723 runner.

WHY THIS IS OPT-IN. It needs `uv`, a network fetch of S-KEY (git+https) and
its checkpoint on first run, and roughly a minute of CPU. That is not a fast-
lane test. Gated on MDT_KEY_MODEL_TESTS=1, mirroring
tests/analysis_beatgrid/test_mutation_end_to_end.py's MDT_BEATGRID_MODEL_TESTS
gate: an explicit environment variable rather than a try/except on an import,
because a test that skips itself when something is broken is a check that
cannot fail.
"""
from __future__ import annotations

import json
import math
import os
import re
import struct
import subprocess
import wave

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RUNNER = os.path.join(REPO_ROOT, "apps", "analysis_key", "skey_runner.py")

pytestmark = pytest.mark.skipif(
    os.environ.get("MDT_KEY_MODEL_TESTS") != "1",
    reason=(
        "needs uv, a network fetch of S-KEY, and about a minute of CPU; "
        "set MDT_KEY_MODEL_TESTS=1 to run"
    ),
)


def _write_synthetic_tone(
    path: str, freq_hz: float = 220.0, seconds: float = 5.0, sr: int = 44100
) -> None:
    n = int(sr * seconds)
    with wave.open(path, "w") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(sr)
        frames = bytearray()
        for i in range(n):
            value = int(3000 * math.sin(2 * math.pi * freq_hz * i / sr))
            frames += struct.pack("<h", value)
        fh.writeframes(bytes(frames))


def test_skey_runner_produces_a_key_and_reports_onnx_export_attempts(tmp_path) -> None:
    audio_path = str(tmp_path / "tone.wav")
    _write_synthetic_tone(audio_path)
    out_path = str(tmp_path / "skey_out.json")

    result = subprocess.run(
        ["uv", "run", RUNNER, "--audio", audio_path, "--out", out_path, "--device", "cpu"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"

    with open(out_path, encoding="utf-8") as fh:
        payload = json.load(fh)

    assert payload["n_tracks"] == 1
    track = payload["results"][audio_path]
    assert track["error"] is None
    assert track["label"] in {
        "A Major", "Bb Major", "B Major", "C Major", "C# Major", "D Major", "D# Major",
        "E Major", "F Major", "F# Major", "G Major", "G# Major",
        "B minor", "C minor", "C# minor", "D minor", "D# minor", "E minor",
        "F minor", "F# minor", "G minor", "G# minor", "A minor", "Bb minor",
    }
    assert track["key_rekordbox_style"]

    assert payload["checkpoint"]["size_bytes"] > 0
    assert payload["checkpoint"]["sha256"]
    assert "UNSPECIFIED" in payload["checkpoint"]["license"]
    assert re.fullmatch(r"[0-9a-f]{40}", payload["skey_revision"])

    assert len(payload["onnx_export"]) == 2
    for attempt in payload["onnx_export"]:
        assert attempt["success"] in (True, False)
        if not attempt["success"]:
            assert attempt["error"]


def test_skey_runner_skip_onnx_export_flag(tmp_path) -> None:
    audio_path = str(tmp_path / "tone.wav")
    _write_synthetic_tone(audio_path)
    out_path = str(tmp_path / "skey_out.json")

    result = subprocess.run(
        [
            "uv", "run", RUNNER, "--audio", audio_path, "--out", out_path,
            "--device", "cpu", "--skip-onnx-export",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    with open(out_path, encoding="utf-8") as fh:
        payload = json.load(fh)
    assert payload["onnx_export"] == []
