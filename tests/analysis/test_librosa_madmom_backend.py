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

from pathlib import Path

import numpy as np
import pytest

from apps.analysis.backends import NONSHIPPABLE_ENV, get_backend
from apps.analysis.backends.base import BackendNonshippable, TrackTooLong
from apps.analysis.backends.librosa import _energy_from_rms_dbfs, _estimate_key
from apps.analysis.backends.librosa_madmom import LibrosaMadmomBackend
from apps.analysis.record import AnalysisRecord


@pytest.mark.requirement("META-01")
def test_registry_has_both_backends(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(NONSHIPPABLE_ENV, "1")
    assert get_backend("librosa+madmom").name == "librosa+madmom"
    assert get_backend("mik").name == "mik"


@pytest.mark.requirement("NATIVE-08")
def test_registry_refuses_madmom_without_the_flag_and_names_the_license(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(NONSHIPPABLE_ENV, raising=False)
    with pytest.raises(BackendNonshippable) as excinfo:
        get_backend("librosa+madmom")
    assert "CC BY-NC-SA" in str(excinfo.value)
    assert "MDT_BENCH_NONSHIPPABLE" in str(excinfo.value)


@pytest.mark.requirement("NATIVE-08")
def test_registry_refuses_madmom_on_any_falsy_flag_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[if] the flag is set to anything other than the literal "1" [then]
    the registry still refuses, [else stop] -- "0", "false" and "" must not
    be read as opt-in."""
    for value in ("0", "false", "", "no"):
        monkeypatch.setenv(NONSHIPPABLE_ENV, value)
        with pytest.raises(BackendNonshippable):
            get_backend("librosa+madmom")


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
