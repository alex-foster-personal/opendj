"""A quoted PARITY-01 figure: lane, denominator, date, never a self-chosen parity."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

# The two library-wide totals PARITY-01 forbids as a figure's denominator.
# They include roughly 7800 rows with no audio to analyze. Re-measure live
# library counts; these integers are the trap the requirement names, not
# today's live totals.
FORBIDDEN_DENOMINATOR_TOTALS = frozenset({9986, 10479})


class ForbiddenDenominator(ValueError):
    """A figure divided by 9986 tracks rows or 10479 live djmdContent rows."""


class ParityThresholdNotCalibrated(ValueError):
    """A build tried to call a lane at parity against a number it chose."""


def refuse_forbidden_denominator(n: int, *, what: str) -> None:
    if n in FORBIDDEN_DENOMINATOR_TOTALS:
        raise ForbiddenDenominator(
            f"{what} denominator_n={n} is one of the library-wide row totals "
            f"{sorted(FORBIDDEN_DENOMINATOR_TOTALS)}, which include roughly "
            "7800 rows with no audio to analyze. Name the lane's own "
            "gradable set instead."
        )


@dataclass(frozen=True)
class LaneFigure:
    """One lane's quoted figure. `at_parity` is structurally False this round."""

    lane: str
    status: str
    measured_at: str
    denominator_name: str
    denominator_n: int
    scored_n: int = 0
    exact_n: int | None = None
    within_0_1_n: int | None = None
    within_1_0_n: int | None = None
    related_n: int | None = None
    mirex_mean: float | None = None
    boundary_f_0_5: float | None = None
    boundary_f_3_0: float | None = None
    label_acc: float | None = None
    failed_own_n: int = 0
    no_own_n: int = 0
    at_parity: bool = False
    owner: str | None = None
    reason: str | None = None
    agree_ids: tuple[str, ...] = ()
    disagree_ids: tuple[str, ...] = ()
    octave_ids: tuple[str, ...] = ()
    no_own_ids: tuple[str, ...] = ()
    ungradable_ids: tuple[str, ...] = ()
    ungradable: Mapping[str, int] = field(default_factory=dict)
    median_r: float | None = None
    mean_r: float | None = None
    min_r: float | None = None
    band_median_r: Mapping[str, float] | None = None
    median_r_secondary: float | None = None
    details: Mapping[str, int | float | None] = field(default_factory=dict)

    def __post_init__(self) -> None:
        refuse_forbidden_denominator(self.denominator_n, what=self.lane)
        if self.at_parity:
            raise ParityThresholdNotCalibrated(
                f"lane {self.lane!r} set at_parity=True; thresholds are "
                "agreed with the maintainer before any build describes a lane as at parity"
            )
        if not self.measured_at:
            raise ValueError(
                f"lane {self.lane!r} figure has no measured_at; a figure "
                "without a date is not a figure"
            )
        if not self.denominator_name:
            raise ValueError(
                f"lane {self.lane!r} figure has no denominator_name; a figure "
                "that does not say what it divided by is not a figure"
            )

    def with_denominator(self, n: int, name: str) -> LaneFigure:
        """Return a copy quoting a different denominator, or refuse a trap total."""
        refuse_forbidden_denominator(n, what=self.lane)
        return replace(self, denominator_n=n, denominator_name=name)

    def numeric_fields(self) -> dict[str, Any]:
        """The fields a round-over-round digest compares, ids included."""
        return {
            "lane": self.lane,
            "status": self.status,
            "denominator_n": self.denominator_n,
            "denominator_name": self.denominator_name,
            "scored_n": self.scored_n,
            "exact_n": self.exact_n,
            "within_0_1_n": self.within_0_1_n,
            "within_1_0_n": self.within_1_0_n,
            "related_n": self.related_n,
            "mirex_mean": self.mirex_mean,
            "boundary_f_0_5": self.boundary_f_0_5,
            "boundary_f_3_0": self.boundary_f_3_0,
            "label_acc": self.label_acc,
            "failed_own_n": self.failed_own_n,
            "no_own_n": self.no_own_n,
            "at_parity": self.at_parity,
            "owner": self.owner,
            "reason": self.reason,
            "agree_ids": list(self.agree_ids),
            "disagree_ids": list(self.disagree_ids),
            "octave_ids": list(self.octave_ids),
            "no_own_ids": list(self.no_own_ids),
            "ungradable_ids": list(self.ungradable_ids),
            "ungradable": dict(self.ungradable),
            "median_r": self.median_r,
            "mean_r": self.mean_r,
            "min_r": self.min_r,
            "band_median_r": dict(self.band_median_r) if self.band_median_r else None,
            "median_r_secondary": self.median_r_secondary,
            "details": dict(self.details),
        }
