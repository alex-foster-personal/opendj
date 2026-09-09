"""The ``own_loudness.backfill`` producer: adapts a scan into a LaneResult.

Adapter half of NATIVE-07 (specs/native-analysis-v1-lanes/nav1-loudness.md).
This module maps :func:`apps.analysis_loudness.adapter.analyze_file` onto the
``loudness`` lane's payload shape (``apps.analysis.lane_payloads``) under
:data:`PRODUCER_BACKEND`, the canonical own-producer name for this lane.

Per nav1-loudness.md's own split clause ("land the adapter, the gate and its
measurements now with no registration claim at all, and open the registration
as its own follow-up PR against the merged contract"), this module makes no
registration claim: there is no dispatcher, in this PR or any other lane's,
that resolves ``own_<lane>.<producer>`` names to a producer callable, and
``apps.analysis.run --backend`` resolves exclusively through
``apps.analysis.backends.get_backend``, whose ``AnalyzerBackend`` protocol
returns a full ``AnalysisRecord`` (bpm, key, energy, ...) that a single-lane
producer cannot supply. Wiring ``own_loudness.backfill`` into a real command
path is fleet-wide infrastructure (a wave-1 dispatcher, per
``apps/analysis_beatgrid/cli.py``'s own docstring deferring ``AnalysisRecord``
construction the same way), not a per-lane change, and is out of scope here.
"""
from __future__ import annotations

import math
from pathlib import Path

from apps.analysis.lanes import LaneResult, own_backend

from .adapter import analyze_file

#: The canonical own-producer name for this lane, per the backend naming
#: scheme in apps.analysis.lanes (own_<lane>.<producer>).
PRODUCER_BACKEND: str = own_backend("loudness", "backfill")

#: The four loudness payload measurements, checked for finiteness below.
_MEASUREMENT_FIELDS = ("integrated_lufs", "true_peak_dbtp", "loudness_range_lu", "rms_db")


def produce_lane_result(path: Path) -> LaneResult:
    """Measure ``path`` and return its ``loudness`` lane result.

    Raises whatever :func:`analyze_file` raises (``LoudnessError``, e.g. a
    missing ffmpeg): a producer that could not measure has nothing to report
    as ``status: ok``, so it fails loudly rather than emitting one.

    A fully silent file decodes fine but yields ``-inf`` true peak and RMS
    from ffmpeg's own R128 scanner; that is a measurement the lane's payload
    contract rejects (:func:`apps.analysis.lane_payloads._require_number`
    requires every value finite), so it is reported as ``status: failed``
    rather than an ``ok`` payload no consumer could validate.
    """
    measured = analyze_file(path)
    payload = {
        "integrated_lufs": measured.integrated_lufs,
        "true_peak_dbtp": measured.true_peak_dbtp,
        "loudness_range_lu": measured.loudness_range_lu,
        "rms_db": measured.rms_db,
    }
    if not all(math.isfinite(payload[field]) for field in _MEASUREMENT_FIELDS):
        return LaneResult(status="failed", reason="non_finite_measurement")
    return LaneResult(status="ok", payload=payload)


__all__ = ["PRODUCER_BACKEND", "produce_lane_result"]
