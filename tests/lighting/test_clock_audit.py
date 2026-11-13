"""Tests for apps.lighting.clock_audit.

[if] a MIDI clock schedule is audited against its beats [then] jitter, drift, re-anchors and violations are reported, [else stop].
"""

from __future__ import annotations

import json

import pytest

from apps.lighting.beat_clock import BeatEvent
from apps.lighting.clock_audit import ClockAuditReport, audit_clock
from apps.lighting.midi_clock import TIMING_CLOCK, ClockMessage, clocks_for_beats

pytestmark = [pytest.mark.requirement("LIGHT-14551")]


def _beats(bpms: list[float], start_ns: int = 1_000_000_000) -> list[BeatEvent]:
    beats: list[BeatEvent] = []
    at = start_ns
    for index, bpm in enumerate(bpms):
        bar_beat = index % 4 + 1
        beats.append(BeatEvent("A", index, bar_beat, bar_beat == 1, bpm, at))
        at += round(60_000_000_000 / bpm)
    return beats


def test_steady_128_bpm_has_24_ppqn_no_jitter_no_drift() -> None:
    """[if] 8 beats at 128 BPM are scheduled [then] audit reports 24 PPQN, zero jitter and zero drift, [else stop]."""
    beats = _beats([128.0] * 8)
    report = audit_clock(clocks_for_beats(beats), beats)
    assert report.ppqn == 24
    assert report.max_jitter_ns == 0
    assert report.drift_ppm == 0
    assert report.violations == []


def test_tempo_step_reports_reanchor_and_no_wide_gap() -> None:
    """[if] tempo steps from 128 to 140 BPM [then] the re-anchor beat time is reported and no gap exceeds 1.5 ticks, [else stop]."""
    beats = _beats([128.0] * 4 + [140.0] * 4)
    report = audit_clock(clocks_for_beats(beats), beats)
    assert report.reanchor_host_ns == [beats[4].at_host_ns]
    assert report.max_gap_ticks <= 1.5
    assert report.violations == []


def test_reversed_and_duplicate_timestamps_are_flagged() -> None:
    """[if] a schedule has a reversed and a duplicate clock timestamp [then] both are listed as violations, [else stop]."""
    beats = _beats([128.0] * 2)
    clocks = clocks_for_beats(beats)
    tick = bytes((TIMING_CLOCK,))
    bad = [*clocks[:5], ClockMessage(clocks[4].at_host_ns, tick), ClockMessage(clocks[2].at_host_ns, tick), *clocks[5:]]
    report = audit_clock(bad, beats)
    assert any("reversed" in v for v in report.violations)
    assert any("duplicate" in v for v in report.violations)


def test_report_is_json_serialisable_dataclass() -> None:
    """[if] a report is built [then] it is a dataclass with max_jitter_ns, drift_ppm and violations that json.dumps accepts, [else stop]."""
    beats = _beats([128.0] * 2)
    report = audit_clock(clocks_for_beats(beats), beats)
    assert isinstance(report, ClockAuditReport)
    payload = json.loads(json.dumps(report.to_dict()))
    assert {"max_jitter_ns", "drift_ppm", "violations"} <= payload.keys()
