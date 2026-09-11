"""The ``own_loudness.backfill`` producer: adapts a scan into a LaneResult.

Adapter half of NATIVE-07 (specs/native-analysis-v1-lanes/nav1-loudness.md).
This module maps :func:`apps.analysis_loudness.adapter.analyze_file` onto the
``loudness`` lane's payload shape (``apps.analysis.lane_payloads``) under
:data:`PRODUCER_BACKEND`, the canonical own-producer name for this lane.

This module remains the lane producer. The ``AnalyzerBackend`` wrapper that
builds a v2 ``AnalysisRecord`` and registers under ``own_loudness.backfill``
lives in :mod:`apps.analysis.backends.own_loudness`, selected through
``apps.analysis.run --backend own_loudness.backfill``.
"""

from __future__ import annotations

import math
from pathlib import Path

from apps.analysis.lanes import LaneResult, own_backend

from .adapter import analyze_file

#: The canonical own-producer name for this lane, per the backend naming
#: scheme in apps.analysis.lanes (own_<lane>.<producer>).
PRODUCER_BACKEND: str = own_backend("loudness", "backfill")

#: Semver stamped on both ``AnalysisRecord.backend_version`` and
#: ``AnalysisRecord.producer_version``. The own-record validator requires
#: those two to agree; canonical selection ranks them as semver.
PRODUCER_VERSION: str = "1.0.0"

#: The four loudness payload measurements, checked for finiteness below.
_MEASUREMENT_FIELDS = ("integrated_lufs", "true_peak_dbtp", "loudness_range_lu", "rms_db")


def produce_lane_result(path: Path, *, binary: str | None = None) -> LaneResult:
    """Measure ``path`` and return its ``loudness`` lane result.

    Raises whatever :func:`analyze_file` raises (``LoudnessError``, e.g. a
    missing ffmpeg): a producer that could not measure has nothing to report
    as ``status: ok``, so it fails loudly rather than emitting one.

    ``binary`` is the already-resolved ffmpeg executable the backend wrapper
    shares across fingerprint decode, R128 and RMS. Direct callers omit it.

    A fully silent file decodes fine but yields ``-inf`` true peak and RMS
    from ffmpeg's own R128 scanner; that is a measurement the lane's payload
    contract rejects (:func:`apps.analysis.lane_payloads._require_number`
    requires every value finite), so it is reported as ``status: failed``
    rather than an ``ok`` payload no consumer could validate.
    """
    measured = analyze_file(path, binary=binary)
    payload = {
        "integrated_lufs": measured.integrated_lufs,
        "true_peak_dbtp": measured.true_peak_dbtp,
        "loudness_range_lu": measured.loudness_range_lu,
        "rms_db": measured.rms_db,
    }
    if not all(math.isfinite(payload[field]) for field in _MEASUREMENT_FIELDS):
        return LaneResult(status="failed", reason="non_finite_measurement")
    return LaneResult(status="ok", payload=payload)


__all__ = ["PRODUCER_BACKEND", "PRODUCER_VERSION", "produce_lane_result"]
