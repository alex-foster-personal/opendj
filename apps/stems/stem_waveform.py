"""Mono peak envelopes for performance stem mini-waveforms (issue #1036).

Decodes each validated stem part once via the shared ffmpeg peak path, downsamples
to a fixed point count, and caches JSON under ``(private path)/``.
Never buffers whole PCM in the HTTP handler beyond what decode already streams.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import numpy as np

from apps.analysis_waveform.bands import _downsample_max
from apps.analysis_waveform.decode import LocalDecodeUnavailable, decode_peaks
from apps.stems.artifacts import StemArtifactError, StemBundle, StemPart, load_stem_bundle
from apps.stems.bundle_lock import PUBLISH_DIR_NAME, beside_stem_root

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


class StemPartChangedError(StemArtifactError):
    """A part was replaced while its envelope decoded, so the envelope is not that file's."""


class StemPinCleanupError(RuntimeError):
    """A waveform pin directory could not be removed; it is left for :func:`sweep_stale_waveform_pins`.

    Not a :class:`StemArtifactError`: the bundle is fine, this machine's
    disk is not, so the route answers 500 rather than a missing part.
    """

    def __init__(self, scratch: Path, cause: OSError) -> None:
        super().__init__(f"could not remove the waveform pin directory {scratch}: {cause}")
        self.scratch = scratch


# A pin lives only for one envelope decode (seconds), so one this old was
# left by a removal that failed, or by a process that died mid-decode.
WAVEFORM_PIN_STALE_S: float = 3600.0
_PIN_PREFIX = "waveform-pin-"


def remove_waveform_pins(scratch: Path) -> None:
    """Remove one request's pin directory, raising :class:`StemPinCleanupError` if any of it stays.

    A link left behind keeps the old stem bytes on disk, so a failed removal
    is reported by name rather than ignored (Sol on #6820); the directory
    is retried by the next pin's :func:`sweep_stale_waveform_pins`.
    """
    try:
        shutil.rmtree(scratch)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise StemPinCleanupError(scratch, exc) from exc


def sweep_stale_waveform_pins(scratch_root: Path, *, now: float | None = None) -> list[Path]:
    """Remove pin directories older than ``WAVEFORM_PIN_STALE_S``; return the ones that stay."""
    moment = time.time() if now is None else now
    left: list[Path] = []
    for child in scratch_root.glob(f"{_PIN_PREFIX}*"):
        try:
            if moment - child.stat().st_mtime < WAVEFORM_PIN_STALE_S:
                continue
            shutil.rmtree(child)
        except FileNotFoundError:
            continue
        except OSError:
            left.append(child)
    return left


def _file_fingerprint(path: Path, stat: os.stat_result | None = None) -> str:
    """The cache key of one input: its path, device and inode, mtime and size.

    Every bundle writer swaps a whole directory in, so a re-render puts a
    new inode at the path even when a coarse clock (1-2 s on HFS+ or FAT)
    leaves its size and mtime as they were; keyed on those alone it would
    be served the old render's envelope (Sol on #6820). ctime is left out:
    pinning the input with a hard link (pin_waveform_inputs) moves it on
    every request without changing a byte.
    """
    stat = path.stat() if stat is None else stat
    key = f"{path}:{stat.st_dev}:{stat.st_ino}:{stat.st_mtime_ns}:{stat.st_size}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _identity(stat: os.stat_result) -> tuple[int, int, int, int]:
    return (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size)


def _require_unchanged(path: Path, before: os.stat_result) -> None:
    """Raise when ``path`` is no longer the file ``before`` described.

    A bundle swap can land after the route resolves the bundle and before or
    during the decode, which then reads the replacement. Its envelope must
    not be served or cached under the file it replaced (Codex on #6820).
    """
    try:
        after = path.stat()
    except FileNotFoundError as exc:
        raise StemPartChangedError(f"{path} was replaced while its envelope decoded") from exc
    if _identity(after) != _identity(before):
        raise StemPartChangedError(f"{path} was replaced while its envelope decoded")


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


def _instrumental_fingerprint(
    bundle: StemBundle, bass_stat: os.stat_result, other_stat: os.stat_result
) -> str:
    bass = bundle.files["bass"]
    other = bundle.files["other"]
    return hashlib.sha256(
        (
            _file_fingerprint(bass, bass_stat)
            + ":"
            + _file_fingerprint(other, other_stat)
            + ":instrumental"
        ).encode()
    ).hexdigest()[:16]


def waveform_inputs(bundle: StemBundle, part: str) -> tuple[Path, ...]:
    """The files the envelope for ``part`` decodes, in decode order."""
    if part == _INSTRUMENTAL_PART and bundle.layout == "demucs4":
        _bundle_part_path(bundle, part)  # raises when bass or other is missing
        return (bundle.files["bass"], bundle.files["other"])
    return (_bundle_part_path(bundle, part),)


def pin_waveform_inputs(
    bundle: StemBundle, part: str
) -> tuple[Path | None, dict[Path, Path], dict[Path, os.stat_result]]:
    """Hard-link the files a ``part`` envelope decodes, so a later swap cannot change them.

    Call it under the read barrier, then decode outside it: a bundle swap
    renames the canonical paths away, but a hard link keeps naming the
    inode the barrier saw, so the decode reads exactly the resolved version
    and an instrumental envelope never mixes two (Codex on #6820). The links
    sit in a fresh directory under ``.stem-publish/`` beside the resolved
    stem root, the filesystem the parts live on. Returns that directory,
    which the caller removes, the canonical-to-link map, and every input's
    stat as the barrier saw it.

    Pinning is all or nothing: if any input cannot be linked (a read-only
    or link-less volume), no input is pinned, so a pinned old bass can never
    be decoded beside an unpinned new other (Codex on #6820). The builder
    then checks every input against the stat recorded here instead.
    """
    inputs = waveform_inputs(bundle, part)
    seen = {source: source.stat() for source in inputs}
    scratch, pinned = pin_files(inputs)
    return scratch, pinned, seen


def pin_files(inputs: Sequence[Path]) -> tuple[Path | None, dict[Path, Path]]:
    """Hard-link every one of ``inputs`` (parts of one bundle) into a fresh pin directory.

    Returns that directory, which the caller removes with
    :func:`remove_waveform_pins`, and the canonical-to-link map; or
    ``(None, {})`` when any input cannot be linked, so nothing is ever
    half pinned. The directory sits under ``.stem-publish/`` beside the
    resolved stem root, on the parts' own filesystem.
    """
    scratch_root = beside_stem_root(inputs[0].parent.parent) / PUBLISH_DIR_NAME
    try:
        scratch_root.mkdir(parents=True, exist_ok=True)
        scratch = Path(tempfile.mkdtemp(prefix=_PIN_PREFIX, dir=scratch_root))
    except OSError:
        return None, {}
    sweep_stale_waveform_pins(scratch_root)
    pinned: dict[Path, Path] = {}
    for index, source in enumerate(inputs):
        target = scratch / f"{index}-{source.name}"
        try:
            os.link(source, target)
        except OSError:
            remove_waveform_pins(scratch)
            return None, {}
        pinned[source] = target
    return scratch, pinned


def build_stem_waveform_payload(
    bundle: StemBundle,
    part: str,
    *,
    cache_root: Path | None = None,
    pinned: dict[Path, Path] | None = None,
    seen: dict[Path, os.stat_result] | None = None,
) -> dict[str, Any]:
    """Return the JSON envelope contract for one bundle part.

    ``pinned`` maps a canonical part path to a hard link of it from
    :func:`pin_waveform_inputs`; the decode reads the link, the cache key
    keeps the canonical path. ``seen`` holds each input's stat from under
    the read barrier: a file that no longer matches it was swapped after
    the bundle resolved, and the decode is refused before it starts.
    """
    pins = pinned or {}
    resolved = seen or {}

    def source(path: Path) -> Path:
        return pins.get(path, path)

    def stat_as_resolved(path: Path) -> os.stat_result:
        current = source(path).stat()
        before = resolved.get(path)
        if before is not None and _identity(current) != _identity(before):
            raise StemPartChangedError(f"{path} was replaced after its bundle resolved")
        return current

    if part == _INSTRUMENTAL_PART and bundle.layout == "demucs4":
        bass_stat = stat_as_resolved(bundle.files["bass"])
        other_stat = stat_as_resolved(bundle.files["other"])
        bass, other = source(bundle.files["bass"]), source(bundle.files["other"])
        fingerprint = _instrumental_fingerprint(bundle, bass_stat, other_stat)
        cache_path = (
            _cache_path(cache_root, bundle.manifest.stable_id, part, fingerprint)
            if cache_root is not None
            else None
        )
        if cache_path is not None:
            cached = _read_cache(cache_path)
            if cached is not None:
                return cached
        bass_env = _decode_part_envelope(bass)
        other_env = _decode_part_envelope(other)
        # Both checks follow both decodes, so a swap between them cannot mix
        # one version's bass with the next version's other.
        _require_unchanged(bass, bass_stat)
        _require_unchanged(other, other_stat)
        envelope = _merge_envelopes(bass_env, other_env)
    else:
        path = _bundle_part_path(bundle, part)
        decoded = source(path)
        part_stat = stat_as_resolved(path)
        fingerprint = _file_fingerprint(path, part_stat)
        cache_path = (
            _cache_path(cache_root, bundle.manifest.stable_id, part, fingerprint)
            if cache_root is not None
            else None
        )
        if cache_path is not None:
            cached = _read_cache(cache_path)
            if cached is not None:
                return cached
        envelope = _decode_part_envelope(decoded)
        _require_unchanged(decoded, part_stat)

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
    "WAVEFORM_PIN_STALE_S",
    "StemPinCleanupError",
    "build_stem_waveform_payload",
    "load_stem_waveform_payload",
    "pin_files",
    "remove_waveform_pins",
    "sweep_stale_waveform_pins",
]
