"""Step 1: range and cardinality probe, per source per field, before any join.

"A 0-4 range facing a 1-10 range is a mapping bug you can see without any
cross-source join at all. Do this first, it is nearly free." (SKILL 4b)

Two mechanical checks, no judgement:

1. DECLARED-UNIT check. Convert the observed min/max through the declared
   unit into the canonical unit and assert it lands inside the plausible
   window for that kind of field. rekordbox BPM declared as ``bpm`` would put
   18000 BPM on the table, which fails here, at zero cost, before any join.
2. CROSS-SOURCE RATIO check. Compare the two sides' canonical spans. If they
   differ by a suspicious constant factor that the declared units do NOT
   already explain, that is a scale mismatch.

The probe reads the WHOLE column. Never a ``head`` sample -- a sampled peek
is what made DJ.Studio's segment INDEX read convincingly as a 0-4 energy enum.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from apps.equivalence.config import CFG, KIND_CANONICAL_RANGE, SourceField
from apps.equivalence.normalisers import (
    ABSENT_UNITS,
    MISSING,
    UNDECIDABLE_UNITS,
    NormaliseError,
    is_comparable_unit,
    normalise,
)

# Factors worth naming when two spans differ. Each is a unit or convention
# error with a known cause.
SUSPICIOUS_FACTORS: tuple[tuple[float, str], ...] = (
    (2.0, "half/double (2x) - bpm halving or a beat/bar confusion"),
    (0.5, "half/double (0.5x) - bpm doubling or a beat/bar confusion"),
    (100.0, "100x - a centi-unit column read as the base unit"),
    (0.01, "0.01x - a base unit read as a centi-unit column"),
    (1000.0, "1000x - seconds read as milliseconds"),
    (0.001, "0.001x - milliseconds read as seconds"),
    (51.0, "51x - a 0-5 star scale facing the 0/51/../255 POPM byte"),
)


@dataclass
class Probe:
    """What one column actually contains. Everything here is measured."""

    source: str
    locator: str
    kind: str
    declared_unit: str
    rows: int
    present: int
    null: int
    distinct: int
    numeric: bool
    raw_min: Any = None
    raw_max: Any = None
    canonical_min: float | None = None
    canonical_max: float | None = None
    canonical_distinct: int = 0
    """Distinct canonical values. The cardinality half of the probe: for a
    categorical field this is the number that matters, since 46 raw spellings
    collapsing to 24 canonical keys IS the finding."""
    mapped_raw_distinct: int = 0
    """Distinct raw spellings that produced a canonical value, i.e. ``distinct``
    minus the spellings that resolved to a documented sentinel or had no rule.

    The collapse finding compares against THIS, not against ``distinct``.
    Measured Tue 28 Jul 2026: comparing against ``distinct`` made every numeric
    column with a single sentinel ('0.0' not analysed) report "more than one
    notation", so rekordbox BPM (1266 -> 1265), MIK ZTEMPO (4202 -> 4201),
    rekordbox Length (561 -> 560) and MIK ZKEY (25 -> 24) all fired falsely.
    Worse, MIK ZKEY is the one column we are certain holds exactly ONE notation,
    so the finding was contradicting the config's own measured note. A finding
    that fires on every column is noise, and noise is what hides the real one."""
    histogram: dict[str, int] = field(default_factory=dict)
    unmapped: list[Any] = field(default_factory=list)
    unmapped_count: int = 0
    out_of_window_count: int = 0
    sentinel_count: int = 0
    declared_unit_ok: bool = True
    findings: list[str] = field(default_factory=list)

    @property
    def zero_variance(self) -> bool:
        return self.present > 0 and self.distinct == 1

    @property
    def comparable(self) -> bool:
        """False when the column cannot enter a comparison at all."""
        return (
            self.declared_unit not in ABSENT_UNITS
            and self.declared_unit not in UNDECIDABLE_UNITS
            and self.present > 0
            and not self.zero_variance
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "locator": self.locator,
            "kind": self.kind,
            "declared_unit": self.declared_unit,
            "rows": self.rows,
            "present": self.present,
            "null": self.null,
            "distinct": self.distinct,
            "raw_min": self.raw_min,
            "raw_max": self.raw_max,
            "canonical_min": self.canonical_min,
            "canonical_max": self.canonical_max,
            "canonical_distinct": self.canonical_distinct,
            "mapped_raw_distinct": self.mapped_raw_distinct,
            "histogram": self.histogram,
            "sentinel_count": self.sentinel_count,
            "unmapped_count": self.unmapped_count,
            "out_of_window_count": self.out_of_window_count,
            "unmapped_examples": self.unmapped[:10],
            "declared_unit_ok": self.declared_unit_ok,
            "findings": self.findings,
        }


@dataclass
class ScaleMismatch:
    """A mechanically detected scale problem. Reported as a MAPPING BUG."""

    kind: str  # 'declared_unit_implausible' | 'cross_source_factor'
    detail: str
    factor: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "detail": self.detail, "factor": self.factor}


# ------------------------------------------------------------- histogram


def _numeric_histogram(values: Sequence[float], bins: int) -> dict[str, int]:
    low, high = min(values), max(values)
    if low == high:
        return {f"{low:g}": len(values)}
    width = (high - low) / bins
    counts: Counter[int] = Counter(
        min(int((v - low) / width), bins - 1) for v in values
    )
    return {
        f"{low + idx * width:.4g}..{low + (idx + 1) * width:.4g}": counts[idx]
        for idx in sorted(counts)
    }


def _categorical_histogram(values: Sequence[Any], max_buckets: int) -> dict[str, int]:
    counts = Counter(str(v) for v in values)
    if len(counts) <= max_buckets:
        return dict(counts.most_common())
        # ordered by frequency so the tail is visible at a glance
    top = dict(counts.most_common(max_buckets))
    top[f"<{len(counts) - max_buckets} rarer values>"] = sum(
        n for _, n in counts.most_common()[max_buckets:]
    )
    return top


# ----------------------------------------------------------------- probe


def _populate_raw_extremes(probe: Probe, present_raw: list[Any]) -> None:
    """Raw extremes and histogram, on the raw values, in the source's own
    units."""
    if probe.numeric:
        numeric_raw = [float(v) for v in present_raw if isinstance(v, (int, float))]
        if len(numeric_raw) != len(present_raw):
            probe.findings.append(
                f"{len(present_raw) - len(numeric_raw)} non-numeric values in a "
                f"numeric column"
            )
        if numeric_raw:
            probe.raw_min, probe.raw_max = min(numeric_raw), max(numeric_raw)
            probe.histogram = _numeric_histogram(
                numeric_raw, CFG.numeric_histogram_bins
            )
    else:
        as_text = sorted(str(v) for v in present_raw)
        probe.raw_min, probe.raw_max = as_text[0], as_text[-1]
        probe.histogram = _categorical_histogram(present_raw, CFG.histogram_max_buckets)


@dataclass
class _CanonicalAccumulation:
    canonical: list[float]
    canonical_seen: set[str]
    unmapped_seen: set[str]
    sentinel_spellings: set[str]
    mapped_spellings: set[str]


def _accumulate_canonical(
    probe: Probe, spec: SourceField, present_raw: list[Any]
) -> _CanonicalAccumulation:
    acc = _CanonicalAccumulation(
        canonical=[],
        canonical_seen=set(),
        unmapped_seen=set(),
        sentinel_spellings=set(),
        mapped_spellings=set(),
    )
    for value in present_raw:
        try:
            result = normalise(value, spec.kind, spec.unit)
        except NormaliseError as exc:
            probe.unmapped_count += 1
            if "outside the legal" in exc.reason:
                probe.out_of_window_count += 1
            if str(value) not in acc.unmapped_seen:
                acc.unmapped_seen.add(str(value))
                probe.unmapped.append(value)
            continue
        if result is MISSING:
            probe.sentinel_count += 1
            acc.sentinel_spellings.add(str(value))
            continue
        acc.mapped_spellings.add(str(value))
        acc.canonical_seen.add(str(result))
        if isinstance(result, float):
            acc.canonical.append(result)
    return acc


def _report_canonical_findings(
    probe: Probe, spec: SourceField, acc: _CanonicalAccumulation
) -> None:
    probe.canonical_distinct = len(acc.canonical_seen)
    probe.mapped_raw_distinct = len(acc.mapped_spellings)
    if acc.canonical:
        probe.canonical_min, probe.canonical_max = min(acc.canonical), max(acc.canonical)
    collapsed = (
        probe.canonical_distinct
        and probe.canonical_distinct < probe.mapped_raw_distinct
    )
    if collapsed:
        # Sentinel and unmapped spellings are EXCLUDED from the left-hand
        # side: a spelling that resolved to MISSING is not a second spelling
        # of a canonical value, it is the absence of one.
        sentinel_note = (
            f" ({len(acc.sentinel_spellings)} further raw spelling(s) resolved "
            f"to a documented sentinel and are not counted here)"
            if acc.sentinel_spellings
            else ""
        )
        probe.findings.append(
            f"{probe.mapped_raw_distinct} raw spellings collapse to "
            f"{probe.canonical_distinct} canonical values, so the raw column "
            f"holds more than one notation or spelling of the same value"
            f"{sentinel_note}"
        )
    if probe.out_of_window_count:
        # The values are readable, they just are not in the unit claimed.
        # Report it as the unit error it is, not as an unknown vocabulary.
        probe.declared_unit_ok = False
        probe.findings.append(
            f"DECLARED UNIT IMPLAUSIBLE: {probe.out_of_window_count} of "
            f"{probe.present} values fall outside the legal input window "
            f"for unit {spec.unit!r} (e.g. {probe.unmapped[:5]}). The column "
            f"is not in the unit it is declared to be in"
        )
    residual_unmapped = probe.unmapped_count - probe.out_of_window_count
    if residual_unmapped:
        probe.findings.append(
            f"NORMALISER NOT TOTAL over the observed domain: "
            f"{residual_unmapped} values in {len(acc.unmapped_seen)} distinct "
            f"forms have no rule (e.g. {probe.unmapped[:5]})"
        )


def _canonicalize_values(probe: Probe, spec: SourceField, present_raw: list[Any]) -> None:
    """Canonicalise every present value; count sentinels and unmapped values,
    and report a collapsed-notation or declared-unit finding where found."""
    if not is_comparable_unit(spec.kind, spec.unit):
        return
    acc = _accumulate_canonical(probe, spec, present_raw)
    _report_canonical_findings(probe, spec, acc)


def _check_canonical_range(probe: Probe, spec: SourceField) -> None:
    window = KIND_CANONICAL_RANGE.get(spec.kind)
    if window is None or probe.canonical_min is None:
        return
    low, high = window
    assert probe.canonical_max is not None
    if probe.canonical_min < low or probe.canonical_max > high:
        probe.declared_unit_ok = False
        probe.findings.append(
            f"DECLARED UNIT IMPLAUSIBLE: {spec.locator} declared "
            f"{spec.unit!r} gives canonical range "
            f"{probe.canonical_min:g}..{probe.canonical_max:g}, outside the "
            f"plausible {low:g}..{high:g} for a {spec.kind} field"
        )


def probe_field(rows: Sequence[Any], spec: SourceField) -> Probe:
    """Full-column probe of ``spec`` over ``rows``. Reads every row."""
    raw_values = [getattr(row, spec.attr, None) for row in rows]
    present_raw = [v for v in raw_values if v is not None and v != ""]
    probe = Probe(
        source=spec.source,
        locator=spec.locator,
        kind=spec.kind,
        declared_unit=spec.unit,
        rows=len(raw_values),
        present=len(present_raw),
        null=len(raw_values) - len(present_raw),
        distinct=len({str(v) for v in present_raw}),
        numeric=spec.kind != "key",
    )

    if spec.unit in ABSENT_UNITS:
        probe.findings.append(
            f"{spec.source} has no such column; this pair cannot be compared"
        )
        return probe
    if spec.unit in UNDECIDABLE_UNITS:
        probe.findings.append(
            f"declared unit {spec.unit!r} is not decidable from the data; "
            f"verdict must be UNTESTED, never a guessed unit"
        )
    if not present_raw:
        probe.findings.append("column is empty; there is no data to compare")
        return probe
    if probe.distinct == 1:
        # A single distinct value carries no information. MIK's ZRATING is 0 on
        # all 7026 rows, so it looks populated and is not.
        probe.findings.append(
            f"ZERO VARIANCE: all {probe.present} values are "
            f"{present_raw[0]!r}. The column is unused, so there is nothing to "
            f"compare even though it is not NULL"
        )

    _populate_raw_extremes(probe, present_raw)
    _canonicalize_values(probe, spec, present_raw)
    _check_canonical_range(probe, spec)
    return probe


# --------------------------------------------------- cross-source ratio


def _span(probe: Probe) -> float | None:
    if probe.canonical_max is None or probe.canonical_min is None:
        return None
    if probe.canonical_max == 0:
        return None
    return probe.canonical_max


def cross_source_scale_mismatch(left: Probe, right: Probe) -> ScaleMismatch | None:
    """A suspicious constant factor between two canonical spans, or None.

    Runs AFTER unit conversion, so a hit here means the declared units do not
    reconcile the two columns and one of them is wrong.
    """
    left_span, right_span = _span(left), _span(right)
    if left_span is None or right_span is None:
        return None
    ratio = left_span / right_span
    for factor, why in SUSPICIOUS_FACTORS:
        if abs(ratio - factor) <= CFG.ratio_tolerance * factor:
            return ScaleMismatch(
                kind="cross_source_factor",
                detail=(
                    f"canonical maxima differ by {ratio:.4g}x "
                    f"({left.locator} {left_span:g} vs {right.locator} "
                    f"{right_span:g}): {why}"
                ),
                factor=ratio,
            )
    return None


def declared_unit_mismatch(probe: Probe) -> ScaleMismatch | None:
    """The declared-unit failure from :func:`probe_field`, as a mismatch record."""
    if probe.declared_unit_ok:
        return None
    return ScaleMismatch(
        kind="declared_unit_implausible",
        detail=next(
            (f for f in probe.findings if f.startswith("DECLARED UNIT")),
            f"{probe.locator} declared {probe.declared_unit!r} is implausible",
        ),
    )


__all__ = [
    "SUSPICIOUS_FACTORS",
    "Probe",
    "ScaleMismatch",
    "cross_source_scale_mismatch",
    "declared_unit_mismatch",
    "probe_field",
]
