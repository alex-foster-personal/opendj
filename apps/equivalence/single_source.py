"""Verdicts for fields only ONE source has.

A source-unique field cannot have an agreement rate. There is nothing to be
apples-to-apples WITH, so cross-source agreement (SKILL 4b step 3) is omitted
and the burden shifts entirely onto the checks that do not need a second
source:

* step 1, range and cardinality probe: min, max, distinct, distribution,
  sentinels, and the declared-unit plausibility window.
* step 5, totality: the normaliser maps every observed value or raises. Fuzzed
  in ``tests/test_equivalence_normalisers.py``.
* for a TIME SERIES, the structural invariants a scalar does not have:
  monotonic non-overlapping spans, no non-positive durations, values inside the
  declared scale, and a bounded loss when converting to our millisecond
  storage base.

**This is weaker evidence than two-source agreement, and the verdict says so
out loud.** The contract status is ``passed`` so the existing gate in
``apps.shared.equivalence`` can unlock the write, but every entry carries
``basis`` (``single_source`` vs ``cross_source``) and a ``suite_status`` of
``passed_single_source``. Nobody reading the file in six months should be able
to mistake a one-sided check for cross-validation.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from apps.equivalence.config import CFG, SingleSourceField
from apps.equivalence.probe import Probe, declared_unit_mismatch, probe_field
from apps.equivalence.single_source_series import (
    MS_ROUNDING_BUDGET_MS,
    SeriesStructure,
    audit_series,
)
from apps.equivalence.sources import MikEnergySegment

# ------------------------------------------------- scalar vs its own series


@dataclass
class SeriesConsistency:
    """A source-unique SCALAR checked against the SERIES of the same quantity.

    Two different questions, kept apart on purpose:

    1. **Containment** (GATING). Is the scalar inside its own series' observed
       range on every track? A scalar that escapes that range is not the same
       quantity as the series, and the DJ.Studio case is the reason this is a
       violation rather than a curiosity: ``energyLevelNr`` is an INDEX into the
       segment array, reaches 17 on a 1-10 scale, and reads convincingly as an
       energy enum in any sampled peek (SKILL 4b). Containment is the only
       cross-check a single-source scalar can have.
    2. **Derivability** (INFORMATIONAL). Is the scalar a stored statistic of the
       series (max / mean / modal segment)? If it were, it would be redundant
       data. If it is not, it carries information the series does not, and
       dropping either loses something. Reported as an exact-match rate per
       hypothesis so the answer is a number, not an impression.
    """

    tracks_with_both: int
    scalar_without_series: int
    series_without_scalar: int
    scalar_above_series_max: int
    scalar_below_series_min: int
    hypothesis_exact_match: dict[str, int] = field(default_factory=dict)
    best_hypothesis: str = ""
    best_hypothesis_rate: float = 0.0
    findings: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)

    @property
    def sound(self) -> bool:
        return not self.violations

    def as_dict(self) -> dict[str, Any]:
        return {
            "tracks_with_both": self.tracks_with_both,
            "scalar_without_series": self.scalar_without_series,
            "series_without_scalar": self.series_without_scalar,
            "scalar_above_series_max": self.scalar_above_series_max,
            "scalar_below_series_min": self.scalar_below_series_min,
            "hypothesis_exact_match": self.hypothesis_exact_match,
            "best_hypothesis": self.best_hypothesis,
            "best_hypothesis_rate": self.best_hypothesis_rate,
            "derivable_from_series": self.best_hypothesis_rate >= 0.99,
            "findings": self.findings,
            "violations": self.violations,
            "sound": self.sound,
        }


def _hypotheses(values: list[float], lengths: list[float]) -> dict[str, float]:
    """Candidate statistics of one track's series, all on the series' own scale."""
    # Counter's stub fixes VALUE type to int, so a float-weighted frequency
    # table (segment length, not a count) needs a plain float-valued dict.
    weight: defaultdict[float, float] = defaultdict(float)
    for value, length in zip(values, lengths, strict=True):
        weight[value] += length
    total = sum(weight.values())
    ordered = sorted(weight.items())

    def quantile(share: float) -> float:
        seen = 0.0
        for value, held in ordered:
            seen += held
            if seen >= share * total:
                return value
        return ordered[-1][0]

    mean = sum(values) / len(values)
    weighted_mean = (
        sum(v * length for v, length in zip(values, lengths, strict=True)) / total
        if total
        else mean
    )
    return {
        "max": max(values),
        "min": min(values),
        "modal_segment_by_count": Counter(values).most_common(1)[0][0],
        "modal_segment_by_duration": max(weight.items(), key=lambda kv: kv[1])[0],
        "round_arithmetic_mean": float(round(mean)),
        "round_duration_weighted_mean": float(round(weighted_mean)),
        "duration_weighted_median": quantile(0.5),
        "duration_weighted_p75": quantile(0.75),
    }


def _scalar_series_hits(
    both: list[int],
    scalar_by_track: dict[int, float | None],
    per_track: dict[int, list[MikEnergySegment]],
) -> tuple[int, int, Counter[str]]:
    """The per-track containment/hypothesis pass, split out of
    ``audit_scalar_against_series`` to keep it under the complexity ceiling."""
    above = below = 0
    hits: Counter[str] = Counter()
    for pk in both:
        scalar = float(scalar_by_track[pk])  # type: ignore[arg-type]
        rows = per_track[pk]
        values = [s.energy for s in rows]
        lengths = [s.length_s for s in rows]
        if scalar > max(values):
            above += 1
        if scalar < min(values):
            below += 1
        for name, candidate in _hypotheses(values, lengths).items():
            if scalar == candidate:
                hits[name] += 1
    return above, below, hits


def _describe_scalar_series_audit(audit: SeriesConsistency, above: int, below: int) -> None:
    """The prose-findings half of ``audit_scalar_against_series``, split out to
    keep it under the complexity ceiling."""
    if above or below:
        audit.violations.append(
            f"the scalar escapes its own series' range on {above + below} tracks "
            f"({above} above the max, {below} below the min), so the two columns "
            f"are NOT the same quantity and one of them is mis-read"
        )
    else:
        audit.findings.append(
            f"CONTAINED: on all {audit.tracks_with_both} tracks holding both, the "
            f"scalar lies inside its own series' observed range. The scalar and "
            f"the series are therefore the same quantity on the same scale."
        )
    if audit.best_hypothesis_rate >= 0.99:
        audit.findings.append(
            f"REDUNDANT: the scalar equals {audit.best_hypothesis} of the series "
            f"on {audit.best_hypothesis_rate:.2%} of tracks, so it is derivable "
            f"and stores nothing new."
        )
    else:
        audit.findings.append(
            f"NOT DERIVABLE from the series: the best-fitting statistic is "
            f"{audit.best_hypothesis} at only {audit.best_hypothesis_rate:.2%} "
            f"exact match. The scalar is an independent number from the same "
            f"analysis, not a stored summary of the stored series, so neither "
            f"column can be reconstructed from the other."
        )
    if audit.scalar_without_series:
        audit.findings.append(
            f"{audit.scalar_without_series} tracks carry a scalar with NO series "
            f"at all, which is a second, independent reason the scalar is not "
            f"redundant: for those tracks it is the only energy we have."
        )


def audit_scalar_against_series(
    scalar_by_track: dict[int, float | None],
    segments: list[MikEnergySegment],
) -> SeriesConsistency:
    """Containment (gating) plus derivability (informational), both measured."""
    per_track: dict[int, list[MikEnergySegment]] = {}
    for segment in segments:
        per_track.setdefault(segment.song_pk, []).append(segment)

    both = sorted(
        pk
        for pk, value in scalar_by_track.items()
        if value is not None and pk in per_track
    )
    above, below, hits = _scalar_series_hits(both, scalar_by_track, per_track)

    audit = SeriesConsistency(
        tracks_with_both=len(both),
        scalar_without_series=sum(
            1
            for pk, value in scalar_by_track.items()
            if value is not None and pk not in per_track
        ),
        series_without_scalar=sum(
            1 for pk in per_track if scalar_by_track.get(pk) is None
        ),
        scalar_above_series_max=above,
        scalar_below_series_min=below,
        hypothesis_exact_match=dict(hits.most_common()),
    )
    if both:
        best, count = (hits.most_common(1) or [("none", 0)])[0]
        audit.best_hypothesis = best
        audit.best_hypothesis_rate = count / len(both)

    _describe_scalar_series_audit(audit, above, below)
    return audit


# ---------------------------------------------------------------- verdict

BASIS_SINGLE = "single_source"
BASIS_CROSS = "cross_source"
SUITE_PASSED_SINGLE = "passed_single_source"
SUITE_FAILED_STRUCTURE = "failed_structure"


@dataclass
class SingleSourceVerdict:
    """A source-unique field's verdict. Same file, deliberately weaker basis."""

    field_name: str
    status: str
    suite_status: str
    basis: str
    reasons: list[str]
    probe: Probe
    structure: SeriesStructure | None
    normaliser: str | None
    shape: str
    series_consistency: SeriesConsistency | None = None

    def entry(self, checked_at: str) -> dict[str, Any]:
        return {
            "status": self.status,
            "normaliser": self.normaliser,
            "checked_at": checked_at,
            "suite_status": self.suite_status,
            "basis": self.basis,
            "basis_meaning": (
                "single_source: ONE source has this field, so there is no "
                "cross-source agreement rate and none was computed. The "
                "evidence is the full-column range and cardinality probe, the "
                "totality of the normaliser, and (for a time series) the "
                "structural invariants. This is WEAKER evidence than "
                "cross_source agreement. Do not read it as cross-validated."
            ),
            "shape": self.shape,
            "reasons": self.reasons,
            "left": None,
            "right": self.probe.as_dict(),
            "agreement": None,
            "agreement_omitted_because": (
                "a field with a single source cannot have an agreement rate"
            ),
            "structure": None if self.structure is None else self.structure.as_dict(),
            "series_consistency": (
                None
                if self.series_consistency is None
                else self.series_consistency.as_dict()
            ),
            "disagreement_signatures": {},
            "systematic_offsets": [],
            "mapping_bugs": [],
            "disagreement_csv": None,
        }


def _single_source_early_verdict(
    spec: SingleSourceField,
    probe: Probe,
    structure: SeriesStructure | None,
    series_consistency: SeriesConsistency | None,
    verdict: SingleSourceVerdict,
    reasons: list[str],
) -> bool:
    """The hard-failure/untested gauntlet from ``decide_single_source``, split
    out to keep it under the complexity ceiling. Mutates ``verdict``/``reasons``
    in place and returns True if one of these early exits fired (the caller
    should return ``verdict`` as-is), False if the probe should be scored."""
    # A structurally broken series cannot be stored at all, whatever its value
    # cardinality, so this outranks every other check.
    if structure is not None and not structure.sound:
        verdict.status = "failed"
        verdict.suite_status = SUITE_FAILED_STRUCTURE
        reasons.extend(f"STRUCTURE VIOLATION: {v}" for v in structure.violations)
        return True

    # A scalar that escapes its own series' range is a mis-read column, which
    # is a mapping bug and not a "we have not proven it yet".
    if series_consistency is not None and not series_consistency.sound:
        verdict.status = "failed"
        verdict.suite_status = "failed_mapping_bug"
        reasons.extend(
            f"SCALAR-VS-SERIES VIOLATION: {v}" for v in series_consistency.violations
        )
        return True

    if not probe.comparable:
        reasons.append(
            f"UNTESTED: {spec.source.locator} is not usable - "
            + ("; ".join(probe.findings) or f"{probe.present} values")
        )
        return True

    mismatch = declared_unit_mismatch(probe)
    if mismatch is not None:
        verdict.status = "failed"
        verdict.suite_status = "failed_mapping_bug"
        reasons.append(f"MAPPING BUG: {mismatch.detail}")
        return True

    residual = probe.unmapped_count - probe.out_of_window_count
    if residual:
        verdict.status = "failed"
        verdict.suite_status = "failed_mapping_bug"
        reasons.append(
            f"MAPPING BUG: the normaliser is not total over the observed domain "
            f"({residual} unmapped values, e.g. {probe.unmapped[:5]})"
        )
        return True

    if probe.present < CFG.min_comparable:
        verdict.suite_status = "untested_too_few_pairs"
        reasons.append(
            f"UNTESTED: only {probe.present} values, below the "
            f"{CFG.min_comparable} needed for the probe to be evidence"
        )
        return True

    return False


def decide_single_source(
    spec: SingleSourceField,
    probe: Probe,
    structure: SeriesStructure | None = None,
    series_consistency: SeriesConsistency | None = None,
) -> SingleSourceVerdict:
    """Pass only if the probe is clean, the normaliser total, structure sound."""
    from apps.equivalence.normalisers import normaliser_name

    reasons: list[str] = []
    verdict = SingleSourceVerdict(
        field_name=spec.field_name,
        status="untested",
        suite_status="untested_no_data",
        basis=BASIS_SINGLE,
        reasons=reasons,
        probe=probe,
        structure=structure,
        normaliser=None,
        shape=spec.shape,
        series_consistency=series_consistency,
    )

    if _single_source_early_verdict(
        spec, probe, structure, series_consistency, verdict, reasons
    ):
        return verdict

    verdict.status = "passed"
    verdict.suite_status = SUITE_PASSED_SINGLE
    verdict.normaliser = normaliser_name(spec.source.kind, spec.source.unit)
    reasons.append(
        f"PASSED (single source, no cross-validation possible): full-column "
        f"probe over {probe.present} values found range "
        f"{probe.raw_min!r}..{probe.raw_max!r} inside the declared "
        f"{spec.source.unit!r} window, {probe.distinct} distinct values, "
        f"{probe.sentinel_count} documented sentinels, and the normaliser is "
        f"total (0 unmapped)."
    )
    if structure is not None:
        reasons.append(
            f"STRUCTURE SOUND: {structure.segments} spans over "
            f"{structure.tracks} tracks, monotonic and non-overlapping, no "
            f"non-positive durations, and the millisecond conversion neither "
            f"vanishes a span nor creates an overlap."
        )
        reasons.extend(f"NOTE: {f}" for f in structure.findings)
    if series_consistency is not None:
        reasons.append(
            f"SCALAR-VS-SERIES CONSISTENT: checked against the "
            f"{spec.companion_series_field} series on "
            f"{series_consistency.tracks_with_both} tracks holding both."
        )
        reasons.extend(f"NOTE: {f}" for f in series_consistency.findings)
    reasons.append(
        "BASIS single_source: this field has no counterpart in any other "
        "source, so no agreement rate exists and none was invented. Weaker "
        "evidence than a cross-source pass."
    )
    return verdict


def probe_single_source(rows: list[Any], spec: SingleSourceField) -> Probe:
    """Full-column probe of a source-unique scalar field."""
    return probe_field(rows, spec.source)


__all__ = [
    "BASIS_CROSS",
    "BASIS_SINGLE",
    "MS_ROUNDING_BUDGET_MS",
    "SUITE_FAILED_STRUCTURE",
    "SUITE_PASSED_SINGLE",
    "SeriesConsistency",
    "SeriesStructure",
    "SingleSourceVerdict",
    "audit_scalar_against_series",
    "audit_series",
    "decide_single_source",
    "probe_single_source",
]
