"""Thin own-envelope producer for PARITY-01 waveform lanes.

Turns decode_peaks output into preview, detail, and tri-band envelopes
without touching the analysis queue, state.db, or webui.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from apps.analysis_waveform.bands import _downsample_max
from apps.analysis_waveform.decode import BAND_NAMES, OVERVIEW_COLUMNS, decode_peaks


def envelopes_from_peaks(peaks: np.ndarray) -> dict[str, Any]:
    """Build the three PARITY-01 own envelopes from uint8 ``(n, 3)`` peaks."""
    if peaks.ndim != 2 or peaks.shape[1] != 3:
        raise ValueError(f"peaks must be (n, 3); got {peaks.shape}")
    detail = peaks.astype(np.float64) / 255.0
    preview_detail = _downsample_max(detail, OVERVIEW_COLUMNS)
    own_triband = {name: detail[:, index].tolist() for index, name in enumerate(BAND_NAMES)}
    own_detail = detail.max(axis=1).tolist()
    own_preview = preview_detail.max(axis=1).tolist()
    return {
        "own_triband": own_triband,
        "own_detail": own_detail,
        "own_preview": own_preview,
    }


def envelopes_from_audio(path: Path) -> dict[str, Any]:
    """Decode audio via ffmpeg and return the three own envelopes."""
    return envelopes_from_peaks(decode_peaks(path))
