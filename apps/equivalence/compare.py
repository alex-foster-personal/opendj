"""Steps 3 and 4: post-normalisation agreement, and offset clustering.

Two rules from SKILL 4b drive the shape of this module:

* "Never report only a percentage." Every disagreement is written to CSV with
  both raw values, both canonical values, the delta, and the match tier, so a
  re-measured disagreement is distinguishable from a regression.
* "If disagreements cluster at a constant delta that is a unit or convention
  error, NOT source disagreement." A cluster is therefore reported as a
  MAPPING BUG and it blocks the verdict. It is never averaged over, and no
  precedence policy is allowed to pick a winner between two readings of one
  truth.
"""

from __future__ import annotations

import csv
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from apps.equivalence.config import CFG, FieldPair
from apps.equivalence.normalisers import (
    MISSING,
    Key,
    NormaliseError,
    is_comparable_unit,
    normalise,
)
from apps.equivalence.probe import SUSPICIOUS_FACTORS
from apps.equivalence.sources import Pairing


@dataclass
class RowComparison:
    """One pair's two values, canonicalised, plus why it agreed or did not."""

    tier: str
    left_id: str
    right_id: str
    title: str
    left_raw: Any
    right_raw: Any
    left_canonical: Any
    right_canonical: Any
    agree: bool
    delta: float | None
    signature: str
    key_confidence: float | None = None
    form: str = "unknown"
    """The raw NOTATION family of this row, e.g. ``rb=camelot|mik=camelot``. A
    cluster confined to one form is a mis-read notation, i.e. our bug."""


@dataclass
class Agreement:
    """Agreement over the rows where BOTH sides had a usable value."""

    pairs: int
    comparable: int
    agree: int
    disagree: int
    left_missing: int
    right_missing: int
    left_unmapped: int
    right_unmapped: int
    tolerance: float
    tolerance_note: str
    form_counts: dict[str, int] = field(default_factory=dict)
    """Comparable rows per raw-notation family. The denominator for the
    subset-coverage test that separates a mis-read notation from disagreement."""

    @property
    def rate(self) -> float | None:
        if self.comparable == 0:
            return None
        return self.agree / self.comparable

    def as_dict(self) -> dict[str, Any]:
        return {
            "pairs": self.pairs,
            "comparable": self.comparable,
            "agree": self.agree,
            "disagree": self.disagree,
            "rate": self.rate,
            "left_missing": self.left_missing,
            "right_missing": self.right_missing,
            "left_unmapped": self.left_unmapped,
            "right_unmapped": self.right_unmapped,
            "tolerance": self.tolerance,
            "tolerance_note": self.tolerance_note,
            "form_counts": self.form_counts,
            "denominator_note": (
                "rate denominator is 'comparable': pairs where BOTH sides held "
                "a usable, normalisable value. It is not the library size and "
                "not the match count."
            ),
        }


MAPPING_BUG = "mapping_bug"
SUSPECT = "suspect"

_CLASSIFICATION_MEANING = {
    MAPPING_BUG: (
        "MAPPING BUG. A unit or convention error, NOT source disagreement. "
        "Fix the mapping; never average over it and never let a precedence "
        "policy pick a winner between two readings of the same truth."
    ),
    SUSPECT: (
        "SUSPECT, unresolved. The delta repeats often enough to be a pattern "
        "but covers too little of the population to be a unit or convention "
        "error, and it is confined to no single notation. It is therefore "
        "either genuine source disagreement or a narrow mapping error, and "
        "the numbers alone cannot say which (SKILL 4b: a low or patterned "
        "disagreement rate is AMBIGUOUS). Adjudicate these rows by ear or in "
        "the source app's own UI before trusting the field."
    ),
}


@dataclass
class OffsetCluster:
    """A repeated delta signature, classified against BOTH denominators."""

    signature: str
    count: int
    share: float  # of the disagreements
    population_share: float  # of the comparable rows
    classification: str  # MAPPING_BUG | SUSPECT
    interpretation: str
    subset_form: str | None = None
    subset_coverage: float | None = None
    examples: list[str] = field(default_factory=list)

    @property
    def is_mapping_bug(self) -> bool:
        return self.classification == MAPPING_BUG

    def as_dict(self) -> dict[str, Any]:
        return {
            "signature": self.signature,
            "count": self.count,
            "share_of_disagreements": self.share,
            "share_of_comparable": self.population_share,
            "classification": self.classification,
            "classification_meaning": _CLASSIFICATION_MEANING[self.classification],
            "confined_to_notation": self.subset_form,
            "notation_coverage": self.subset_coverage,
            "interpretation": self.interpretation,
            "examples": self.examples[:5],
        }


# ------------------------------------------------------------ signatures


def _key_signature(left: Key, right: Key) -> str:
    """Name the relation between two keys, so identical errors group together."""
    if left == right:
        return "equal"
    if left == right.relative:
        return "relative_major_minor"
    semitones = (left.pitch_class - right.pitch_class) % 12
    if left.mode == right.mode:
        if semitones in (1, 11):
            return f"semitone_{semitones - 12 if semitones == 11 else semitones:+d}"
        if semitones == 7:
            return "camelot_hop_+1_fifth_up"
        if semitones == 5:
            return "camelot_hop_-1_fifth_down"
        return f"same_mode_semitones_{semitones}"
    if semitones == 0:
        return "parallel_major_minor"
    return f"mode_flip_semitones_{semitones}"


_KEY_INTERPRETATION: dict[str, str] = {
    "relative_major_minor": (
        "RELATIVE MAJOR/MINOR: one side reports the relative of the other "
        "(Am vs C). SKILL 4b names this as the single easiest way to look 95% "
        "right and be systematically wrong, so it is checked for explicitly"
    ),
    "parallel_major_minor": (
        "PARALLEL MAJOR/MINOR: same tonic, opposite mode (Db vs Dbm). Either a "
        "mode bit dropped in the mapping, or the classic third-detection "
        "ambiguity between two analysers"
    ),
    "semitone_+1": "constant +1 semitone: a pitch-class off-by-one in a mapping table",
    "semitone_-1": "constant -1 semitone: a pitch-class off-by-one in a mapping table",
    "camelot_hop_+1_fifth_up": (
        "one step clockwise on the Camelot wheel (a perfect fifth up). As a "
        "mapping error this is a wrong wheel offset, e.g. Camelot read as "
        "open-key, which would move EVERY row; over a small subset it is more "
        "likely the dominant-vs-tonic ambiguity between two key detectors"
    ),
    "camelot_hop_-1_fifth_down": (
        "one step anticlockwise on the Camelot wheel (a perfect fifth down): as "
        "above, mirrored"
    ),
}

_HALF_OR_DOUBLE = "half_or_double"


def _numeric_signature(left: float, right: float) -> str:
    if right != 0:
        ratio = left / right
        for factor, _why in SUSPICIOUS_FACTORS:
            if abs(ratio - factor) <= CFG.ratio_tolerance * factor:
                # 2x and 0.5x are ONE phenomenon seen from either side; keeping
                # them apart halves the apparent size of the real cluster.
                if factor in (2.0, 0.5):
                    return _HALF_OR_DOUBLE
                return f"ratio_{factor:g}x"
    bucket = round((left - right) / CFG.additive_bucket) * CFG.additive_bucket
    return f"additive_{bucket:+g}"


def _numeric_interpretation(signature: str) -> str:
    if signature == _HALF_OR_DOUBLE:
        return (
            "exactly 2x or 0.5x: half/double. As a mapping error it means one "
            "side stores a different tempo octave by convention; as source "
            "disagreement it means one analyser locked onto the wrong octave "
            "on those tracks"
        )
    if signature.startswith("ratio_"):
        factor = signature[len("ratio_") : -1]
        for candidate, why in SUSPICIOUS_FACTORS:
            if f"{candidate:g}" == factor:
                return f"exact {factor}x on a cluster of rows: {why}"
        return f"exact {factor}x on a cluster of rows: a unit error"
    if signature.startswith("additive_"):
        return (
            f"constant additive offset of {signature[len('additive_') :]} in the "
            f"canonical unit across a cluster of rows: an origin or convention "
            f"error (e.g. a base-0 vs base-1 index, or a fixed calibration)"
        )
    return "repeated delta signature"


# --------------------------------------------------------- notation forms

_CAMELOT_FORM = re.compile(r"^\s*(1[0-2]|[1-9])\s*[AaBb]\s*$")
_MUSICAL_FORM = re.compile(r"^\s*[A-Ga-g][#b♭♯]?\s*(m|min|minor|maj|major|M)?\s*$")


def _raw_form(value: Any, kind: str) -> str:
    """Label the raw NOTATION of one value, so a per-notation bug is visible."""
    if kind != "key":
        return "numeric"
    text = str(value).strip()
    if _CAMELOT_FORM.match(text):
        return "camelot"
    if _MUSICAL_FORM.match(text):
        return "musical"
    return "other"


def _pair_form(left_raw: Any, right_raw: Any, kind: str) -> str:
    return f"rb={_raw_form(left_raw, kind)}|mik={_raw_form(right_raw, kind)}"


# ------------------------------------------------------------- comparison


def compare_pair(
    pairings: list[Pairing], spec: FieldPair
) -> tuple[Agreement, list[RowComparison]]:
    """Normalise both sides of every pairing and split agree / disagree."""
    left_ok = is_comparable_unit(spec.left.kind, spec.left.unit)
    right_ok = is_comparable_unit(spec.right.kind, spec.right.unit)
    comparisons: list[RowComparison] = []
    counts: Counter[str] = Counter()
    form_counts: Counter[str] = Counter()

    for pairing in pairings:
        raw_left = getattr(pairing.left, spec.left.attr, None)
        raw_right = getattr(pairing.right, spec.right.attr, None)
        canon_left = _canonical(raw_left, spec.left, left_ok, counts, "left")
        canon_right = _canonical(raw_right, spec.right, right_ok, counts, "right")
        if canon_left is None or canon_right is None:
            continue  # missing, unmapped, or the unit blocks comparison
        agree, delta, signature = _judge(canon_left, canon_right, spec)
        form = _pair_form(raw_left, raw_right, spec.left.kind)
        counts["comparable"] += 1
        form_counts[form] += 1
        counts["agree" if agree else "disagree"] += 1
        if not agree:
            comparisons.append(
                RowComparison(
                    tier=pairing.tier,
                    left_id=pairing.left.content_id,
                    right_id=str(pairing.right.pk),
                    title=pairing.right.title or pairing.left.title or "",
                    left_raw=raw_left,
                    right_raw=raw_right,
                    left_canonical=_render(canon_left),
                    right_canonical=_render(canon_right),
                    agree=False,
                    delta=delta,
                    signature=signature,
                    key_confidence=pairing.right.key_confidence,
                    form=form,
                )
            )

    agreement = Agreement(
        pairs=len(pairings),
        comparable=counts["comparable"],
        agree=counts["agree"],
        disagree=counts["disagree"],
        left_missing=counts["left_missing"],
        right_missing=counts["right_missing"],
        left_unmapped=counts["left_unmapped"],
        right_unmapped=counts["right_unmapped"],
        tolerance=spec.tolerance,
        tolerance_note=spec.tolerance_note,
        form_counts=dict(form_counts.most_common()),
    )
    return agreement, comparisons


def _canonical(raw: Any, spec, unit_ok: bool, counts: Counter, side: str) -> Any:
    if not unit_ok:
        counts[f"{side}_missing"] += 1
        return None
    try:
        value = normalise(raw, spec.kind, spec.unit)
    except NormaliseError:
        counts[f"{side}_unmapped"] += 1
        return None
    if value is MISSING:
        counts[f"{side}_missing"] += 1
        return None
    return value


def _judge(left: Any, right: Any, spec: FieldPair) -> tuple[bool, float | None, str]:
    if isinstance(left, Key) and isinstance(right, Key):
        signature = _key_signature(left, right)
        return signature == "equal", None, signature
    delta = float(left) - float(right)
    if abs(delta) <= spec.tolerance:
        return True, delta, "equal"
    return False, delta, _numeric_signature(float(left), float(right))


def _render(value: Any) -> str:
    if isinstance(value, Key):
        return f"{value.camelot} (pc={value.pitch_class},{value.mode})"
    return f"{float(value):g}"


# --------------------------------------------------------- offset clusters


def _offset_cluster_for_signature(
    signature: str,
    rows: list[RowComparison],
    *,
    kind: str,
    total: int,
    comparable: int,
    forms: dict[str, int],
) -> OffsetCluster | None:
    """One signature group's classification, split out of
    ``detect_offset_clusters`` to keep it under the complexity ceiling."""
    share = len(rows) / total
    population_share = len(rows) / comparable
    if len(rows) < CFG.cluster_min_count or share < CFG.cluster_min_share:
        return None
    if population_share < CFG.suspect_population_share:
        return None
    subset_form, subset_coverage = _dominant_form(rows, forms)
    systematic = population_share >= CFG.systematic_population_share or (
        subset_coverage is not None and subset_coverage >= CFG.subset_coverage_share
    )
    if kind == "key":
        interpretation = _KEY_INTERPRETATION.get(
            signature,
            f"repeated key relation {signature!r} across a cluster of rows",
        )
    else:
        interpretation = _numeric_interpretation(signature)
    return OffsetCluster(
        signature=signature,
        count=len(rows),
        share=share,
        population_share=population_share,
        classification=MAPPING_BUG if systematic else SUSPECT,
        interpretation=interpretation,
        subset_form=subset_form if systematic else None,
        subset_coverage=subset_coverage,
        examples=[
            f"{r.title!r}: {r.left_raw!r} vs {r.right_raw!r} "
            f"-> {r.left_canonical} vs {r.right_canonical}"
            for r in rows[:5]
        ],
    )


def detect_offset_clusters(
    comparisons: list[RowComparison],
    *,
    kind: str,
    comparable: int,
    form_counts: dict[str, int] | None = None,
) -> list[OffsetCluster]:
    """Named delta clusters, each classified as a mapping bug or a suspect.

    A cluster is a proven MAPPING BUG when it is near-universal (>= 25% of the
    comparable population) or when it swallows >= 90% of one raw notation. Both
    are what a unit or convention error looks like: deterministic given the
    input, not scattered. Everything else that still repeats is a SUSPECT and
    the verdict becomes inconclusive rather than passing or failing.
    """
    if not comparisons or comparable <= 0:
        return []
    forms = form_counts or {}
    total = len(comparisons)
    grouped: dict[str, list[RowComparison]] = {}
    for comparison in comparisons:
        grouped.setdefault(comparison.signature, []).append(comparison)

    clusters = [
        cluster
        for signature, rows in grouped.items()
        if (
            cluster := _offset_cluster_for_signature(
                signature, rows, kind=kind, total=total,
                comparable=comparable, forms=forms,
            )
        )
        is not None
    ]
    return sorted(clusters, key=lambda c: c.count, reverse=True)


def _dominant_form(
    rows: list[RowComparison], form_counts: dict[str, int]
) -> tuple[str | None, float | None]:
    """The notation this cluster is confined to, and how much of it it covers."""
    if not form_counts:
        return None, None
    per_form = Counter(row.form for row in rows)
    form, hits = per_form.most_common(1)[0]
    if hits != len(rows):
        return form, None  # the cluster spans several notations: not a notation bug
    population = form_counts.get(form, 0)
    if population <= 0:
        return form, None
    return form, hits / population


def signature_histogram(comparisons: list[RowComparison]) -> dict[str, int]:
    """Every disagreement signature and its count, cluster or not."""
    return dict(Counter(c.signature for c in comparisons).most_common())


# ------------------------------------------------------------- CSV dump


CSV_COLUMNS = (
    "tier",
    "rekordbox_id",
    "mik_pk",
    "title",
    "left_raw",
    "right_raw",
    "left_canonical",
    "right_canonical",
    "delta_canonical",
    "signature",
    "raw_notation_form",
    "mik_key_confidence",
)


def dump_disagreements(comparisons: list[RowComparison], out_path: Path) -> Path:
    """Write every disagreeing row. A percentage alone is not a result."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_COLUMNS)
        for row in comparisons:
            writer.writerow(
                [
                    row.tier,
                    row.left_id,
                    row.right_id,
                    row.title,
                    row.left_raw,
                    row.right_raw,
                    row.left_canonical,
                    row.right_canonical,
                    "" if row.delta is None else f"{row.delta:g}",
                    row.signature,
                    row.form,
                    "" if row.key_confidence is None else f"{row.key_confidence:.4f}",
                ]
            )
    return out_path


__all__ = [
    "CSV_COLUMNS",
    "MAPPING_BUG",
    "SUSPECT",
    "Agreement",
    "OffsetCluster",
    "RowComparison",
    "compare_pair",
    "detect_offset_clusters",
    "dump_disagreements",
    "signature_histogram",
]
