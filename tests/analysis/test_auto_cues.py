"""Auto-cue proposer tests (META-04)."""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from apps.analysis import auto_cues as ac
from apps.analysis.record import AnalysisRecord


def _rec_for_cues(
    *,
    duration_s: float = 240.0,
    onsets_s: list[float] | None = None,
    downbeats_s: list[float] | None = None,
    rms: list[float] | None = None,
    stable_id: str = "sid",
) -> AnalysisRecord:
    return AnalysisRecord(
        stable_id=stable_id,
        backend="librosa+madmom",
        backend_version="test-1",
        analyzed_at=datetime(2026, 4, 17, tzinfo=UTC),
        duration_s=duration_s,
        sample_rate=44100,
        bpm=120.0, bpm_confidence=0.9,
        key_camelot="8A", key_openkey="1m", key_confidence=0.9,
        energy=6,
        onsets_s=onsets_s or [],
        downbeats_s=downbeats_s or [],
        rms_peaks_s=[],
        features_blob={"rms": rms or [], "rms_hop": 512},
    )


@pytest.mark.requirement("META-04")
def test_max_cues_cap_respected() -> None:
    # 16 evenly spaced onsets across a 240 s track, bin_count=8 -> 8 cues max.
    onsets = [i * 15.0 for i in range(16)]
    rms = [0.5] * 200
    rec = _rec_for_cues(
        onsets_s=onsets, rms=rms,
    )
    tp = ac.propose_cues(rec)
    assert len(tp.cues) <= 8


@pytest.mark.requirement("META-04")
def test_drop_label_on_global_peak() -> None:
    # 8 onsets, one per bin.  Third onset has very high RMS -> drop lands
    # within 0.5 s of its onset time.
    onsets = [5.0, 35.0, 120.0, 150.0, 180.0, 200.0, 220.0, 235.0]
    # Build an RMS envelope with a sharp bump around t=120s.  Envelope is
    # downsampled to 256 frames across the 240s duration.
    rms = [0.05] * 256
    # The peak index corresponds to t=120s -> 120/240 * 255 ~= 128.
    for i in range(120, 140):
        rms[i] = 1.0
    rec = _rec_for_cues(duration_s=240.0, onsets_s=onsets, rms=rms)
    tp = ac.propose_cues(rec)
    drop_cues = [c for c in tp.cues if c.label == "drop"]
    assert len(drop_cues) == 1
    assert abs(drop_cues[0].time_s - 120.0) < 20.0, f"drop at {drop_cues[0].time_s}"


@pytest.mark.requirement("META-04")
def test_min_cue_gap_enforced() -> None:
    # Onsets cluster tight within a single bin; after dedup we should still
    # respect the 100 ms gap pairwise.
    onsets = [10.0, 10.01, 10.02, 40.0, 80.0, 120.0]
    rms = [0.5] * 200
    rec = _rec_for_cues(onsets_s=onsets, rms=rms)
    tp = ac.propose_cues(rec)
    times = sorted(c.time_s for c in tp.cues)
    for a, b in zip(times, times[1:], strict=False):
        assert (b - a) >= 0.1  # 100 ms default


@pytest.mark.requirement("META-04")
def test_intro_label_on_first_bin() -> None:
    onsets = [0.5, 40.0, 80.0, 120.0, 160.0, 200.0, 230.0]
    rms = [0.5] * 200
    rec = _rec_for_cues(onsets_s=onsets, rms=rms)
    tp = ac.propose_cues(rec)
    if tp.cues:
        assert tp.cues[0].label in ("intro", "drop")  # 0.5 s is in first bin


@pytest.mark.requirement("META-04")
def test_snap_to_downbeat_tolerance() -> None:
    # Onset at 10.03 s, downbeat at 10.00 s -> within 60 ms tol -> snap.
    onsets = [10.03]
    rms = [0.5] * 200
    rec = _rec_for_cues(onsets_s=onsets, downbeats_s=[10.0], rms=rms)
    tp = ac.propose_cues(rec)
    assert len(tp.cues) == 1
    assert abs(tp.cues[0].time_s - 10.0) < 1e-6


@pytest.mark.requirement("META-04")
def test_snap_skipped_when_far() -> None:
    # Onset at 10.5 s, downbeat at 10.0 s -> 500 ms off (> 60 ms tol) -> keep onset.
    onsets = [10.5]
    rms = [0.5] * 200
    rec = _rec_for_cues(onsets_s=onsets, downbeats_s=[10.0], rms=rms)
    tp = ac.propose_cues(rec)
    assert len(tp.cues) == 1
    assert abs(tp.cues[0].time_s - 10.5) < 1e-6


@pytest.mark.requirement("META-04")
def test_run_writes_json_and_events(tmp_path: Path) -> None:
    onsets = [0.5, 40.0, 80.0, 120.0, 160.0, 200.0, 230.0]
    rms = [0.5] * 200
    rec1 = _rec_for_cues(stable_id="t1", onsets_s=onsets, rms=rms)
    rec2 = _rec_for_cues(stable_id="t2", onsets_s=onsets, rms=rms)
    db = tmp_path / "s.db"
    out = tmp_path / "cues.json"
    proposals = ac.run(records=[rec1, rec2], output=out, db_path=db)
    assert len(proposals) == 2
    assert out.exists()
    doc = json.loads(out.read_text("utf-8"))
    assert {d["stable_id"] for d in doc} == {"t1", "t2"}

    with sqlite3.connect(str(db)) as conn:
        rows = conn.execute(
            "SELECT event_type, stable_id FROM analysis_events"
        ).fetchall()
    sids = {sid for et, sid in rows if et == "cue.proposal"}
    assert sids == {"t1", "t2"}


@pytest.mark.requirement("META-04")
def test_label_filter(tmp_path: Path) -> None:
    onsets = [0.5, 40.0, 80.0, 120.0, 160.0, 200.0, 230.0]
    rms = [0.5] * 200
    rec = _rec_for_cues(onsets_s=onsets, rms=rms)
    proposals = ac.run(
        records=[rec],
        output=tmp_path / "o.json",
        db_path=tmp_path / "s.db",
        label_filter={"drop"},
        emit_events=False,
    )
    assert len(proposals) == 1
    assert all(c.label == "drop" for c in proposals[0].cues)
