"""AnalysisRecord schema + JSON round-trip (META-01)."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from apps.analysis.record import AnalysisRecord


def _sample() -> AnalysisRecord:
    return AnalysisRecord(
        stable_id="pathid_abc",
        backend="librosa+madmom",
        backend_version="librosa==0.10.2+madmom==0.17.dev",
        analyzed_at=datetime(2026, 4, 17, 1, 23, 45, tzinfo=UTC),
        duration_s=123.45,
        sample_rate=44100,
        bpm=128.0,
        bpm_confidence=0.95,
        key_camelot="8A",
        key_openkey="1m",
        key_confidence=0.88,
        energy=7,
        onsets_s=[0.5, 1.0],
        downbeats_s=[0.0],
        rms_peaks_s=[1.2],
        features_blob={"rms": [0.1, 0.2]},
    )


@pytest.mark.requirement("META-01")
def test_roundtrip_bytes_identical() -> None:
    rec = _sample()
    j = rec.to_json()
    rec2 = AnalysisRecord.from_json(j)
    assert rec2.to_json() == j


@pytest.mark.requirement("META-01")
def test_json_is_sorted_keys() -> None:
    j = _sample().to_json()
    assert j.index('"analyzed_at"') < j.index('"backend"')


@pytest.mark.requirement("META-01")
def test_naive_datetime_coerced_to_utc() -> None:
    rec = AnalysisRecord(
        stable_id="x", backend="b", backend_version="v",
        analyzed_at=datetime(2026, 1, 1, 0, 0, 0),
        duration_s=1.0, sample_rate=44100,
        bpm=120.0, bpm_confidence=1.0,
        key_camelot="1A", key_openkey="6m", key_confidence=1.0,
        energy=5,
    )
    rec2 = AnalysisRecord.from_json(rec.to_json())
    assert rec2.analyzed_at.tzinfo is not None


@pytest.mark.requirement("META-01")
def test_accepts_Z_and_plus00() -> None:
    body = (
        '{"analyzed_at":$DT,"backend":"b","backend_version":"v","bpm":120.0,'
        '"bpm_confidence":1.0,"downbeats_s":[],"duration_s":1.0,"energy":5,'
        '"energy_source":"inferred","features_blob":{},"key_camelot":"1A",'
        '"key_confidence":1.0,"key_openkey":"6m","onsets_s":[],'
        '"rms_peaks_s":[],"sample_rate":44100,"stable_id":"x"}'
    )
    r1 = AnalysisRecord.from_json(body.replace("$DT", '"2026-04-17T01:23:45Z"'))
    r2 = AnalysisRecord.from_json(body.replace("$DT", '"2026-04-17T01:23:45+00:00"'))
    assert r1.analyzed_at == r2.analyzed_at
