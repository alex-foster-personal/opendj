"""Coverage ledger helpers for release discovery."""

from __future__ import annotations

from typing import Literal

from apps.compatint.schema import DiscoveryCoverageAxis, MixxxDiscoveryResult

_CoverageStatus = Literal["partial", "complete"]


def build_discovery_coverage(*, acquired: int, denominator: int) -> DiscoveryCoverageAxis:
    if acquired < 0 or denominator < 0:
        raise ValueError("coverage counts must be non-negative")
    if acquired > denominator:
        raise ValueError("acquired exceeds discovery denominator")
    status: _CoverageStatus = "complete" if acquired == denominator and denominator > 0 else "partial"
    return DiscoveryCoverageAxis(acquired=acquired, denominator=denominator, status=status)


def format_mixxx_discovery_coverage_line(result: MixxxDiscoveryResult) -> str:
    cov = result.discovery_coverage
    return f"Mixxx discovery coverage: {cov.acquired}/{cov.denominator} ({result.list_kind} set; not exhaustive)"


def render_discovery_report(result: MixxxDiscoveryResult) -> str:
    lines = [
        format_mixxx_discovery_coverage_line(result),
        f"Mixxx stable releases recorded: {len(result.stable_releases)}",
        f"Mixxx release gaps recorded: {len(result.gaps)}",
    ]
    return "\n".join(lines) + "\n"
