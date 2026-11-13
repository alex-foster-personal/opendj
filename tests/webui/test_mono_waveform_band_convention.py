"""Backend half of the mono waveform band convention (issue #12585).

A heights-only lane (PWAV/PWV3) is served as ``kind: "mono"`` with ONE real
envelope repeated under low/mid/high; the browser paints the per-point max of
the three as a single mono envelope. Both halves read the same fixture,
``apps/webui/frontend/tests/unit/fixtures/mono-waveform-band-convention.json``;
``mono-waveform-band-convention.test.mjs`` is the browser half.

Regression one-liners:
  - if a mono payload's low, mid and high arrays differ at any point then broken
  - if the mono payload drifts from the fixture the browser test renders then broken
  - if downsampling a mono lane splits the three bands apart then broken

[if] a waveform payload declares mono bands [then] low, mid and high carry one envelope, [else stop].
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from apps.analysis_waveform.bands import _bands_payload, _mono_bands

pytestmark = pytest.mark.requirement("PARITY-03")

FIXTURE = (
    Path(__file__).resolve().parents[2] / "apps/webui/frontend/tests/unit/fixtures/mono-waveform-band-convention.json"
)


class _HeightsTag:
    """Stand-in for a parsed PWAV/PWV3 tag: ``get()[0]`` is the heights array."""

    def __init__(self, heights: list[int]) -> None:
        """Hold the raw 0..31 heights."""
        self._heights = heights

    def get(self) -> tuple[list[int]]:
        """Return the heights the way pyrekordbox's tag does."""
        return (self._heights,)


def _fixture() -> dict[str, Any]:
    """The shared cross-surface fixture."""
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_mono_payload_matches_the_fixture_the_browser_renders() -> None:
    """[if] a local waveform payload declares mono bands [then] it equals the fixture the browser paints, [else stop]."""
    fx = _fixture()
    detail = _bands_payload(_mono_bands(_HeightsTag(fx["pwav_heights"])), fx["points"])
    assert fx["waveform"]["kind"] == "mono"
    assert detail == fx["waveform"]["detail"]


def test_downsampled_mono_lane_keeps_the_bands_identical() -> None:
    """[if] a mono lane is downsampled [then] the three bands stay identical, [else stop]."""
    heights = [0, 31, 15, 8, 24, 3, 0, 31, 12, 5, 29, 1]
    detail = _bands_payload(_mono_bands(_HeightsTag(heights)), 5)
    assert detail["length"] == 5
    assert detail["low"] == detail["mid"] == detail["high"]
