"""Implementation of :class:`~apps.analysis_loudness.contract.LoudnessAnalysisService`.

Delegates to the existing producer functions; no logic lives here.
"""

from __future__ import annotations

from pathlib import Path

from apps.analysis.lanes import LaneResult

from .backfill import produce_lane_result


class LoudnessAnalysisServiceImpl:
    """The shipped R128 loudness producer behind its contract."""

    def produce_lane_result(self, path: Path, *, binary: str | None = None) -> LaneResult:
        return produce_lane_result(path, binary=binary)


__all__ = ["LoudnessAnalysisServiceImpl"]
