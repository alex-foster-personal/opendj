"""Score a PARITY-01 payload and render the round report."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from apps.parity import SCORER_VERSION
from apps.parity.bpm import score_bpm
from apps.parity.cues import refuse_forbidden_cue_banklist_keys, score_cues_anlz, score_cues_db
from apps.parity.figure import LaneFigure, ParityThresholdNotCalibrated
from apps.parity.key import score_key
from apps.parity.lanes import DELEGATED_THIS_ROUND, LANE_IDS, SCORED_THIS_ROUND
from apps.parity.phrase import score_phrase
from apps.parity.remaining import classify_remaining
from apps.parity.vocal import score_vocal
from apps.parity.waveform import score_waveform


@dataclass(frozen=True)
class ParityReport:
    """One scored round: every enumerated lane, one figure each."""

    scorer_version: str
    measured_at: str
    figures: tuple[LaneFigure, ...]
    round: int = 0

    def figure(self, lane: str) -> LaneFigure:
        for item in self.figures:
            if item.lane == lane:
                return item
        known = ", ".join(item.lane for item in self.figures)
        raise KeyError(f"no figure for lane {lane!r}; report has {known}")

    def numeric_digest(self) -> str:
        payload = {
            "scorer_version": self.scorer_version,
            "figures": [item.numeric_fields() for item in self.figures],
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def claim_parity(lane: str, *, threshold: float, maintainer_agreed: bool) -> None:
    """Refuse a self-chosen parity claim. the maintainer-gated agreement is not this PR."""
    if not maintainer_agreed:
        raise ParityThresholdNotCalibrated(
            f"lane {lane!r} cannot be called at parity against a self-chosen "
            f"threshold {threshold}: thresholds are agreed with the maintainer against "
            "his own calibration before any build describes a lane as at parity"
        )
    raise ParityThresholdNotCalibrated(
        f"lane {lane!r}: even with the maintainer-gated agreement recorded, this scorer "
        "does not declare parity; lock the threshold in specs/parity-01.md first"
    )


def _present_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    tracks = payload.get("tracks")
    if not isinstance(tracks, list):
        raise TypeError("payload.tracks must be a list of library-shaped rows")
    return [row for row in tracks if row.get("present")]


def score_payload(payload: dict[str, Any]) -> ParityReport:
    """Score every enumerated lane. Missing measured_at is a refusal, not now()."""
    refuse_forbidden_cue_banklist_keys(payload)
    measured_at = payload.get("measured_at")
    if not measured_at:
        raise ValueError(
            "payload has no measured_at; a figure without a date is not a figure"
        )
    rows = _present_rows(payload)
    round_label = int(payload.get("round", 0))
    figures: list[LaneFigure] = []
    for lane in LANE_IDS:
        if lane == "bpm":
            figures.append(score_bpm(rows, measured_at=measured_at))
        elif lane == "key":
            figures.append(score_key(rows, measured_at=measured_at))
        elif lane in {"waveform_preview", "waveform_detail", "waveform_triband"}:
            figures.append(score_waveform(lane, rows, measured_at=measured_at))
        elif lane == "phrase":
            figures.append(score_phrase(rows, measured_at=measured_at))
        elif lane == "cues_db":
            figures.append(score_cues_db(rows, measured_at=measured_at))
        elif lane == "cues_anlz":
            figures.append(score_cues_anlz(rows, measured_at=measured_at))
        elif lane == "vocal":
            figures.append(score_vocal(rows, measured_at=measured_at))
        else:
            figures.append(classify_remaining(lane, rows, measured_at=measured_at))
    return ParityReport(
        scorer_version=SCORER_VERSION,
        measured_at=measured_at,
        figures=tuple(figures),
        round=round_label,
    )


def _waveform_notes(figure: LaneFigure) -> str:
    parts = []
    if figure.median_r is not None:
        parts.append(f"median r {figure.median_r:.6f} (n={figure.scored_n})")
    if figure.min_r is not None:
        parts.append(f"min {figure.min_r:.3f}")
    if figure.band_median_r:
        band_bits = [
            f"{figure.band_median_r[name]:.3f}"
            for name in ("low", "mid", "high")
            if name in figure.band_median_r
        ]
        bands = "/".join(band_bits)
        parts.append(f"bands low/mid/high {bands}")
    if figure.median_r_secondary is not None:
        parts.append(f"secondary median r {figure.median_r_secondary:.6f}")
    parts.append("Reporting r, not a threshold.")
    return "; ".join(parts)


def _phrase_notes(figure: LaneFigure) -> str:
    if figure.scored_n > 0:
        return (
            f"boundary F@0.5s {figure.boundary_f_0_5:.3f} of {figure.scored_n} scored; "
            f"F@3.0s {figure.boundary_f_3_0:.3f}; kind acc {figure.label_acc:.3f}. "
            "Reporting bands, not a threshold. Denominator is PSSI-present tracks "
            "in this fixture, never the beatgrid pool."
        )
    if figure.denominator_n == 0:
        missing = figure.ungradable.get("missing_pssi", 0)
        return (
            f"no PSSI in this fixture; missing_pssi {missing}. "
            "Phrase denominator is PSSI-present tracks, never the beatgrid pool."
        )
    return (
        f"no own phrase analysis on {figure.no_own_n} PSSI-present tracks. "
        "Native phrase producer is v2; reporting the wait, not a miss."
    )


def _figure_notes(figure: LaneFigure) -> str:
    notes = figure.reason or figure.owner or ""
    lane = figure.lane
    if lane == "bpm" and figure.within_0_1_n is not None:
        notes = (
            f"within 0.1 BPM {figure.within_0_1_n}/{figure.scored_n}; "
            f"within 1.0 BPM {figure.within_1_0_n}/{figure.scored_n}; "
            f"octave {len(figure.octave_ids)}. Reporting bands, not a threshold."
        )
    elif lane == "key" and figure.mirex_mean is not None:
        related = figure.related_n if figure.related_n is not None else 0
        notes = (
            f"MIREX mean {figure.mirex_mean:.3f} of {figure.scored_n} scored; "
            f"related {related}; failed_own {figure.failed_own_n}. "
            "Reuses analysis_bench key weighted_score."
        )
    elif lane in {"waveform_preview", "waveform_detail", "waveform_triband"}:
        notes = _waveform_notes(figure)
    elif lane == "phrase" and figure.status == "scored":
        notes = _phrase_notes(figure)
    elif lane == "cues_db" and figure.status == "scored":
        matched = figure.details.get("matched_cues_n", 0)
        rb_cues = figure.details.get("rb_cues_n", 0)
        own_only = figure.details.get("own_only_cues_n", 0)
        conflicts = figure.details.get("conflicts_n", 0)
        notes = (
            f"20 ms grain, not a threshold. "
            f"matched {matched}/{rb_cues} rb cues; "
            f"own-only {own_only}; conflicts {conflicts}. "
            f"exact {figure.exact_n}/{figure.scored_n} scored; "
            f"no_own {figure.no_own_n}."
        )
    elif lane == "cues_anlz" and figure.status == "scored":
        pcob_n = figure.details.get("pcob_n", 0)
        pco2_n = figure.details.get("pco2_n", 0)
        unreadable = figure.ungradable.get("unreadable_ext", 0)
        ext_note = f" ({unreadable} unreadable_ext)" if unreadable else ""
        notes = (
            f"PCOB {pcob_n}; PCO2 {pco2_n}{ext_note}. "
            f"20 ms grain, not a threshold. "
            f"exact {figure.exact_n}/{figure.scored_n} scored."
        )
    elif lane == "vocal" and figure.iou_mean is not None:
        notes = (
            f"mean IoU {figure.iou_mean:.3f} of {figure.scored_n} scored; "
            f"exact (IoU=1) {figure.exact_n}; reporting, not a threshold."
        )
    elif lane == "vocal" and figure.status == "scored":
        notes = (
            f"mean IoU - of {figure.scored_n} scored; "
            f"exact (IoU=1) {figure.exact_n or 0}; reporting, not a threshold."
        )
    return notes


def render_report(report: ParityReport, *, round_n: int | None = None) -> str:
    """Markdown a later session can resume from. Never says 'at parity'."""
    label = round_n if round_n is not None else report.round
    lines = [
        f"# PARITY-01 round {label}",
        "",
        f"Measured {report.measured_at}. Scorer {report.scorer_version}.",
        "No lane is described as matching a calibrated threshold; "
        "thresholds are the maintainer-gated and unset.",
        "",
        "| lane | status | denominator_n | denominator | scored | exact | "
        "ungradable | no_own | notes |",
        "|---|---|---:|---|---:|---:|---:|---:|---|",
    ]
    for figure in report.figures:
        ungradable_n = sum(figure.ungradable.values())
        exact = "-" if figure.exact_n is None else str(figure.exact_n)
        notes = _figure_notes(figure)
        lines.append(
            f"| {figure.lane} | {figure.status} | {figure.denominator_n} | "
            f"{figure.denominator_name} | {figure.scored_n} | {exact} | "
            f"{ungradable_n} | {figure.no_own_n} | {notes} |"
        )
    lines.extend(
        [
            "",
            f"numeric_digest `{report.numeric_digest()}`",
            "",
            "Scored this round: " + ", ".join(SCORED_THIS_ROUND) + ".",
            "Delegated: " + ", ".join(DELEGATED_THIS_ROUND) + ".",
            "Remaining lanes are not_scored_this_round, never 0-of-N agreement.",
            "",
        ]
    )
    return "\n".join(lines)
