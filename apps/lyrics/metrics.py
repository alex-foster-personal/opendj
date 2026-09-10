"""Word-onset alignment metrics (MIREX-style) for lyric alignment evals.

Scoring contract (specs/karaoke-lyrics-alignment.md): predictions are forced alignments
of the reference word list, so ref and pred are compared 1:1 by index and a length
mismatch is a hard error, never a warning.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass

TOLERANCES_S: tuple[float, ...] = (0.05, 0.1, 0.2, 0.3, 0.5)


@dataclass(frozen=True)
class OnsetErrorReport:
    n_words: int
    mean_abs_error_s: float  # AAE, the MIREX headline number
    median_abs_error_s: float
    p95_abs_error_s: float
    within: dict[float, float]  # tolerance_s -> fraction of onsets within it


#-----------------------------------------------------------------------------


def _p95(sorted_values: list[float]) -> float:
    if not sorted_values:
        raise ValueError("empty values")
    idx = min(len(sorted_values) - 1, math.ceil(0.95 * len(sorted_values)) - 1)
    return sorted_values[idx]


def score_onsets(ref_starts_s: list[float], pred_starts_s: list[float]) -> OnsetErrorReport:
    if len(ref_starts_s) != len(pred_starts_s):
        raise ValueError(
            f"forced alignment must cover every reference word: "
            f"{len(ref_starts_s)} ref vs {len(pred_starts_s)} pred onsets"
        )
    if not ref_starts_s:
        raise ValueError("no words to score")
    abs_errors = sorted(abs(r - p) for r, p in zip(ref_starts_s, pred_starts_s, strict=True))
    if any(math.isnan(e) for e in abs_errors):
        raise ValueError("NaN onset in ref or pred")
    return OnsetErrorReport(
        n_words=len(abs_errors),
        mean_abs_error_s=statistics.fmean(abs_errors),
        median_abs_error_s=statistics.median(abs_errors),
        p95_abs_error_s=_p95(abs_errors),
        within={
            tol: sum(1 for e in abs_errors if e <= tol) / len(abs_errors) for tol in TOLERANCES_S
        },
    )


def aggregate(reports: list[OnsetErrorReport]) -> OnsetErrorReport:
    """Word-pooled aggregate across songs (every word counts once, long songs weigh more)."""
    if not reports:
        raise ValueError("no reports to aggregate")
    n = sum(r.n_words for r in reports)
    # Pooled mean/within are exact from per-song stats; median/p95 are approximated by the
    # n-weighted mean of per-song values (exact pooling would need raw errors).
    return OnsetErrorReport(
        n_words=n,
        mean_abs_error_s=sum(r.mean_abs_error_s * r.n_words for r in reports) / n,
        median_abs_error_s=sum(r.median_abs_error_s * r.n_words for r in reports) / n,
        p95_abs_error_s=sum(r.p95_abs_error_s * r.n_words for r in reports) / n,
        within={
            tol: sum(r.within[tol] * r.n_words for r in reports) / n for tol in TOLERANCES_S
        },
    )


def format_report(label: str, report: OnsetErrorReport) -> str:
    within = "  ".join(f"@{int(tol * 1000)}ms {frac:6.1%}" for tol, frac in report.within.items())
    return (
        f"{label:<44s} n={report.n_words:>5d}  AAE {report.mean_abs_error_s:7.3f}s  "
        f"med {report.median_abs_error_s:7.3f}s  p95 {report.p95_abs_error_s:7.3f}s  {within}"
    )
