"""librosa+madmom backend tests (META-01).

The three tests that actually call ``LibrosaMadmomBackend.analyze`` are
marked ``requires_madmom``: madmom is a git-HEAD install (requirements.txt,
--no-build-isolation) that no pyproject extra can supply, so a `uv sync`
venv has librosa but no beat tracker. CI installs it and runs them; the
pure helper tests below need neither and always run.

We tolerate madmom's +/- few-BPM noise on short synthetic clicks + the
half / double tempo ambiguity it sometimes falls into.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from apps.analysis.backends import NONSHIPPABLE_ENV, get_backend
from apps.analysis.backends.base import TrackTooLong
from apps.analysis.backends.librosa import _energy_from_rms_dbfs, _estimate_key
from apps.analysis.backends.librosa_madmom import LibrosaMadmomBackend
from apps.analysis.record import AnalysisRecord

REPO_ROOT: Path = Path(__file__).resolve().parents[2]

_GET_MADMOM_BACKEND_PROBE = """
import json
from apps.analysis.backends import get_backend
from apps.analysis.backends.base import BackendNonshippable
try:
    backend = get_backend("librosa+madmom")
    print(json.dumps({"ok": True, "name": backend.name}))
except BackendNonshippable as exc:
    print(json.dumps({"ok": False, "message": str(exc)}))
"""


def _resolve_madmom_backend_in_subprocess(env: dict[str, str]) -> dict[str, object]:
    """Exercise the real registry in its own process -- AGENTS.md's
    fail-closed test contract bans monkeypatching, so the flag must be a
    genuine subprocess environment variable, never an in-process patch."""
    proc = subprocess.run(
        [sys.executable, "-c", _GET_MADMOM_BACKEND_PROBE],
        capture_output=True, text=True, cwd=REPO_ROOT, env=env, timeout=60, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.requirement("META-01")
def test_registry_has_both_backends() -> None:
    result = _resolve_madmom_backend_in_subprocess({**os.environ, NONSHIPPABLE_ENV: "1"})
    assert result == {"ok": True, "name": "librosa+madmom"}
    assert get_backend("mik").name == "mik"


@pytest.mark.requirement("NATIVE-08")
def test_registry_refuses_madmom_without_the_flag_and_names_the_license() -> None:
    env = {k: v for k, v in os.environ.items() if k != NONSHIPPABLE_ENV}
    result = _resolve_madmom_backend_in_subprocess(env)
    assert result["ok"] is False
    assert "CC BY-NC-SA" in str(result["message"])
    assert "MDT_BENCH_NONSHIPPABLE" in str(result["message"])


@pytest.mark.requirement("NATIVE-08")
@pytest.mark.parametrize("value", ["0", "false", "", "no"])
def test_registry_refuses_madmom_on_any_falsy_flag_value(value: str) -> None:
    """[if] the flag is set to anything other than the literal "1" [then]
    the registry still refuses, [else stop] -- "0", "false" and "" must not
    be read as opt-in."""
    result = _resolve_madmom_backend_in_subprocess({**os.environ, NONSHIPPABLE_ENV: value})
    assert result["ok"] is False


@pytest.mark.requirement("META-01")
@pytest.mark.requires_madmom
def test_click_120_bpm(click_120_path: Path) -> None:
    rec = LibrosaMadmomBackend.analyze(click_120_path, "sid120")
    assert isinstance(rec, AnalysisRecord)
    assert any(abs(rec.bpm - x) < 3.0 for x in (120.0, 60.0, 240.0)), f"bpm={rec.bpm}"
    assert len(rec.onsets_s) > 0
    assert rec.duration_s > 10.0
    assert rec.sample_rate == 44100
    assert rec.key_camelot and rec.key_openkey
    assert 1 <= rec.energy <= 10
    assert "+madmom==" in rec.backend_version
    assert rec.features_blob["downbeat_tracking"] is True


@pytest.mark.requirement("META-01")
@pytest.mark.requires_madmom
def test_click_90_bpm(click_90_path: Path) -> None:
    rec = LibrosaMadmomBackend.analyze(click_90_path, "sid90")
    assert any(abs(rec.bpm - x) < 3.0 for x in (90.0, 45.0, 180.0)), f"bpm={rec.bpm}"


@pytest.mark.requirement("META-01")
@pytest.mark.requires_madmom
def test_too_long_track_raises(
    click_120_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apps.analysis import config as cfg
    monkeypatch.setattr(cfg, "load_config", lambda _=None: {
        "analyzer": {"sample_rate_hz": 44100, "mono": True, "max_track_minutes": 0.01},
        "energy": {"rms_dbfs_bins": [[0.0, 10]]},
    })
    with pytest.raises(TrackTooLong):
        LibrosaMadmomBackend.analyze(click_120_path, "sidlong")


@pytest.mark.requirement("META-01")
def test_energy_bin_boundaries() -> None:
    bins = [[-30.0, 1], [-20.0, 5], [0.0, 10]]
    assert _energy_from_rms_dbfs(-35.0, bins) == 1
    assert _energy_from_rms_dbfs(-25.0, bins) == 5
    assert _energy_from_rms_dbfs(-10.0, bins) == 10


@pytest.mark.requirement("META-01")
def test_estimate_key_shape() -> None:
    chroma = np.zeros((12, 100), dtype=np.float64)
    chroma[0, :] = 1.0
    cam, ok, conf = _estimate_key(chroma)
    assert cam[-1] in ("A", "B")
    assert ok[-1] in ("m", "d")
    assert 0.0 <= conf <= 1.0
