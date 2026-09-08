"""Portable librosa default backend contracts (META-01).

Salvaged from PR #412 (commit cac8301d): the ``librosa`` backend must be
the production default everywhere, analyze real audio with only the PyPI
``analysis`` extra installed, and never fabricate downbeat capability it
does not have. The learned ``librosa+madmom`` backend stays explicit-only.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from apps.analysis import auto_cues, detect_bad_beatgrid, run, write_tags
from apps.analysis.backends import DEFAULT_BACKEND, NONSHIPPABLE_ENV, get_backend
from apps.analysis.backends.base import BackendNonshippable
from apps.analysis.backends.librosa import LibrosaBackend
from apps.analysis.record import AnalysisRecord


@pytest.mark.requirement("META-01")
def test_every_production_default_uses_exported_librosa_constant() -> None:
    assert DEFAULT_BACKEND == "librosa"
    assert inspect.signature(run.run).parameters["backend_name"].default == DEFAULT_BACKEND
    assert run._parse_args(["--files", "track.wav"]).backend == DEFAULT_BACKEND
    assert auto_cues._parse_args([]).backend == DEFAULT_BACKEND
    assert detect_bad_beatgrid._parse_args([]).backend == DEFAULT_BACKEND
    assert write_tags._parse_args(["--files", "track.wav"]).backend == DEFAULT_BACKEND

    for module in (run, auto_cues, detect_bad_beatgrid, write_tags):
        source = inspect.getsource(module)
        assert 'default="librosa+madmom"' not in source


@pytest.mark.requirement("META-01")
def test_registry_exposes_real_librosa_default_and_explicit_combined_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert get_backend(DEFAULT_BACKEND) is LibrosaBackend
    monkeypatch.setenv(NONSHIPPABLE_ENV, "1")
    assert get_backend("librosa+madmom").name == "librosa+madmom"
    assert get_backend("mik").name == "mik"


@pytest.mark.requirement("NATIVE-08")
def test_registry_refuses_madmom_without_the_nonshippable_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(NONSHIPPABLE_ENV, raising=False)
    with pytest.raises(BackendNonshippable, match="CC BY-NC-SA"):
        get_backend("librosa+madmom")
    # control: the portable default is never gated by the same flag.
    assert get_backend(DEFAULT_BACKEND) is LibrosaBackend


@pytest.mark.requirement("META-01")
def test_librosa_default_analyzes_real_click_without_fabricated_downbeats(
    click_120_path: Path,
) -> None:
    record = LibrosaBackend.analyze(click_120_path, "librosa-120")

    assert isinstance(record, AnalysisRecord)
    assert record.backend == DEFAULT_BACKEND
    assert record.backend_version.startswith("librosa==")
    assert "+numpy==" in record.backend_version
    assert "+scipy==" in record.backend_version
    assert any(abs(record.bpm - bpm) < 3.0 for bpm in (60.0, 120.0, 240.0))
    assert record.onsets_s
    assert record.key_camelot and record.key_openkey
    assert 1 <= record.energy <= 10
    assert record.downbeats_s == []
    assert record.features_blob["downbeat_tracking"] is False


@pytest.mark.requirement("META-02")
@pytest.mark.requirement("META-04")
def test_downstream_consumers_do_not_claim_absent_downbeat_capability(
    click_120_path: Path,
) -> None:
    record = LibrosaBackend.analyze(click_120_path, "librosa-downstream")

    proposal = auto_cues.propose_cues(record)
    assert all(cue.time_s in record.onsets_s for cue in proposal.cues)
    flag = detect_bad_beatgrid.detect_one(record)
    assert detect_bad_beatgrid.REASON_DOWNBEAT not in flag.reasons
