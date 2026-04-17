"""MIK backend unit tests (META-01)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from apps.analysis.backends.base import BackendNotAvailable
from apps.analysis.backends.mik import MikBackend


@pytest.mark.requirement("META-01")
def test_mik_not_installed_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(MikBackend, "_binary_path", classmethod(lambda cls: None))
    with pytest.raises(BackendNotAvailable) as exc:
        MikBackend.analyze(tmp_path / "fake.mp3", "sid_x")
    assert "mixed-in-key-cli" in str(exc.value)
    assert "librosa+madmom" in str(exc.value) or "PATH" in str(exc.value)


@pytest.mark.requirement("META-01")
def test_mik_parses_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(MikBackend, "_binary_path", classmethod(lambda cls: "/tmp/mik"))

    class FakeResult:
        returncode = 0
        stdout = json.dumps({
            "duration_s": 200.0, "sample_rate": 44100,
            "bpm": 128.0, "bpm_confidence": 1.0,
            "key_camelot": "11A", "key_openkey": "11m", "key_confidence": 1.0,
            "energy": 9,
            "onsets_s": [0.0, 1.0], "downbeats_s": [0.0, 2.0], "rms_peaks_s": [1.5],
            "version": "mik-cli-5.2.0",
        })
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeResult())
    rec = MikBackend.analyze(tmp_path / "fake.mp3", "sid1")
    assert rec.bpm == 128.0
    assert rec.energy == 9
    assert rec.energy_source == "mik"
    assert rec.backend == "mik"
    assert rec.key_camelot == "11A"
