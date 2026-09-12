"""CLI: `python -m apps.parity score --payload` prints a real report."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tests.parity.payloads import MEASURED_AT

pytestmark = pytest.mark.requirement("PARITY-01")


def test_score_cli_prints_denominators_and_the_measurement_date() -> None:
    """[if] the CLI scores a fixture payload [then] stdout names each scored
    lane's denominator, the measurement date, and the scorer version, and
    does not claim parity.
    """
    path = Path(__file__).parent / "fixtures" / "round1_waveform.json"
    done = subprocess.run(
        [sys.executable, "-m", "apps.parity", "score", "--payload", str(path)],
        check=False,
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[2],
    )
    assert done.returncode == 0, done.stderr
    out = done.stdout
    assert MEASURED_AT in out
    assert "scorer" in out.lower()
    assert "bpm" in out.lower()
    assert "key" in out.lower()
    assert "waveform_preview" in out
    assert "waveform_detail" in out
    assert "waveform_triband" in out
    assert "phrase" in out.lower()
    assert "cues_db" in out.lower()
    assert "scored" in out
    assert "at parity" not in out.lower()
    assert "delegated" in out.lower()


def test_score_cli_round1_cues_lanes() -> None:
    path = Path(__file__).parent / "fixtures" / "round1.json"
    done = subprocess.run(
        [sys.executable, "-m", "apps.parity", "score", "--payload", str(path)],
        check=False,
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[2],
    )
    assert done.returncode == 0, done.stderr
    out = done.stdout
    assert "cues_db" in out
    assert "cues_anlz" in out
    assert "scored" in out
    assert "missing_djmd_cue" in out or "unreadable_ext" in out
    assert "at parity" not in out.lower()
    assert "of 9986" not in out
    assert "of 10479" not in out


def test_score_cli_round1_vocal_fixture_exits_zero() -> None:
    """The round-1 vocal fixture scores through the CLI without claiming parity."""
    path = Path(__file__).parent / "fixtures" / "round1-vocal.json"
    done = subprocess.run(
        [sys.executable, "-m", "apps.parity", "score", "--payload", str(path)],
        check=False,
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[2],
    )
    assert done.returncode == 0, done.stderr
    out = done.stdout
    assert "vocal" in out.lower()
    assert "tracks with rekordbox PVDI among present audio" in out
    assert "at parity" not in out.lower()


def test_score_cli_round1_phrase_fixture_exits_zero() -> None:
    """The round-1 phrase fixture scores through the CLI without claiming parity."""
    path = Path(__file__).parent / "fixtures" / "round1-phrase.json"
    done = subprocess.run(
        [sys.executable, "-m", "apps.parity", "score", "--payload", str(path)],
        check=False,
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[2],
    )
    assert done.returncode == 0, done.stderr
    out = done.stdout.lower()
    assert "phrase" in out
    assert "at parity" not in out
