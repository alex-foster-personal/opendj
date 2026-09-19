"""Bad-beatgrid detector tests (META-02)."""
from __future__ import annotations

import dataclasses
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from apps.analysis import detect_bad_beatgrid as dbg
from apps.analysis.record import AnalysisRecord


def _rec(**overrides) -> AnalysisRecord:
    # Annotated so mypy checks the OVERRIDES against the dataclass rather than
    # inferring `dict[str, object]` and then reporting one arg-type error per
    # distinct field type on the ** call. Widening the record (native-analysis
    # v1) made that count grow with the record instead of with the test.
    base: dict[str, Any] = dict(
        stable_id="sid",
        backend="librosa+madmom",
        backend_version="test-1",
        analyzed_at=datetime(2026, 4, 17, tzinfo=UTC),
        duration_s=180.0,
        sample_rate=44100,
        bpm=120.0, bpm_confidence=0.9,
        key_camelot="8A", key_openkey="8m", key_confidence=0.9,
        energy=6,
        onsets_s=[0.0, 0.5, 1.0, 1.5],
        downbeats_s=[0.0, 2.0, 4.0, 6.0],
        rms_peaks_s=[1.0, 3.0],
        features_blob={"bpm_per_frame": [120.0] * 20, "rms": [0.3] * 32, "rms_hop": 512},
    )
    base.update(overrides)
    return AnalysisRecord(**base)


@pytest.mark.requirement("META-02")
def test_clean_track_emits_no_flag() -> None:
    rec = _rec()
    flag = dbg.detect_one(rec)
    assert flag.reasons == []


@pytest.mark.requirement("META-02")
def test_downbeat_transient_gap_fires() -> None:
    # First onset at 0.0; first downbeat at 4.0 s; at 120 bpm that is 8 beats
    # gap, well over the 2-beat threshold.
    rec = _rec(onsets_s=[0.0, 0.5, 1.0], downbeats_s=[4.0, 6.0, 8.0])
    flag = dbg.detect_one(rec)
    assert dbg.REASON_DOWNBEAT in flag.reasons
    assert flag.first_transient_s == 0.0
    assert flag.first_downbeat_s == 4.0


@pytest.mark.requirement("META-02")
def test_bpm_drift_fires_without_tempo_marker() -> None:
    # Standard deviation across [120, 121, 119, 120, ...] is ~ 0.5,
    # mean ~ 120 -> drift_pct ~ 0.42 % > default 0.3 %.  No inter-frame
    # delta exceeds 5 % of mean, so no marker -> flag fires.
    bpms = [120.0, 121.0, 119.0, 120.0, 121.0, 119.0, 120.5, 119.5] * 4
    rec = _rec(features_blob={
        "bpm_per_frame": bpms, "rms": [0.3] * 64, "rms_hop": 512,
    })
    flag = dbg.detect_one(rec)
    assert dbg.REASON_DRIFT in flag.reasons
    assert flag.bpm_drift_pct is not None and flag.bpm_drift_pct > 0.3


@pytest.mark.requirement("META-02")
def test_bpm_drift_suppressed_by_tempo_marker() -> None:
    # Same drift, but one frame is a deliberate tempo change (>5 % jump).
    bpms = [120.0, 121.0, 140.0, 140.0, 140.0, 140.0]  # jump > 5 %
    rec = _rec(features_blob={
        "bpm_per_frame": bpms, "rms": [0.3] * 64, "rms_hop": 512,
    })
    flag = dbg.detect_one(rec)
    assert dbg.REASON_DRIFT not in flag.reasons


@pytest.mark.requirement("META-02")
def test_cue_off_grid_fires() -> None:
    rec = _rec(bpm=120.0, downbeats_s=[0.0], onsets_s=[0.0])
    # Beat period at 120 bpm is 0.5 s.  A cue at 0.08 s is 80 ms off grid.
    flag = dbg.detect_one(rec, cues_s=[0.08])
    assert dbg.REASON_CUE_OFF_GRID in flag.reasons
    assert flag.max_cue_offset_ms is not None
    assert 79.0 < flag.max_cue_offset_ms < 81.0


@pytest.mark.requirement("META-02")
def test_cue_on_grid_does_not_fire() -> None:
    rec = _rec(bpm=120.0, downbeats_s=[0.0], onsets_s=[0.0])
    # Cue at 0.5 s = 1 beat; 0 ms offset.
    flag = dbg.detect_one(rec, cues_s=[0.5, 1.0, 1.5])
    assert dbg.REASON_CUE_OFF_GRID not in flag.reasons


@pytest.mark.requirement("META-02")
def test_run_writes_csv_and_events(tmp_path: Path) -> None:
    rec_clean = _rec(stable_id="clean")
    rec_bad = dataclasses.replace(
        _rec(stable_id="bad", onsets_s=[0.0], downbeats_s=[4.0]),
        onsets_s=[0.0], downbeats_s=[4.0],
    )
    db = tmp_path / "state.db"
    csv_out = tmp_path / "flags.csv"
    flags = dbg.run(
        records=[rec_clean, rec_bad],
        output=csv_out,
        db_path=db,
    )
    assert len(flags) == 1
    assert flags[0].stable_id == "bad"
    assert csv_out.exists()
    with sqlite3.connect(str(db)) as conn:
        rows = conn.execute(
            "SELECT event_type, stable_id FROM analysis_events"
        ).fetchall()
    assert ("beatgrid.flag", "bad") in rows


@pytest.mark.requirement("META-02")
def test_run_respects_reasons_filter(tmp_path: Path) -> None:
    rec = _rec(stable_id="r", onsets_s=[0.0], downbeats_s=[4.0])
    # DOWNBEAT_TRANSIENT_GAP only.
    flags = dbg.run(
        records=[rec],
        output=tmp_path / "f.csv",
        db_path=tmp_path / "s.db",
        reasons_exclude={dbg.REASON_DOWNBEAT},
        emit_events=False,
    )
    assert flags == []
