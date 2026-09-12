"""Own-envelope producer for PARITY-01 waveform lanes."""

from __future__ import annotations

import numpy as np
import pytest

from apps.parity.waveform_own import envelopes_from_peaks

pytestmark = pytest.mark.requirement("PARITY-01")


def test_envelopes_from_peaks_builds_preview_detail_and_triband() -> None:
    peaks = np.zeros((16, 3), dtype=np.uint8)
    peaks[:, 0] = np.arange(16, dtype=np.uint8) * 8
    peaks[:, 1] = np.arange(16, dtype=np.uint8) * 4 + 1
    peaks[:, 2] = 0
    result = envelopes_from_peaks(peaks)
    assert set(result["own_triband"]) == {"low", "mid", "high"}
    assert result["own_triband"]["low"] != result["own_triband"]["mid"]
    assert max(result["own_detail"]) == pytest.approx(max(result["own_triband"]["low"]))
    assert max(result["own_preview"]) >= max(result["own_detail"])
