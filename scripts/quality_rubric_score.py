"""Map probe findings to integer dimension scores."""

from __future__ import annotations

from typing import Any

from scripts.quality_rubric_model import Dimension


def score_dimension(dimension: Dimension, findings: list[dict[str, Any]]) -> int:
    if any(f.get("class") == "probe_unscorable" for f in findings):
        return 0
    points_by_class = {d.finding: d.points for d in dimension.deductions}
    score = 5
    for finding in findings:
        cls = str(finding.get("class", ""))
        score -= points_by_class.get(cls, 0)
    return max(0, min(5, score))


def sort_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        findings,
        key=lambda f: (
            f.get("code") or "",
            str(f.get("class", "")),
            str(f.get("path", "")),
            str(f.get("href") or ""),
        ),
    )
