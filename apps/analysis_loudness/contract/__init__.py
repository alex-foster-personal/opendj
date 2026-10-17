"""Public contract of :mod:`apps.analysis_loudness` (ADR-NEW-module-contracts).

Code outside this package imports only this module. It names the producer
identity the ``loudness`` lane stamps on its records and the one operation
outsiders call, :meth:`LoudnessAnalysisService.produce_lane_result`. The
implementation is :class:`apps.analysis_loudness.service.LoudnessAnalysisServiceImpl`,
built by :func:`apps.analysis_loudness.http.build_service` for the delivery layers.

Imports here stay stdlib-only. ``LaneResult`` is named for type checkers only:
it lives in :mod:`apps.analysis.lanes`, which is not a contract module.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol, runtime_checkable

if TYPE_CHECKING:
    from apps.analysis.lanes import LaneResult

CONTRACT_VERSION: Final[str] = "analysis_loudness/1"

#: The canonical own-producer name for this lane, per the backend naming
#: scheme in apps.analysis.lanes (own_<lane>.<producer>). Held equal to
#: ``own_backend("loudness", "backfill")`` by tests/analysis_loudness.
PRODUCER_BACKEND: Final[str] = "own_loudness.backfill"

#: Semver stamped on both ``AnalysisRecord.backend_version`` and
#: ``AnalysisRecord.producer_version``. The own-record validator requires
#: those two to agree; canonical selection ranks them as semver.
PRODUCER_VERSION: Final[str] = "1.0.0"


@runtime_checkable
class LoudnessAnalysisService(Protocol):
    """The operations code outside :mod:`apps.analysis_loudness` uses."""

    def produce_lane_result(self, path: Path, *, binary: str | None = None) -> LaneResult:
        """Measure ``path`` and return its ``loudness`` lane result.

        Raises ``apps.loudness.scan.LoudnessError`` when it cannot measure;
        a non-finite measurement is a ``failed`` lane result, never ``ok``.
        """
        ...


__all__ = [
    "CONTRACT_VERSION",
    "PRODUCER_BACKEND",
    "PRODUCER_VERSION",
    "LoudnessAnalysisService",
]
