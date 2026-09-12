"""Retained Beat This! framewise logits for the v2 dynamic-grid fitter.

This module loads activations and refuses when they are missing. The piecewise-
constant tempo map fit lives in ``tempo_map.py``; this module does not fit a
grid or synthesize tempo from beat times alone.
"""
from __future__ import annotations

import base64
import hashlib
import io
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

FPS = 50

ACTIVATIONS_DIR_ENV = "MDT_BEATGRID_ACTIVATIONS_DIR"


class ActivationsMissing(RuntimeError):
    """Dynamic grid fit refused because activations are missing or unreadable."""


def activation_npz_name(audio_path: str) -> str:
    """Digest-in-filename rule; keep in lockstep with `beat_this_runner.analyze_one`."""
    stem = os.path.splitext(os.path.basename(audio_path))[0]
    tag = hashlib.sha256(os.path.abspath(audio_path).encode("utf-8")).hexdigest()[:12]
    return f"{stem}.{tag}.npz"


def default_activations_dir(*, create: bool = True) -> Path:
    """Durable activations directory under ``DATA_DIR`` or ``MDT_BEATGRID_ACTIVATIONS_DIR``."""
    override = os.environ.get(ACTIVATIONS_DIR_ENV, "").strip()
    if override:
        path = Path(override)
        if not path.is_dir():
            if path.exists():
                raise ActivationsMissing(
                    f"{ACTIVATIONS_DIR_ENV}={path} is not a directory; this producer "
                    "does not fall back when an explicit override is unusable"
                )
            if not create:
                raise ActivationsMissing(
                    f"{ACTIVATIONS_DIR_ENV}={path} does not exist and create=False"
                )
            path.mkdir(parents=True, exist_ok=True)
        return path
    from apps.shared.platform_paths import DATA_DIR

    path = DATA_DIR / "analysis" / "beatgrid" / "activations"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def load_activations(
    *,
    npz: str | None = None,
    blob: bytes | str | None = None,
) -> dict[str, Any]:
    """Open retained logits from exactly one of ``npz`` path or ``blob`` bytes."""
    if (npz is None) == (blob is None):
        raise ValueError("exactly one of npz or blob must be provided")

    if blob is not None:
        raw = base64.b64decode(blob) if isinstance(blob, str) else blob
        with np.load(io.BytesIO(raw), allow_pickle=False) as archive:
            return _arrays_from_archive(archive)

    path = Path(npz)
    if not path.is_file():
        raise ActivationsMissing(
            f"dynamic grid fit refused: activations npz not found at {npz!r}"
        )
    with np.load(path, allow_pickle=False) as archive:
        return _arrays_from_archive(archive)


def _arrays_from_archive(archive: np.lib.npyio.NpzFile) -> dict[str, Any]:
    for key in ("beat", "downbeat", "fps"):
        if key not in archive:
            raise ActivationsMissing(
                f"dynamic grid fit refused: activations npz missing required key {key!r}"
            )
    fps = int(archive["fps"])
    if fps <= 0:
        raise ActivationsMissing(
            f"dynamic grid fit refused: activations fps {fps!r} is not a positive rate"
        )
    return {
        "beat": np.asarray(archive["beat"]),
        "downbeat": np.asarray(archive["downbeat"]),
        "fps": fps,
    }


def activations_ref(record: Any) -> Mapping[str, Any] | None:
    """Read ``features_blob['activations']``, else ok-lane ``payload['activations']``."""
    if isinstance(record, Mapping) and ("npz" in record or "blob" in record):
        return record

    features_blob = getattr(record, "features_blob", None)
    if isinstance(features_blob, Mapping):
        ref = features_blob.get("activations")
        if isinstance(ref, Mapping) and ("npz" in ref or "blob" in ref):
            return ref

    lanes = getattr(record, "lanes", None)
    if isinstance(lanes, Mapping):
        lane = lanes.get("beatgrid")
        if lane is not None:
            payload = getattr(lane, "payload", None)
            if isinstance(payload, Mapping):
                ref = payload.get("activations")
                if isinstance(ref, Mapping) and ("npz" in ref or "blob" in ref):
                    return ref
    return None


def require_activations_for_fit(record_or_ref: Any) -> dict[str, Any]:
    """Load retained activations or raise ``ActivationsMissing``; never invent a grid."""
    ref = activations_ref(record_or_ref)
    if ref is None:
        raise ActivationsMissing(
            "dynamic grid fit refused: record carries no activations pointer"
        )
    npz_path = ref.get("npz")
    blob = ref.get("blob")
    if npz_path is not None:
        return load_activations(npz=str(npz_path))
    if blob is not None:
        return load_activations(blob=blob)
    raise ActivationsMissing(
        "dynamic grid fit refused: activations pointer has neither npz nor blob"
    )


def fit_dynamic_grid(record_or_ref: Any, beat_times: Sequence[float] | None = None):
    """Entry for the v2 fitter; lazy re-export from ``tempo_map``."""
    from apps.analysis_beatgrid.tempo_map import fit_dynamic_grid as _fit

    return _fit(record_or_ref, beat_times=beat_times)


__all__ = [
    "ACTIVATIONS_DIR_ENV",
    "ActivationsMissing",
    "FPS",
    "activation_npz_name",
    "activations_ref",
    "default_activations_dir",
    "fit_dynamic_grid",
    "load_activations",
    "require_activations_for_fit",
]
