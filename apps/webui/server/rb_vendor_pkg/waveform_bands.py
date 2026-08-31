"""Waveform band decode: raw ANLZ tag entries -> the ``waveform`` payload bands.

Split out of ``anlz.py`` for the same reason ``waveform_native.py`` was (see its
docstring): this is band arithmetic, not ANLZ container decode, and ``anlz.py``
had reached the 600-line gate. Nothing here touches the filesystem, pyrekordbox,
or a PMAI container -- it takes an already-parsed tag (or a band dict) and does
numpy work, which is also what makes the native/Python equivalence tests cheap.

Consumers reach every name here through the ``rb_vendor`` facade.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from apps.webui.server.rb_vendor_pkg.waveform_native import _WAVEFORM_NATIVE

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


def _mono_bands(tag: Any) -> dict[str, np.ndarray]:
    """PWAV/PWV3 heights (0..31) duplicated across all 3 band keys.

    kind="mono" declares to the client that these are single-band heights,
    not synthesised tri-band data; the values themselves are real.
    """
    heights = np.asarray(tag.get()[0], dtype=np.float64)
    scaled = np.clip(heights / _MONO_SCALE, 0.0, 1.0)
    return {name: scaled for name, _ in _BAND_COLUMNS}
