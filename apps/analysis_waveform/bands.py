"""Waveform band arithmetic: band dicts (and raw ANLZ tag entries) -> payload bands.

Split out of ``anlz.py`` for the same reason ``native.py`` was (see its
docstring): this is band arithmetic, not ANLZ container decode, and ``anlz.py``
had reached the 600-line gate. Nothing here touches the filesystem, pyrekordbox,
or a PMAI container -- it takes an already-parsed tag (or a band dict) and does
numpy work, which is also what makes the native/Python equivalence tests cheap.

Homed in ``apps.analysis_waveform`` rather than ``apps.webui`` since the own
tri-band lane landed (NATIVE-06, specs/native-analysis-v1.md D5). Both the
rekordbox ANLZ path and this repo's own ffmpeg decoder shape their bands with
these functions, and a domain package importing the delivery layer to reach
them would have closed an ``apps.analysis_waveform <-> apps.webui`` package
cycle (the inversion .importlinter's DEBT block prescribes, applied before the
cycle existed rather than after).

webui consumers still reach every name here through the ``rb_vendor`` facade.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from apps.analysis_waveform.native import _WAVEFORM_NATIVE

# The ONE browser-strip width (SPIKE-A1 section 2): uint8[120][3] interleaved
# [low, mid, hi]. Homed here rather than restated per source, so the rekordbox
# ANLZ strip and this repo's own decoded strip cannot drift to two widths.
STRIP_COLUMNS: int = 120
# PWV6/PWV7 raw byte columns -> band names (SPIKE-A1 section 3 proof).
_BAND_COLUMNS: tuple[tuple[str, int], ...] = (("low", 0), ("mid", 1), ("high", 2))
_TRI_SCALE: float = 127.0
_MONO_SCALE: float = 31.0


def _downsample_max(arr: np.ndarray, points: int) -> np.ndarray:
    """Per-bucket max downsample along axis 0 to <= ``points`` entries."""
    n = arr.shape[0]
    if n <= points:
        return arr
    edges = (np.arange(points) * n) // points
    return np.maximum.reduceat(arr, edges, axis=0)


def _bands_payload_python(bands: dict[str, np.ndarray], points: int) -> dict[str, Any]:
    """Original NumPy/Python implementation retained as the exact fallback."""
    out: dict[str, Any] = {}
    length = 0
    for name, arr in bands.items():
        down = _downsample_max(arr, points)
        length = int(down.shape[0])
        out[name] = [round(float(v), 4) for v in down]
    out["length"] = length
    return out


def _bands_payload(bands: dict[str, np.ndarray], points: int) -> dict[str, Any]:
    lengths = {int(arr.shape[0]) for arr in bands.values()}
    if len(lengths) > 1:
        raise ValueError("waveform bands must have equal lengths")
    if _WAVEFORM_NATIVE is not None:
        return _WAVEFORM_NATIVE.bands_payload(bands, points)
    return _bands_payload_python(bands, points)


def _tri_bands(tag: Any) -> dict[str, np.ndarray]:
    """Decode raw PWV6/PWV7 3-byte entries -> low/mid/high floats 0..1."""
    raw = np.frombuffer(tag.content.entries, dtype=np.uint8).reshape(-1, 3)
    scaled = np.clip(raw.astype(np.float64) / _TRI_SCALE, 0.0, 1.0)
    return {name: scaled[:, col] for name, col in _BAND_COLUMNS}


# What rekordbox writes into PWV6 for a full-scale sine in each band, (low,
# mid, high), parsed from rekordcrate's public sweep fixture
# (docs/research/dj-waveform-display-sota-20260922.md section 7). rekordbox
# does not encode its three bands on one scale: a sine that fills our low band
# to 255 is a 69 there, and a high one is 176. Own peaks put through this
# scale draw with rekordbox's balance (white highs on top, blue lows and amber
# mids underneath) instead of a low band that buries the other two
# (specs/ui-contracts/library-preview-waveform). One fixture on one rekordbox
# version: a real-music calibration against PWV6 can replace it.
PWV6_FULL_SCALE_SINE: tuple[int, int, int] = (69, 86, 176)
#: Bumped whenever the bytes ``pwv6_scale`` writes for the same peaks change.
PWV6_SCALE_VERSION: str = "pwv6-sine-1"
_OWN_PEAK_FULL_SCALE: float = 255.0


def pwv6_scale(peaks: np.ndarray) -> np.ndarray:
    """Own ``(n, 3)`` uint8 peak columns re-encoded on rekordbox PWV6's scale."""
    gain = np.asarray(PWV6_FULL_SCALE_SINE, dtype=np.float64) / _OWN_PEAK_FULL_SCALE
    return np.clip(np.rint(peaks.astype(np.float64) * gain), 0, 255).astype(np.uint8)


def pwv6_scaled_bands(peaks: np.ndarray) -> dict[str, np.ndarray]:
    """Own peak columns as 0..1 bands, scaled exactly as a PWV6 tag is."""
    scaled = np.clip(pwv6_scale(peaks).astype(np.float64) / _TRI_SCALE, 0.0, 1.0)
    return {name: scaled[:, col] for name, col in _BAND_COLUMNS}


def _mono_bands(tag: Any) -> dict[str, np.ndarray]:
    """PWAV/PWV3 heights (0..31) duplicated across all 3 band keys.

    kind="mono" declares to the client that these are single-band heights,
    not synthesised tri-band data; the values themselves are real.
    """
    heights = np.asarray(tag.get()[0], dtype=np.float64)
    scaled = np.clip(heights / _MONO_SCALE, 0.0, 1.0)
    return {name: scaled for name, _ in _BAND_COLUMNS}
