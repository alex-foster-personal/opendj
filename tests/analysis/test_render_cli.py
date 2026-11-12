"""``apps.analysis.render_cli`` offline stretch render tests (issue #12231)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from apps.analysis import render_cli as cli
from apps.shared.odj_audio_binary import OdjAudioUnavailable

pytestmark = pytest.mark.requires_canonical_decode

REQ = "TRACK-EDITING-13510"


@pytest.fixture()
def tone_wav(tmp_path: Path) -> Path:
    wav = tmp_path / "tone.wav"
    cli._write_tone_fixture_wav(wav, hz=440.0, seconds=2.0)
    return wav


@pytest.fixture()
def odj_audio() -> Path:
    try:
        return cli._odj_audio()
    except OdjAudioUnavailable as exc:
        if os.environ.get("MDT_REQUIRE_AUDIO_ENGINE_BUILD") == "1":
            pytest.fail(str(exc))
        pytest.skip(str(exc))


def _run_cli(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "apps.analysis.render_cli", *argv],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.requirement(REQ)
def test_key_locked_bpm_render_uses_stretch_not_varispeed(tone_wav: Path, tmp_path: Path, odj_audio: Path) -> None:
    """[if] key-locked BPM render [then] receipt uses stretch not varispeed, [else stop]."""
    del odj_audio
    out = tmp_path / "out.wav"
    proc = _run_cli(
        str(tone_wav),
        str(out),
        "--source-bpm",
        "120",
        "--target-bpm",
        "128",
        "--key-lock",
        "--source-hz",
        "440",
    )
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout.strip())
    assert receipt["engine"] == "stretch"
    assert receipt["varispeed_fallback"] is False
    assert "requested_semitones" in receipt and "measured_semitones" in receipt
    beat_s = 60.0 / 128.0
    assert abs(receipt["measured_duration_s"] - (2.0 * 120.0 / 128.0)) <= beat_s


@pytest.mark.requirement(REQ)
def test_semitone_render_receipt_includes_shifts(tone_wav: Path, tmp_path: Path, odj_audio: Path) -> None:
    """[if] semitone render [then] receipt lists requested and measured shift, [else stop]."""
    del odj_audio
    out = tmp_path / "shifted.wav"
    proc = _run_cli(
        str(tone_wav),
        str(out),
        "--semitones",
        "2",
        "--source-hz",
        "440",
    )
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout.strip())
    assert abs(receipt["requested_semitones"] - 2.0) < 0.1
    assert abs(receipt["measured_semitones"] - 2.0) < 0.6
    assert "measured_duration_s" in receipt


@pytest.mark.requirement(REQ)
def test_refuses_existing_destination(tone_wav: Path, tmp_path: Path) -> None:
    """[if] destination exists [then] CLI refuses without overwriting, [else stop]."""
    dest = tmp_path / "taken.wav"
    dest.write_bytes(b"keep")
    before = tone_wav.read_bytes()
    proc = _run_cli(
        str(tone_wav),
        str(dest),
        "--semitones",
        "1",
        "--source-hz",
        "440",
    )
    assert proc.returncode == cli.EXIT_USAGE
    assert dest.read_bytes() == b"keep"
    assert tone_wav.read_bytes() == before


@pytest.mark.requirement(REQ)
def test_refuses_destination_equal_to_source(tone_wav: Path) -> None:
    """[if] destination equals source [then] CLI refuses and leaves source intact, [else stop]."""
    before = tone_wav.read_bytes()
    proc = _run_cli(
        str(tone_wav),
        str(tone_wav),
        "--semitones",
        "1",
    )
    assert proc.returncode == cli.EXIT_USAGE
    assert tone_wav.read_bytes() == before


@pytest.mark.requirement(REQ)
def test_stretch_unavailable_fails_without_varispeed_receipt(
    tone_wav: Path, tmp_path: Path, odj_audio: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] stretch engine unavailable with key lock [then] CLI fails with no fallback, [else stop]."""
    del odj_audio
    monkeypatch.setenv("ODJ_STRETCH_FORCE_UNAVAILABLE", "1")
    out = tmp_path / "out.wav"
    proc = _run_cli(
        str(tone_wav),
        str(out),
        "--source-bpm",
        "120",
        "--target-bpm",
        "128",
        "--key-lock",
    )
    assert proc.returncode == cli.EXIT_UNAVAILABLE
    assert not out.exists()
    assert "varispeed_fallback" not in proc.stdout


# ----- helpers for sibling issue #12232 (loudness); keep isolated below --------


def test_render_cli_module_exports_fixture_writer() -> None:
    """Guardrail: synthetic fixture helper stays importable for loudness tests."""
    assert callable(cli._write_tone_fixture_wav)
