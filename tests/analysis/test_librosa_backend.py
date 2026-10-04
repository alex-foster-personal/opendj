"""Portable librosa default backend contracts (META-01).

Salvaged from PR #412 (commit cac8301d): the ``librosa`` backend must be
the production default everywhere, analyze real audio with only the PyPI
``analysis`` extra installed, and never fabricate downbeat capability it
does not have. The learned ``librosa+madmom`` backend stays explicit-only.
"""
from __future__ import annotations

import inspect
import json
import math
import os
import struct
import subprocess
import sys
import wave
from pathlib import Path

import pytest

from apps.analysis import auto_cues, detect_bad_beatgrid, run
from apps.analysis.backends import DEFAULT_BACKEND, NONSHIPPABLE_ENV, get_backend
from apps.analysis.backends import librosa as librosa_backend_module
from apps.analysis.backends.librosa import LibrosaBackend, estimate_downbeats_from_beats
from apps.analysis.record import AnalysisRecord
from apps.analysis_key.canon import from_camelot, from_open_key

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
def test_every_production_default_uses_exported_librosa_constant() -> None:
    assert DEFAULT_BACKEND == "librosa"
    assert inspect.signature(run.run).parameters["backend_name"].default == DEFAULT_BACKEND
    assert run._parse_args(["--files", "track.wav"]).backend == DEFAULT_BACKEND
    assert auto_cues._parse_args([]).backend == DEFAULT_BACKEND
    assert detect_bad_beatgrid._parse_args([]).backend == DEFAULT_BACKEND

    for module in (run, auto_cues, detect_bad_beatgrid):
        source = inspect.getsource(module)
        assert 'default="librosa+madmom"' not in source


@pytest.mark.requirement("META-01")
def test_registry_exposes_real_librosa_default_and_explicit_combined_backend() -> None:
    assert get_backend(DEFAULT_BACKEND) is LibrosaBackend
    result = _resolve_madmom_backend_in_subprocess({**os.environ, NONSHIPPABLE_ENV: "1"})
    assert result == {"ok": True, "name": "librosa+madmom"}
    assert get_backend("mik").name == "mik"


@pytest.mark.requirement("NATIVE-08")
def test_registry_refuses_madmom_without_the_nonshippable_flag() -> None:
    env = {k: v for k, v in os.environ.items() if k != NONSHIPPABLE_ENV}
    result = _resolve_madmom_backend_in_subprocess(env)
    assert result["ok"] is False
    assert "CC BY-NC-SA" in str(result["message"])
    # control: the portable default is never gated by the same flag.
    assert get_backend(DEFAULT_BACKEND) is LibrosaBackend


def _expected_bar_downbeats(bpm: float, duration_s: float) -> list[float]:
    period = 60.0 / bpm
    downbeats: list[float] = []
    beat = 0
    t0 = 0.0
    while t0 < duration_s:
        if beat % 4 == 0:
            downbeats.append(t0)
        beat += 1
        t0 += period
    return downbeats


def _write_accented_bar_click_track(path: Path, *, bpm: float = 128.0, duration_s: float = 20.0) -> None:
    """Four-on-the-floor click with a stronger transient on beat 1 of each bar."""
    sample_rate = 44_100
    period = 60.0 / bpm
    n_samples = int(duration_s * sample_rate)
    samples = [0.0] * n_samples
    beat = 0
    t0 = 0.0
    while t0 < duration_s:
        start = int(t0 * sample_rate)
        burst = int(0.03 * sample_rate)
        is_downbeat = beat % 4 == 0
        freq = 1800.0 if is_downbeat else 1200.0
        amp = 0.9 if is_downbeat else 0.45
        for i in range(burst):
            if start + i >= n_samples:
                break
            envelope = math.exp(-i / (0.01 * sample_rate))
            samples[start + i] += amp * envelope * math.sin(2 * math.pi * freq * i / sample_rate)
        beat += 1
        t0 += period

    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(
            b"".join(struct.pack("<h", int(max(-1.0, min(1.0, sample)) * 32000)) for sample in samples)
        )


@pytest.mark.requirement("META-01")
def test_librosa_default_analyzes_uniform_click_without_fabricated_downbeats(
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


@pytest.mark.requirement("META-01")
def test_librosa_default_detects_downbeats_on_accented_bar_audio(tmp_path: Path) -> None:
    path = tmp_path / "accented_128bpm.wav"
    _write_accented_bar_click_track(path, bpm=128.0, duration_s=20.0)
    record = LibrosaBackend.analyze(path, "librosa-accented")

    assert record.downbeats_s
    assert record.downbeats_s == sorted(record.downbeats_s)
    assert len(record.downbeats_s) == len(set(record.downbeats_s))
    assert record.features_blob["downbeat_tracking"] is True
    assert abs(record.bpm - 128.0) < 3.0
    expected = _expected_bar_downbeats(128.0, 20.0)
    bar_period_s = 4 * (60.0 / 128.0)
    assert len(record.downbeats_s) >= 3
    for measured in record.downbeats_s:
        assert any(abs(measured - reference) < 0.08 for reference in expected)
    for index in range(len(record.downbeats_s) - 1):
        spacing = record.downbeats_s[index + 1] - record.downbeats_s[index]
        assert abs(spacing - bar_period_s) < 0.12


@pytest.mark.requirement("META-01")
def test_downbeat_estimator_rejects_ambiguous_phase_scores() -> None:
    beats = [float(index) * 0.5 for index in range(16)]
    uniform = [1.0] * len(beats)
    downbeats, tracked = estimate_downbeats_from_beats(beats, uniform)
    assert downbeats == []
    assert tracked is False

    ambiguous = [1.0, 1.2, 1.0, 1.2, 1.0, 1.2, 1.0, 1.2, 1.0, 1.2, 1.0, 1.2]
    downbeats, tracked = estimate_downbeats_from_beats(beats[:12], ambiguous)
    assert downbeats == []
    assert tracked is False

    clear = [2.0, 1.0, 1.0, 1.0, 2.0, 1.0, 1.0, 1.0, 2.0, 1.0, 1.0, 1.0]
    downbeats, tracked = estimate_downbeats_from_beats(beats[:12], clear)
    assert tracked is True
    assert downbeats == [beats[index] for index in range(0, 12, 4)]


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


@pytest.mark.requirement("META-01")
@pytest.mark.parametrize("pitch_class", range(12))
def test_camelot_and_open_key_tables_agree_with_canon_for_every_pitch_class(
    pitch_class: int,
) -> None:
    """Regression for a real bug: the two tables here are hand-maintained
    separately from apps/analysis_key/canon.py's, so a fix to one (like the
    Open Key table correction above) can silently leave the other stale for
    every pitch class, not just whichever one a caller happens to notice."""
    for camelot_table, openkey_table in (
        (librosa_backend_module._CAMELOT_MAJOR, librosa_backend_module._OPENKEY_MAJOR),
        (librosa_backend_module._CAMELOT_MINOR, librosa_backend_module._OPENKEY_MINOR),
    ):
        camelot = camelot_table[pitch_class]
        open_key = openkey_table[pitch_class]
        assert from_camelot(camelot) == from_open_key(open_key)
