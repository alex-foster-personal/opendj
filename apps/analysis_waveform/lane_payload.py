"""Peak columns -> the own waveform lane's ok payload shape.

Pure conversion: no filesystem, no ffmpeg, no store. The mapped-track overlay
in `own_waveform_overlay.py` deep-copies `preview` and `detail` from records
built here.

-Cursor
"""
from __future__ import annotations

from typing import Any

import numpy as np

from apps.analysis_waveform.bands import (
    _bands_payload,
    _downsample_max,
    measured_peak_bands,
    pwv6_scaled_bands,
)
from apps.analysis_waveform.decode import OVERVIEW_COLUMNS

REASON_NOT_DECODED = "not_decoded"


def build_waveform_payload(peaks: np.ndarray, *, points: int | None = None) -> dict[str, Any]:
    """Turn ``(n, 3)`` uint8 peak columns into the ok lane payload.

    When ``points`` is set, both ``preview`` and ``detail`` downsample to that
    width (``local_anlz_payload`` passes the client's ``/anlz`` budget). When
    omitted, each section keeps its natural band length (store backfill path).
    """
    overview = _downsample_max(peaks, OVERVIEW_COLUMNS)
    # The overview is drawn like a rekordbox PWV6 preview; the detail keeps
    # the measured peaks, which the scrolling lane normalizes per band anyway.
    preview_bands = pwv6_scaled_bands(overview)
    detail_bands = measured_peak_bands(peaks)
    if points is not None:
        preview_points = detail_points = points
    else:
        preview_points = int(preview_bands["low"].shape[0])
        detail_points = int(detail_bands["low"].shape[0])
    return {
        "kind": "tri",
        "preview": _bands_payload(preview_bands, points=preview_points),
        "detail": _bands_payload(detail_bands, points=detail_points),
    }


__all__ = ["REASON_NOT_DECODED", "build_waveform_payload"]
