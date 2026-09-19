"""Derive vocal-cache regions from an existing 4-stem bundle (no demucs).

One demucs pass already produced ``vocals`` + accompaniment stems. Region
detection is only RMS ratio + hysteresis on those files, so a stem bundle
with no vocal-cache entry can be filled for free on CPU.

Used by ``python -m apps.vocals from-stems``.
"""

from __future__ import annotations

import hashlib
import importlib.util
import time
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import numpy as np

from apps.stems.artifacts import StemBundle, load_stem_bundle
from apps.vocals import cache as vcache

_WORKER_PATH: Path = (
    Path(__file__).resolve().parents[2] / "scripts" / "vocal_region_worker.py"
)
_worker: ModuleType | None = None


def _worker_mod() -> ModuleType:
    """Load the PEP 723 worker's pure maths without pulling torch."""
    global _worker
    if _worker is not None:
        return _worker
    spec = importlib.util.spec_from_file_location("vocal_region_worker", _WORKER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load worker module from {_WORKER_PATH}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _worker = mod
    return mod


def bundle_stem_identity(bundle: StemBundle) -> str:
    """sha256 identity of the GENERATED STEMS themselves: manifest.json's own
    bytes (model name/version, layout, source provenance, the part->file
    mapping) plus each part file's ``(part, filename, size, mtime_ns)`` from
    a ``stat()`` call.

    Distinct from ``bundle.manifest.source.sha256``, which identifies only
    the ORIGINAL track and stays fixed across every re-separation of it: a
    new demucs model, a new model version, changed separation parameters, or
    a single repaired/re-rendered part all leave ``source.sha256``
    unchanged, so a caller that keys freshness or cache-reuse on it alone
    (see :mod:`apps.lyrics.library_verdicts`) can silently keep trusting a
    verdict or a cached coverage number computed from stems that no longer
    exist on disk. This identity changes whenever any of those does.

    Deliberately audio-free: it never reads or hashes the stem audio bytes
    themselves (that would mean re-reading gigabytes of audio on every
    library scan) -- only ``manifest.json`` (small, already touched at load
    time) plus one cheap filesystem ``stat()`` per part.

    Accepted caveat: this cannot detect a part rewritten with byte-identical
    content at a preserved mtime (a deliberately adversarial case), and DOES
    force a harmless recompute for a part merely touched/copied without its
    content changing. Neither is silent data loss -- at worst an extra
    recompute, the same trust boundary every other filesystem-derived signal
    in this module already accepts.
    """
    manifest_path = next(iter(bundle.files.values())).parent / "manifest.json"
    hasher = hashlib.sha256(manifest_path.read_bytes())
    for part in bundle.parts:
        file_path = bundle.files[part]
        info = file_path.stat()
        hasher.update(
            f"\0{part}:{file_path.name}:{info.st_size}:{info.st_mtime_ns}".encode()
        )
    return hasher.hexdigest()


def stems_dir(data_dir: Path) -> Path:
    return data_dir / "state" / "stems"


def list_bundle_ids(root: Path) -> list[str]:
    """stable_ids with a loadable stem bundle, sorted."""
    if not root.is_dir():
        return []
    out: list[str] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.is_symlink():
            continue
        try:
            load_stem_bundle(child.name, stems_dir=root)
        except Exception:
            continue
        out.append(child.name)
    return out


def _read_mono(path: Path) -> tuple[np.ndarray, int]:
    """Mono float32 + sample rate. Fail fast on empty / unreadable audio.

    soundfile is lazy-imported: it ships only in the ``analysis`` extra, and a
    module-level import here would make that extra a hard base requirement for
    ``python -m apps.vocals`` and for collecting ``tests/vocals/`` at all
    (apps/vocals/cli.py imports this module at module level). Same pattern as
    scripts/vocal_region_worker.py and scripts/stem_bundle_worker.py.
    """
    import soundfile as sf

    data, sr = sf.read(str(path), always_2d=True, dtype="float32")
    if data.size == 0:
        raise RuntimeError(f"empty stem audio: {path}")
    if sr <= 0:
        raise RuntimeError(f"non-positive sample rate in {path}: {sr}")
    return data.mean(axis=1), int(sr)


def _rms_envelope(mono: np.ndarray, sr: int, hop_s: float) -> list[float]:
    hop = int(sr * hop_s)
    if hop <= 0:
        raise RuntimeError(f"hop frames <= 0 (sr={sr}, hop_s={hop_s})")
    n = len(mono) // hop
    if n == 0:
        raise RuntimeError(f"audio shorter than one hop ({hop} frames @ {sr} Hz)")
    framed = mono[: n * hop].reshape(n, hop)
    # Contract the two views directly instead of materialising ``framed *
    # framed``. This avoids allocating an intermediate square matrix.
    power = np.einsum("ij,ij->i", framed, framed, optimize=False) / hop
    return np.sqrt(power).astype(np.float64).tolist()


def derive_worker_result(bundle: StemBundle) -> dict[str, Any]:
    """Build a vocal-cache worker-result dict from a validated stem bundle."""
    w = _worker_mod()
    hop_s = float(w.HOP_S)
    monos: dict[str, np.ndarray] = {}
    sr: int | None = None
    for part in bundle.parts:
        mono, part_sr = _read_mono(bundle.files[part])
        if sr is None:
            sr = part_sr
        elif part_sr != sr:
            raise RuntimeError(f"stem sample-rate mismatch: {part}={part_sr} vs {sr}")
        monos[part] = mono
    assert sr is not None
    n = min(len(m) for m in monos.values())
    if n <= 0:
        raise RuntimeError("stem parts have zero overlapping frames")
    vocals = monos["vocals"][:n]
    mix = sum(monos[p][:n] for p in bundle.parts)

    t0 = time.perf_counter()
    v_env = _rms_envelope(vocals, sr, hop_s)
    m_env = _rms_envelope(mix, sr, hop_s)
    k = min(len(v_env), len(m_env))
    ratio = w.ratio_envelope(v_env[:k], m_env[:k], hop_s)
    duration_s = n / float(sr)
    payload = w.regions_payload(ratio, duration_s=duration_s, hop_s=hop_s)
    on_ratio = float(payload.pop("on_ratio"))
    off_ratio = float(payload.pop("off_ratio"))
    thresholds_adapted = bool(payload.pop("thresholds_adapted"))
    derive_s = time.perf_counter() - t0

    model_name = bundle.manifest.model.name
    return {
        "schema": vcache.VOCAL_CACHE_SCHEMA,
        "source": vcache.VOCAL_CACHE_SOURCE,
        "duration_s": round(duration_s, 3),
        "device": "from-stems",
        "source_sample_rate": sr,
        "analysis_sample_rate": sr,
        "params": {
            "model": model_name,
            "hop_s": hop_s,
            "on_ratio": on_ratio,
            "off_ratio": off_ratio,
            "thresholds_adapted": thresholds_adapted,
            "merge_gap_s": float(w.MERGE_GAP_S),
            "min_region_s": float(w.MIN_REGION_S),
            "confidence_gain": float(w.CONFIDENCE_GAIN),
            "derived_from_stems": True,
            # Identity of the bundle GENERATION these regions were derived
            # from -- distinct from ``duration_s`` above, which a replaced
            # bundle can trivially match by chance. ``bundle_stem_sha256`` is
            # keyed on the GENERATED STEMS (see bundle_stem_identity), not
            # merely the source track, so a re-separation with a new model,
            # version, or a repaired part is caught too, not only a fully
            # replaced bundle. A reader that trusts this cache entry as a
            # stand-in for a fresh decode (see
            # apps.lyrics.library_verdicts._cached_coverage_pct) must require
            # both fields to match the ON-DISK bundle, not just duration.
            "bundle_layout": bundle.layout,
            "bundle_stem_sha256": bundle_stem_identity(bundle),
        },
        "timings": {"derive_s": round(derive_s, 3)},
        **payload,
    }


def write_from_bundle(
    data_dir: Path,
    stable_id: str,
    audio_path: Path,
    *,
    stems_root: Path | None = None,
    stem_roots: Sequence[Path] | None = None,
) -> dict[str, Any]:
    """Derive regions from stems and publish a vocal-cache entry."""
    if stems_root is not None and stem_roots is not None:
        raise ValueError("stems_root and stem_roots are mutually exclusive")
    root = stems_root if stems_root is not None else stems_dir(data_dir)
    bundle = load_stem_bundle(stable_id, stems_dir=root, roots=stem_roots)
    result = derive_worker_result(bundle)
    return vcache.write_entry(
        vcache.cache_path(data_dir, stable_id),
        result,
        audio_path,
    )
