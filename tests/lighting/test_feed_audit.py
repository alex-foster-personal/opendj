"""Beat feed timing audit tests (issue #14385).

[if] the feed audit misses prediction error, a missing or doubled beat, a stopped-deck beat or a late section [then] fail, [else stop].
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from apps.lighting.beat_clock import NS_PER_MS, DeckSample, predict_beats
from apps.lighting.feed_audit import SectionEmit, audit_feed
from apps.lighting.sections import LightSection, resolve_sections

BPM = 128.0
BEAT_MS = 60_000.0 / BPM
BARS = 64
T0_NS = 5_000_000_000
TICK_MS = 1000.0 / 30.0

pytestmark = [pytest.mark.requirement("LIGHT-14455")]


def _grid(beats: int = BARS * 4) -> list[dict[str, float]]:
    return [{"n": i % 4 + 1, "time_ms": 100.0 + i * BEAT_MS, "bpm": BPM} for i in range(beats)]


def _trace(grid, *, rate: float = 1.0, stop_after_ms: float | None = None):
    """30 Hz samples over the grid with injected host time; the feed predicts one tick ahead."""
    end_ms = grid[-1]["time_ms"] + BEAT_MS
    samples, beats, pos, host_ns = [], [], 0.0, T0_NS
    while pos < end_ms:
        playing = stop_after_ms is None or pos < stop_after_ms
        s = DeckSample("A", pos, rate, playing, host_ns)
        samples.append(s)
        beats.extend(predict_beats(grid, s, TICK_MS))
        pos, host_ns = pos + TICK_MS * rate, host_ns + round(TICK_MS * NS_PER_MS)
    samples.append(DeckSample("A", pos, rate, stop_after_ms is None, host_ns))
    return samples, beats


def test_prediction_error_within_one_ms_on_fixture_trace() -> None:
    """[if] beats come from the grid maths [then] max error is at most 1 ms and a 3 ms shift is flagged, [else stop]."""
    grid = _grid()
    samples, beats = _trace(grid, rate=1.04)
    report = audit_feed(grid, samples, beats)
    assert report.ok, report.findings
    assert report.beats_checked == BARS * 4
    assert report.max_error_ms <= 1.0
    beats[10] = replace(beats[10], at_host_ns=beats[10].at_host_ns + 3 * NS_PER_MS)
    bad = audit_feed(grid, samples, beats)
    assert bad.kinds() == {"prediction_error"}
    assert 2.9 < bad.max_error_ms < 3.1


def test_flags_missing_or_doubled_beat_over_64_bars() -> None:
    """[if] a 64-bar replay drops one beat or sends one twice [then] missing_beat or doubled_beat fires, [else stop]."""
    grid = _grid()
    samples, beats = _trace(grid)
    assert audit_feed(grid, samples, beats).ok
    missing = audit_feed(grid, samples, beats[:100] + beats[101:])
    assert [(f.kind, f.beat_index) for f in missing.findings] == [("missing_beat", 100)]
    doubled = audit_feed(grid, samples, [*beats, beats[200]])
    assert [(f.kind, f.beat_index) for f in doubled.findings] == [("doubled_beat", 200)]


def test_flags_beat_for_stopped_deck() -> None:
    """[if] a beat is sent while the latest sample says the deck is stopped [then] stopped_deck fires, [else stop]."""
    grid = _grid(64)
    samples, beats = _trace(grid, stop_after_ms=grid[32]["time_ms"] - BEAT_MS / 2)
    assert audit_feed(grid, samples, beats).ok
    stopped = next(s for s in samples if not s.playing)
    ghost = replace(beats[-1], beat_index=40, at_host_ns=stopped.host_time_ns + 10 * NS_PER_MS)
    report = audit_feed(grid, samples, [*beats, ghost])
    assert [(f.kind, f.beat_index) for f in report.findings] == [("stopped_deck", 40)]


def test_flags_section_event_more_than_one_beat_from_boundary() -> None:
    """[if] a section event lands over one beat from its span boundary [then] section_off_boundary fires, [else stop]."""
    grid = _grid()
    samples, beats = _trace(grid)
    drop_ms = grid[64]["time_ms"]
    spans = resolve_sections(
        {
            "phrases": [
                {"start_s": 0.0, "end_s": drop_ms / 1000, "label": "buildup"},
                {"start_s": drop_ms / 1000, "end_s": 200.0, "label": "drop"},
            ]
        }
    )

    def emit(track_ms: float) -> SectionEmit:
        return SectionEmit("A", LightSection.DROP, T0_NS + round(track_ms * NS_PER_MS))

    near = audit_feed(grid, samples, beats, spans=spans, section_emits=[emit(drop_ms + 0.9 * BEAT_MS)])
    assert near.ok, near.findings
    late = audit_feed(grid, samples, beats, spans=spans, section_emits=[emit(drop_ms + 1.5 * BEAT_MS)])
    assert late.kinds() == {"section_off_boundary"}
