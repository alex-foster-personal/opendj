"""Mono peak envelopes for performance stem mini-waveforms (issue #1036).

Decodes each validated stem part once via the shared ffmpeg peak path, downsamples
to a fixed point count, and caches JSON under ``data/state/stem-waveform-cache/``.
Never buffers whole PCM in the HTTP handler beyond what decode already streams.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, cast

import numpy as np

from apps.analysis_waveform.bands import _downsample_max
from apps.analysis_waveform.decode import LocalDecodeUnavailable, decode_peaks
from apps.stems.artifacts import StemArtifactError, StemBundle, StemPart, load_stem_bundle

STEM_WAVEFORM_POINTS: int = 512
STEM_WAVEFORM_SCHEMA: int = 1
_CACHE_DIR_NAME = "stem-waveform-cache"
_WRITE_LOCK = threading.Lock()

# Virtual bundle part: demucs4 instrumental control maps to max(bass, other).
_INSTRUMENTAL_PART = "instrumental"


def _normalize_peaks(peaks: np.ndarray) -> list[float]:
    """Mono peaks in ``0..1``, length ``STEM_WAVEFORM_POINTS``."""
    mono = peaks.max(axis=1) if peaks.ndim == 2 else peaks.reshape(-1)
    down = _downsample_max(mono.astype(np.float64), STEM_WAVEFORM_POINTS)
    peak = float(down.max()) if down.size else 0.0
    if peak <= 0:
        return [0.0] * STEM_WAVEFORM_POINTS
    scaled = (down / peak).clip(0.0, 1.0)
    values = [round(float(v), 6) for v in scaled.tolist()]
    if len(values) < STEM_WAVEFORM_POINTS:
        values.extend([0.0] * (STEM_WAVEFORM_POINTS - len(values)))
    return values[:STEM_WAVEFORM_POINTS]


def _file_fingerprint(path: Path) -> str:
    stat = path.stat()
    return hashlib.sha256(f"{path}:{stat.st_mtime_ns}:{stat.st_size}".encode()).hexdigest()[:16]


def _cache_path(cache_root: Path, stable_id: str, part: str, fingerprint: str) -> Path:
    safe_id = stable_id.replace("/", "_")
    return cache_root / safe_id / f"{part}-{fingerprint}.json"


def _read_cache(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    envelope = payload.get("envelope")
    if not isinstance(envelope, list):
        return None
    return payload


def _write_cache_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        delete=False,
    ) as tmp:
        tmp.write(text)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def _decode_part_envelope(path: Path) -> list[float]:
    try:
        peaks = decode_peaks(path)
    except LocalDecodeUnavailable as exc:
        raise StemArtifactError(str(exc)) from exc
    return _normalize_peaks(peaks)


def _merge_envelopes(left: list[float], right: list[float]) -> list[float]:
    n = max(len(left), len(right))
    out: list[float] = []
    for i in range(n):
        lv = left[i] if i < len(left) else 0.0
        rv = right[i] if i < len(right) else 0.0
        out.append(max(lv, rv))
    return out


def _bundle_part_path(bundle: StemBundle, part: str) -> Path:
    if part == _INSTRUMENTAL_PART:
        if bundle.layout != "demucs4":
            if part in bundle.parts:
                return bundle.files[cast(StemPart, part)]
            raise StemArtifactError(
                f"unknown stem part {part!r} for a {bundle.layout} bundle; it has {bundle.parts}"
            )
        bass = bundle.files.get("bass")
        other = bundle.files.get("other")
        if bass is None or other is None:
            raise StemArtifactError(
                f"{bundle.layout} bundle missing bass/other for instrumental envelope"
            )
        return bass  # fingerprint uses both files below
    if part not in bundle.parts:
        raise StemArtifactError(
            f"unknown stem part {part!r} for a {bundle.layout} bundle; it has {bundle.parts}"
        )
    return bundle.files[cast(StemPart, part)]


def _instrumental_fingerprint(bundle: StemBundle) -> str:
    bass = bundle.files["bass"]
    other = bundle.files["other"]
    return hashlib.sha256(
        (
            _file_fingerprint(bass)
            + ":"
            + _file_fingerprint(other)
            + ":instrumental"
        ).encode()
    ).hexdigest()[:16]


def build_stem_waveform_payload(
    bundle: StemBundle,
    part: str,
    *,
    cache_root: Path | None = None,
) -> dict[str, Any]:
    """Return the JSON envelope contract for one bundle part."""
    if part == _INSTRUMENTAL_PART and bundle.layout == "demucs4":
        fingerprint = _instrumental_fingerprint(bundle)
        cache_path = (
            _cache_path(cache_root, bundle.manifest.stable_id, part, fingerprint)
            if cache_root is not None
            else None
        )
        if cache_path is not None:
            cached = _read_cache(cache_path)
            if cached is not None:
                return cached
        bass_env = _decode_part_envelope(bundle.files["bass"])
        other_env = _decode_part_envelope(bundle.files["other"])
        envelope = _merge_envelopes(bass_env, other_env)
    else:
        path = _bundle_part_path(bundle, part)
        fingerprint = _file_fingerprint(path)
        cache_path = (
            _cache_path(cache_root, bundle.manifest.stable_id, part, fingerprint)
            if cache_root is not None
            else None
        )
        if cache_path is not None:
            cached = _read_cache(cache_path)
            if cached is not None:
                return cached
        envelope = _decode_part_envelope(path)

    payload = {
        "schema": STEM_WAVEFORM_SCHEMA,
        "stable_id": bundle.manifest.stable_id,
        "part": part,
        "layout": bundle.layout,
        "points": STEM_WAVEFORM_POINTS,
        "envelope": envelope,
    }
    if cache_path is not None:
        with _WRITE_LOCK:
            _write_cache_atomic(cache_path, payload)
    return payload


def load_stem_waveform_payload(
    stable_id: str,
    part: str,
    *,
    stems_dir: Path,
    cache_root: Path,
    roots: tuple[Path, ...] | None = None,
) -> dict[str, Any]:
    """Load bundle and return cached or freshly decoded envelope JSON."""
    bundle = load_stem_bundle(stable_id, stems_dir=stems_dir, roots=roots)
    return build_stem_waveform_payload(bundle, part, cache_root=cache_root)


__all__ = [
    "STEM_WAVEFORM_POINTS",
    "STEM_WAVEFORM_SCHEMA",
    "build_stem_waveform_payload",
    "load_stem_waveform_payload",
]
