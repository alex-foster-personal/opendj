"""The gate: turn probes, agreement and clusters into a per-field verdict.

Output contract is ``apps.shared.equivalence`` (unit A's consumer), which is
the authority on shape::

    {"meta": {...},
     "fields": {"<our field>": {"status": "passed"|"failed"|"untested",
                                "normaliser": "<name>|null",
                                "checked_at": "<ISO-8601>", ...}}}

Extra keys inside a field entry are carried for humans and ignored by the
consumer's parser, so the full evidence travels with the verdict.

A field is ``passed`` ONLY if all of these hold:

1. both sides have a comparable declared unit and non-empty data
2. no scale mismatch (declared-unit implausible, or a cross-source factor)
3. the normaliser is TOTAL over the observed domain (zero unmapped values)
4. no systematic-offset cluster remains after normalisation
5. at least ``CFG.min_comparable`` comparable pairs, and the pair is not a proxy

Anything that trips 2, 3 or 4 is ``failed`` and is reported as a MAPPING BUG.
Anything that merely lacks data or a decidable unit is ``untested``. Absence
of evidence is never recorded as agreement.
"""

from __future__ import annotations

from apps.equivalence.compare import Agreement, OffsetCluster
from apps.equivalence.config import CFG, FieldPair
from apps.equivalence.normalisers import normaliser_name, units_incompatible
from apps.equivalence.probe import (
    Probe,
    ScaleMismatch,
    cross_source_scale_mismatch,
    declared_unit_mismatch,
)
from apps.equivalence.verdict_report import build_document, render_report, write_document
from apps.equivalence.verdict_types import (
    FAILED,
    PASSED,
    SUITE_DIFFUSE_DISAGREEMENT,
    SUITE_INCONCLUSIVE,
    SUITE_MAPPING_BUG,
    SUITE_NO_DATA,
    SUITE_PASSED,
    SUITE_PROXY,
    SUITE_THIN,
    SUITE_UNKNOWN_UNIT,
    UNTESTED,
    PairVerdict,
)

# ------------------------------------------------------------- the gate


def _structural_check(
    verdict: PairVerdict, spec: FieldPair, left: Probe, right: Probe
) -> bool:
    """Step 1: is a comparison meaningful at all? Mutates ``verdict`` in
    place and returns ``True`` when :func:`decide` must return immediately.

    Checked before any cluster, because a delta cluster measured against a
    column that holds one value (MIK's all-zero ``ZRATING``) is an artefact
    of the missing data, not a mapping bug -- an earlier version of this
    function reported exactly that and called rating FAILED at
    "additive_+5".
    """
    family = units_incompatible(spec.left.kind, spec.left.unit, spec.right.unit)
    if family is not None:
        verdict.suite_status = SUITE_UNKNOWN_UNIT
        verdict.reasons.append(f"UNTESTED: {family}")
        return True
    structural = False
    for probe, side in ((left, "left"), (right, "right")):
        if probe.comparable:
            continue
        # First cause wins, so the label is deterministic: absent or constant
        # data is a stronger statement than an undecidable unit.
        if not structural and probe.declared_unit in ("unknown", "unknown_db_family"):
            verdict.suite_status = SUITE_UNKNOWN_UNIT
        structural = True
        verdict.reasons.append(
            f"UNTESTED: {side} side {probe.locator} is not comparable - "
            + (
                "; ".join(probe.findings)
                or f"declared unit {probe.declared_unit!r}, {probe.present} values"
            )
        )
    if structural:
        verdict.reasons.append(
            "Any agreement rate on this pair would be an artefact: with one "
            "side absent, constant or in an undecidable unit there is nothing "
            "to be apples-to-apples WITH."
        )
    return structural


def _mechanical_failures(
    verdict: PairVerdict,
    left: Probe,
    right: Probe,
    mismatches: list[ScaleMismatch],
    clusters: list[OffsetCluster],
) -> None:
    """Steps 2/3/4, in the order the skill lists them. Mutates ``verdict``
    to FAILED if any mechanical check trips; leaves it untouched otherwise.
    """
    if mismatches:
        verdict.status = FAILED
        verdict.suite_status = SUITE_MAPPING_BUG
        verdict.reasons.extend(f"MAPPING BUG: {m.detail}" for m in mismatches)
    residual = sum(p.unmapped_count - p.out_of_window_count for p in (left, right))
    if residual:
        verdict.status = FAILED
        verdict.suite_status = SUITE_MAPPING_BUG
        verdict.reasons.append(
            f"MAPPING BUG: the normaliser is not total over the observed "
            f"domain ({residual} unmapped values). A silent pass-through here "
            f"is the bug SKILL 4b step 5 forbids"
        )
    proven = [c for c in clusters if c.is_mapping_bug]
    if proven:
        verdict.status = FAILED
        verdict.suite_status = SUITE_MAPPING_BUG
        verdict.reasons.extend(
            f"MAPPING BUG: systematic offset {c.signature!r} on {c.count} rows "
            f"({c.share:.1%} of disagreements, {c.population_share:.1%} of the "
            f"comparable population"
            + (
                f", {c.subset_coverage:.0%} of all {c.subset_form} rows"
                if c.subset_coverage is not None
                else ""
            )
            + f") - {c.interpretation}"
            for c in proven
        )


def _evidence_check(
    verdict: PairVerdict,
    spec: FieldPair,
    left: Probe,
    right: Probe,
    agreement: Agreement | None,
    clusters: list[OffsetCluster],
    normaliser: str,
) -> None:
    """Step 5: enough evidence, not a proxy, and nothing unexplained left
    over. Mutates ``verdict`` to its final PASSED/UNTESTED/INCONCLUSIVE
    state; each branch below is a terminal outcome for this step.
    """
    if spec.proxy:
        verdict.suite_status = SUITE_PROXY
        verdict.reasons.append(f"UNTESTED: {spec.proxy_reason}")
        return
    if agreement is None or agreement.comparable < CFG.min_comparable:
        got = 0 if agreement is None else agreement.comparable
        verdict.suite_status = SUITE_THIN
        verdict.reasons.append(
            f"UNTESTED: only {got} comparable pairs, below the "
            f"{CFG.min_comparable} needed for a rate to be evidence"
        )
        return
    suspects = [c for c in clusters if not c.is_mapping_bug]
    if suspects:
        verdict.suite_status = SUITE_INCONCLUSIVE
        verdict.reasons.append(
            "INCONCLUSIVE: the scale checks pass and the normaliser is total, "
            "but named delta clusters remain in the disagreements and they are "
            "too narrow to prove a unit or convention error. Per SKILL 4b that "
            "is AMBIGUOUS: either one source is wrong or the fields are not the "
            "same field, and no percentage can tell you which. Adjudicate the "
            "clusters below (known-answer harness) before this field feeds the "
            "precedence policy."
        )
        verdict.reasons.extend(
            f"SUSPECT CLUSTER {c.signature!r}: {c.count} rows "
            f"({c.share:.1%} of disagreements, {c.population_share:.1%} of the "
            f"comparable population) - {c.interpretation}"
            for c in suspects
        )
        return

    # A disagreement rate is only silence, not evidence, when nothing repeats
    # at one delta often enough to be named above. Deltas dispersed across
    # many distinct, individually-too-small signatures never reach a cluster
    # threshold, so "no cluster found" must not be read as "the sources
    # agree": require the unexplained disagreement to be under the same noise
    # floor a named cluster would have to clear to even be reported.
    disagreement_share = (
        agreement.disagree / agreement.comparable if agreement.comparable else 0.0
    )
    if disagreement_share > CFG.suspect_population_share:
        verdict.suite_status = SUITE_DIFFUSE_DISAGREEMENT
        verdict.reasons.append(
            f"INCONCLUSIVE: {agreement.disagree}/{agreement.comparable} "
            f"({disagreement_share:.1%}) of comparable pairs disagree with no "
            f"named delta cluster explaining any of it. Deltas that never "
            f"repeat often enough at one value to be named are not proof the "
            f"fields agree; per SKILL 4b, a disagreement rate is meaningless "
            f"until it is understood, and absence of a named cluster is not "
            f"the same as absence of disagreement"
        )
        return

    verdict.status = PASSED
    verdict.suite_status = SUITE_PASSED
    verdict.normaliser = normaliser
    verdict.reasons.append(
        f"PASSED: no scale mismatch, normaliser total over "
        f"{left.present + right.present} observed values, no delta cluster left "
        f"in {agreement.disagree} disagreements, "
        f"{agreement.agree}/{agreement.comparable} agree "
        f"({(agreement.rate or 0):.1%})"
    )


def decide(
    spec: FieldPair,
    left: Probe,
    right: Probe,
    agreement: Agreement | None,
    clusters: list[OffsetCluster],
) -> PairVerdict:
    """Apply the gate conditions in order (see the numbered steps in
    :func:`_structural_check`, :func:`_mechanical_failures` and
    :func:`_evidence_check`). Ordering is load-bearing; see
    :func:`_structural_check` for why STRUCTURAL is checked first.
    """
    mismatches: list[ScaleMismatch] = []
    for probe in (left, right):
        found = declared_unit_mismatch(probe)
        if found is not None:
            mismatches.append(found)
    cross = cross_source_scale_mismatch(left, right)
    if cross is not None:
        mismatches.append(cross)

    normaliser = (
        f"{normaliser_name(spec.left.kind, spec.left.unit)} | "
        f"{normaliser_name(spec.right.kind, spec.right.unit)}"
    )

    verdict = PairVerdict(
        field_name=spec.field_name,
        status=UNTESTED,
        suite_status=SUITE_NO_DATA,
        reasons=[],
        left=left,
        right=right,
        agreement=agreement,
        clusters=clusters,
        mismatches=mismatches,
        normaliser=None,
        proxy=spec.proxy,
        proxy_reason=spec.proxy_reason,
    )

    if _structural_check(verdict, spec, left, right):
        return verdict

    _mechanical_failures(verdict, left, right, mismatches, clusters)
    if verdict.status == FAILED:
        return verdict

    _evidence_check(verdict, spec, left, right, agreement, clusters, normaliser)
    return verdict


__all__ = [
    "FAILED",
    "PASSED",
    "UNTESTED",
    "PairVerdict",
    "build_document",
    "decide",
    "render_report",
    "write_document",
]
