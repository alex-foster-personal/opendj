"""Peak columns -> the own waveform lane's ok payload shape.

Pure conversion: no filesystem, no ffmpeg, no store. The mapped-track overlay
in `own_waveform_overlay.py` deep-copies `preview` and `detail` from records
built here.

-Cursor
"""
from __future__ import annotations

from typing import Any

import numpy as np

from apps.analysis_waveform.bands import _bands_payload, _downsample_max, pwv6_scaled_bands
from apps.analysis_waveform.decode import BAND_NAMES, OVERVIEW_COLUMNS

REASON_NOT_DECODED = "not_decoded"
_PEAK_SCALE = 255.0


def _bands_from_peaks(peaks: np.ndarray) -> dict[str, np.ndarray]:
    """Three measured envelopes, 0..1, under their band keys."""
    scaled = np.clip(peaks.astype(np.float64) / _PEAK_SCALE, 0.0, 1.0)
    return {name: scaled[:, index] for index, name in enumerate(BAND_NAMES)}


def build_waveform_payload(peaks: np.ndarray) -> dict[str, Any]:
    """Turn ``(n, 3)`` uint8 peak columns into the ok lane payload."""
    overview = _downsample_max(peaks, OVERVIEW_COLUMNS)
    # The overview is drawn like a rekordbox PWV6 preview; the detail keeps
    # the measured peaks, which the scrolling lane normalizes per band anyway.
    preview_bands = pwv6_scaled_bands(overview)
    detail_bands = _bands_from_peaks(peaks)
    preview_len = int(preview_bands["low"].shape[0])
    detail_len = int(detail_bands["low"].shape[0])
    return {
        "kind": "tri",
        "preview": _bands_payload(preview_bands, points=preview_len),
        "detail": _bands_payload(detail_bands, points=detail_len),
    }


__all__ = ["REASON_NOT_DECODED", "build_waveform_payload"]
