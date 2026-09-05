"""Shared vocabulary for :mod:`apps.equivalence.verdict` and
:mod:`apps.equivalence.verdict_report`: the status/basis/suite-status
constants and the ``PairVerdict`` dataclass.

Split into its own module purely to break the import cycle a report-emission
module would otherwise have with ``verdict.py`` (``verdict_report`` needs
these; ``verdict.py`` needs ``verdict_report``'s emission functions back).
``verdict.py`` re-exports every name here, so
``from apps.equivalence.verdict import PairVerdict`` (the existing import
shape used throughout the codebase) is unaffected.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from apps.equivalence.compare import Agreement, OffsetCluster
from apps.equivalence.probe import Probe, ScaleMismatch
from apps.shared.equivalence import BASIS_CROSS_SOURCE as _BASIS_CROSS_SOURCE
from apps.shared.equivalence import BASIS_NONE as _BASIS_NONE

PASSED = "passed"
FAILED = "failed"
UNTESTED = "untested"

# Verification basis, per the consumer contract in apps/shared/equivalence.py.
# Imported rather than restated so the two cannot drift apart.
BASIS_CROSS_SOURCE = _BASIS_CROSS_SOURCE
BASIS_NONE = _BASIS_NONE

# The consumer contract has three statuses. The suite distinguishes more, and
# carries the finer label as an extra key so the nuance is not lost: "we ran the
# test and it was inconclusive" is very different from "we never ran it", even
# though both correctly block a write.
SUITE_PASSED = "passed"
SUITE_MAPPING_BUG = "failed_mapping_bug"
SUITE_INCONCLUSIVE = "inconclusive_suspect_cluster"
SUITE_DIFFUSE_DISAGREEMENT = "inconclusive_diffuse_disagreement"
SUITE_NO_DATA = "untested_no_data"
SUITE_UNKNOWN_UNIT = "untested_unknown_unit"
SUITE_PROXY = "untested_proxy_field"
SUITE_THIN = "untested_too_few_pairs"


@dataclass
class PairVerdict:
    """One field pair's verdict plus every piece of evidence behind it."""

    field_name: str
    status: str
    suite_status: str
    reasons: list[str]
    left: Probe
    right: Probe
    agreement: Agreement | None
    clusters: list[OffsetCluster]
    mismatches: list[ScaleMismatch]
    signature_histogram: dict[str, int] = field(default_factory=dict)
    disagreement_csv: str | None = None
    normaliser: str | None = None
    proxy: bool = False
    proxy_reason: str = ""

    @property
    def mapping_bugs(self) -> list[dict[str, Any]]:
        """Everything PROVEN to be OUR error rather than source disagreement."""
        bugs: list[dict[str, Any]] = [
            {"kind": "scale_mismatch", **m.as_dict()} for m in self.mismatches
        ]
        bugs += [
            {"kind": "systematic_offset", **c.as_dict()}
            for c in self.clusters
            if c.is_mapping_bug
        ]
        for probe in (self.left, self.right):
            residual = probe.unmapped_count - probe.out_of_window_count
            if residual:
                bugs.append(
                    {
                        "kind": "normaliser_not_total",
                        "detail": (
                            f"{probe.locator}: {residual} values have no rule "
                            f"(examples {probe.unmapped[:5]})"
                        ),
                    }
                )
        return bugs

    def entry(self, checked_at: str) -> dict[str, Any]:
        """The verdict-file entry. ``status``/``normaliser``/``checked_at`` are
        the consumer's contract; the rest is evidence for humans."""
        return {
            "status": self.status,
            "normaliser": self.normaliser,
            "checked_at": checked_at,
            "suite_status": self.suite_status,
            # Every entry declares its BASIS so a one-sided check can never be
            # mistaken for cross-validation. See apps/equivalence/single_source.py.
            # A non-passing verdict has verified NOTHING, so it must claim
            # 'unverified': the consumer in apps/shared/equivalence.py rejects a
            # failed or untested field that claims a real basis, and it is right
            # to, because 'we compared two sources and it was inconclusive' is
            # not evidence of cross-validation.
            "basis": BASIS_CROSS_SOURCE if self.status == PASSED else BASIS_NONE,
            "basis_meaning": (
                "cross_source: two independent sources hold this field and the "
                "verdict rests on post-normalisation agreement between them."
                if self.status == PASSED
                else "unverified: this field verified nothing, so it names no basis."
            ),
            "shape": "scalar",
            "reasons": self.reasons,
            "proxy": self.proxy,
            "proxy_reason": self.proxy_reason or None,
            "left": self.left.as_dict(),
            "right": self.right.as_dict(),
            "agreement": None if self.agreement is None else self.agreement.as_dict(),
            "disagreement_signatures": self.signature_histogram,
            "systematic_offsets": [c.as_dict() for c in self.clusters],
            "mapping_bugs": self.mapping_bugs,
            "disagreement_csv": self.disagreement_csv,
        }
