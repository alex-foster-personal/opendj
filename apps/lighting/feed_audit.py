"""Beat feed timing audit over a replay trace (issue #14385, spec 01 section 5).

A trace is what the feed saw and what it emitted: the deck's position samples, its beatgrid, the
``BeatEvent`` list it sent and the section changes it sent. ``audit_feed`` checks those against the
grid maths and reports the hard vetoes: prediction error over the limit, a missing or doubled beat,
a beat for a stopped deck, a section event more than one beat from its boundary. Pure; no I/O.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import pairwise

from apps.lighting.beat_clock import NS_PER_MS, BeatEvent, DeckSample, GridBeat
from apps.lighting.sections import LightSection, SectionSpan

MAX_PREDICTION_ERROR_MS = 1.0


@dataclass(frozen=True)
class SectionEmit:
    """A section change the feed sent for a deck, stamped with host time."""

    deck: str
    section: LightSection
    at_host_ns: int


@dataclass(frozen=True)
class Finding:
    """One audit veto."""

    kind: str
    deck: str
    detail: str
    beat_index: int | None = None


@dataclass
class AuditReport:
    """Result of one audit: worst prediction error and every veto."""

    max_error_ms: float = 0.0
    beats_checked: int = 0
    findings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when no veto fired."""
        return not self.findings

    def kinds(self) -> set[str]:
        """The distinct finding kinds."""
        return {f.kind for f in self.findings}


def _sample_before(samples: Sequence[DeckSample], times: list[int], host_ns: int) -> DeckSample | None:
    i = bisect_right(times, host_ns) - 1
    return samples[i] if i >= 0 else None


def _track_ms(sample: DeckSample, host_ns: int) -> float:
    return sample.position_ms + (host_ns - sample.host_time_ns) / NS_PER_MS * sample.rate


def _expected_indexes(grid_ms: list[float], samples: Sequence[DeckSample]) -> set[int]:
    """Grid beats whose track time a playing stretch between consecutive samples covered."""
    out: set[int] = set()
    for a, b in pairwise(samples):
        if not a.playing or a.rate <= 0:
            continue
        lo, hi = a.position_ms, b.position_ms
        out.update(i for i, t in enumerate(grid_ms) if lo <= t < hi)
    return out


def audit_feed(
    grid: Sequence[GridBeat],
    samples: Sequence[DeckSample],
    beats: Sequence[BeatEvent],
    *,
    spans: Sequence[SectionSpan] = (),
    section_emits: Sequence[SectionEmit] = (),
    max_error_ms: float = MAX_PREDICTION_ERROR_MS,
) -> AuditReport:
    """Audit one deck's emitted beats and section events against its grid and samples."""
    report = AuditReport()
    ordered = sorted(samples, key=lambda s: s.host_time_ns)
    if not ordered:
        return report
    deck = ordered[0].deck
    times = [s.host_time_ns for s in ordered]
    grid_ms = [float(g["time_ms"]) for g in grid]
    deck_beats = [b for b in beats if b.deck == deck]
    for beat in deck_beats:
        sample = _sample_before(ordered, times, beat.at_host_ns)
        if sample is None or not sample.playing or sample.rate <= 0:
            report.findings.append(Finding("stopped_deck", deck, f"beat at {beat.at_host_ns} ns", beat.beat_index))
            continue
        if not 0 <= beat.beat_index < len(grid_ms):
            continue
        heard_ns = sample.host_time_ns + (grid_ms[beat.beat_index] - sample.position_ms) / sample.rate * NS_PER_MS
        error_ms = abs(beat.at_host_ns - heard_ns) / NS_PER_MS
        report.beats_checked += 1
        report.max_error_ms = max(report.max_error_ms, error_ms)
        if error_ms > max_error_ms:
            report.findings.append(Finding("prediction_error", deck, f"{error_ms:.3f} ms", beat.beat_index))
    counts = Counter(b.beat_index for b in deck_beats)
    for index in sorted(_expected_indexes(grid_ms, ordered)):
        if counts[index] == 0:
            report.findings.append(Finding("missing_beat", deck, f"grid beat {index}", index))
    for index, n in sorted(counts.items()):
        if n > 1:
            report.findings.append(Finding("doubled_beat", deck, f"grid beat {index} sent {n} times", index))
    for emit in section_emits:
        if emit.deck != deck:
            continue
        report.findings.extend(_audit_section(grid, ordered, times, spans, emit))
    return report


def _audit_section(
    grid: Sequence[GridBeat],
    ordered: Sequence[DeckSample],
    times: list[int],
    spans: Sequence[SectionSpan],
    emit: SectionEmit,
) -> list[Finding]:
    sample = _sample_before(ordered, times, emit.at_host_ns)
    if sample is None:
        return [Finding("section_off_boundary", emit.deck, f"{emit.section} before any sample")]
    at_ms = _track_ms(sample, emit.at_host_ns)
    boundaries = [s.start_ms for s in spans[1:] if s.section == emit.section]
    if not boundaries:
        return [Finding("section_off_boundary", emit.deck, f"{emit.section} has no boundary")]
    nearest = min(boundaries, key=lambda b: abs(b - at_ms))
    bpm = _bpm_at(grid, nearest)
    beat_ms = 60_000.0 / bpm if bpm > 0 else 0.0
    off_ms = abs(at_ms - nearest)
    if off_ms > beat_ms:
        return [Finding("section_off_boundary", emit.deck, f"{emit.section} {off_ms:.1f} ms from {nearest} ms")]
    return []


def _bpm_at(grid: Sequence[GridBeat], track_ms: float) -> float:
    if not grid:
        return 0.0
    i = max(0, bisect_right([float(g["time_ms"]) for g in grid], track_ms) - 1)
    return float(grid[i]["bpm"])
