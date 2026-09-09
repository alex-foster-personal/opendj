"""Fixtures for the native-analysis v1 contract tests.

Why this directory is not ``tests/analysis``: that package's ``conftest.py``
does ``pytest.importorskip("soundfile")`` at MODULE level, so a venv without
the analysis extra skips the entire directory and pytest still exits 0 with
"no tests ran". The fast CI lane also passes ``--ignore=tests/analysis``.
Either would turn every test below into a check that cannot fail, which is
exactly what `.claude/rules/verification.md` forbids. These tests need no
audio stack at all -- they are pure dataclass, SQL and selection logic -- so
they live where they actually run.

-Claude
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

import pytest

from apps.analysis import store as store_mod
from apps.analysis.lanes import LaneResult
from apps.analysis.record import AnalysisRecord

STAMP = datetime(2026, 9, 9, 12, 0, 0, tzinfo=timezone.utc)


def beatgrid_payload(bpm: float = 128.0, tempo_changes: int = 0) -> dict[str, Any]:
    return {
        "beats": [
            {"t": round(i * 60.0 / bpm, 3), "n": (i % 4) + 1, "bpm": bpm}
            for i in range(8)
        ],
        "bpm": bpm,
        "bpm_confidence": 0.93,
        "octave_reason": "beat interval admits only one octave in 60..200",
        "first_downbeat_s": 0.0,
        "tempo_changes": [
            {"at_s": 120.0 + i, "bpm_before": bpm, "bpm_after": bpm * 1.03,
             "confidence": 0.8}
            for i in range(tempo_changes)
        ],
        "static_grid_untrusted": tempo_changes > 0,
    }


def key_payload(camelot: str = "8A", segments: int = 1) -> dict[str, Any]:
    return {
        "camelot": camelot,
        "openkey": "1m",
        "pitch_class": 9,
        "is_minor": True,
        "confidence": 0.77,
        "segments": {
            "status": "ok",
            "reason": None,
            "segments": [
                {
                    "start_bar": i * 16, "end_bar": (i + 1) * 16,
                    "start_s": i * 30.0, "end_s": (i + 1) * 30.0,
                    "key_camelot": camelot, "key_openkey": "1m",
                    "confidence": 0.7,
                }
                for i in range(segments)
            ],
        },
    }


def loudness_payload(lufs: float = -8.2, dbtp: float = -0.3) -> dict[str, Any]:
    return {
        "integrated_lufs": lufs,
        "true_peak_dbtp": dbtp,
        "loudness_range_lu": 5.1,
        "rms_db": -11.4,
    }


def waveform_payload() -> dict[str, Any]:
    return {
        "kind": "tri",
        "preview": {"length": 3, "low": [1, 2, 3], "mid": [1, 2, 3], "high": [1, 2, 3]},
        "detail": {"length": 2, "low": [4, 5], "mid": [4, 5], "high": [4, 5]},
    }


def own_record(
    *,
    stable_id: str = "t1",
    lane: str = "beatgrid",
    producer: str = "inapp",
    version: str = "1.0.0",
    result: LaneResult | None = None,
    uses_model: bool = False,
    model_sha256: str | None = None,
    decode_fingerprint: str = "sha256:decode-fixture",
    backend: str | None = None,
) -> AnalysisRecord:
    """A minimal, contract-valid own record. Every field is stated, not defaulted."""
    if result is None:
        payloads = {
            "beatgrid": beatgrid_payload,
            "key": key_payload,
            "loudness": loudness_payload,
            "waveform": waveform_payload,
            "vocal": dict,
        }
        result = LaneResult(status="ok", payload=payloads[lane]())
    if backend is None:
        backend = f"own_{lane}.{producer}"
    return AnalysisRecord(
        stable_id=stable_id,
        backend=backend,
        backend_version=version,
        analyzed_at=STAMP,
        duration_s=300.0,
        sample_rate=44100,
        bpm=128.0,
        bpm_confidence=0.93,
        key_camelot="8A",
        key_openkey="1m",
        key_confidence=0.77,
        energy=6,
        energy_source="inferred",
        producer=producer,  # type: ignore[arg-type]
        producer_version=version,
        uses_model=uses_model,
        model_sha256=model_sha256,
        decode_fingerprint=decode_fingerprint,
        lanes={lane: result},
    )


@pytest.fixture()
def db(tmp_path) -> sqlite3.Connection:
    """A real state.db with the Phase 5 + analysis schemas applied."""
    conn = store_mod.open_conn(tmp_path / "state.db")
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def _clean_toggles():
    """Every test starts at the launch state, and leaves it there."""
    from apps.analysis import selection

    selection.reset_toggles()
    yield
    selection.reset_toggles()
