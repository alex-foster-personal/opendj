"""Vocal-cache contract: data/state/vocal-cache/{stable_id}.json.

Single source of truth for the demucs vocal-region cache shape
(SPIKE-SUMMARY section 3, SPIKE-B2 section 6.3). Both writers
(apps.vocals CLI) and readers (apps.webui.server.rb_vendor /anlz merge)
go through this module so the contract cannot fork. stdlib only - the
webui server imports this and must not pull torch.

Entry shape (schema 1):

    { "schema": 1,
      "source": "demucs-htdemucs",
      "confidence": 0.84,                  # scalar = max region confidence
      "audio_mtime": 1714154080.33,        # exact st_mtime of the audio file
      "computed_at": "2026-07-22T10:00:00Z",
      "fps": 2.0,                          # envelope rate (1 / hop_s)
      "duration_s": 230.4,
      "coverage_pct": 62.8,
      "regions": [ {"start_s": 8.3, "end_s": 57.9,
                    "confidence": 0.84, "intensity": 3} ],
      "params": {...}, "worker": {...},
      "preset": {"tag": "htdemucs-ov0.1", "model": "htdemucs",
                 "overlap": 0.1, "shifts": 0, "rung": 3} }

``preset`` is OPTIONAL and additive (entries written before it exist
without it). It records WHICH separation config produced the regions so a
later quality upgrade can re-run selectively instead of re-running the
whole library. It is deliberately not folded into ``source``: that field
is a contract constant every reader uses, and widening it would
invalidate existing entries.

``intensity`` (1..4) is derived from confidence at write time so the
/anlz merge emits the exact region shape PVDI regions use and the
frontend renders both identically (confidence -> bar opacity via the
existing intensity ramp).

Invalidation: an entry is served only while the audio file exists and
its st_mtime equals ``audio_mtime`` exactly (same equality rule as the
anlz-cache). Stale/absent -> None (a real "not analyzed" state);
corrupt JSON or missing fields -> raise (fail fast, never guess).
"""
from __future__ import annotations

import json
import math
import os
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

VOCAL_CACHE_SCHEMA: int = 2
VOCAL_CACHE_SOURCE: str = "demucs-htdemucs"
VOCAL_CACHE_DIRNAME: str = "vocal-cache"

_REQUIRED_ENTRY_FIELDS: tuple[str, ...] = (
    "schema", "source", "confidence", "audio_mtime", "computed_at",
    "audio_signature", "fps", "duration_s", "coverage_pct", "regions",
    "params", "worker",
)
_REQUIRED_WORKER_FIELDS: tuple[str, ...] = (
    "schema", "source", "fps", "regions", "coverage_pct", "params",
    "duration_s",
)


def _is_finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def cache_dir(data_dir: Path) -> Path:
    return data_dir / "state" / VOCAL_CACHE_DIRNAME


def cache_path(data_dir: Path, stable_id: str) -> Path:
    root = cache_dir(data_dir).resolve()
    path = (root / f"{stable_id}.json").resolve()
    if path.parent != root:
        raise ValueError(f"stable_id escapes vocal-cache directory: {stable_id!r}")
    return path


def audio_signature(path: Path) -> dict[str, int]:
    """Identity token for one source-file generation.

    mtime alone cannot detect a replacement that preserves its timestamp.
    Device, inode, size, and nanosecond mtime make that cache-poisoning
    route explicit and fail closed on any observed source-file swap.
    """
    stat = path.stat()
    return {
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


# ``device`` is a MOUNT-scoped id, not a file identity: macOS hands a volume a
# fresh st_dev on every remount, so an untouched file on an external/iCloud
# volume changes device while inode/size/mtime_ns all stay put. Comparing it
# can invalidate untouched entries after a remount. It is still WRITTEN
# (diagnostics, and it is a required
# contract field) but deliberately not COMPARED. The swap-detection the field
# was added for is already carried by inode + size + mtime_ns, none of which a
# remount touches.
_SIGNATURE_COMPARED_FIELDS: tuple[str, ...] = ("inode", "size", "mtime_ns")


def signature_matches(recorded: Mapping[str, Any], observed: Mapping[str, Any]) -> bool:
    """True when two audio signatures describe the same file generation.

    Fail-fast on a malformed signature rather than silently comparing a
    subset: a missing compared field is a contract breach, not a mismatch.
    """
    for field in _SIGNATURE_COMPARED_FIELDS:
        if field not in recorded or field not in observed:
            raise ValueError(f"audio signature missing compared field {field!r}")
        if recorded[field] != observed[field]:
            return False
    return True


def _validate_entry(entry: Any, path: Path) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise ValueError(f"vocal-cache entry {path} must be a JSON object")
    if "schema" not in entry:
        raise ValueError(f"vocal-cache entry {path} missing fields: ['schema']")
    if entry["schema"] != VOCAL_CACHE_SCHEMA:
        return entry
    missing = [f for f in _REQUIRED_ENTRY_FIELDS if f not in entry]
    if missing:
        raise ValueError(f"vocal-cache entry {path} missing fields: {missing}")
    if entry["source"] != VOCAL_CACHE_SOURCE:
        raise ValueError(
            f"vocal-cache entry {path} has unknown source {entry['source']!r}"
        )
    if not _is_finite_number(entry["confidence"]) or not 0 <= entry["confidence"] <= 1:
        raise ValueError(f"vocal-cache entry {path} has invalid confidence")
    if not _is_finite_number(entry["audio_mtime"]):
        raise ValueError(f"vocal-cache entry {path} has invalid audio_mtime")
    computed_at = entry["computed_at"]
    if not isinstance(computed_at, str):
        raise ValueError(f"vocal-cache entry {path} has invalid computed_at")
    try:
        parsed_computed_at = datetime.fromisoformat(computed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"vocal-cache entry {path} has invalid computed_at"
        ) from exc
    if parsed_computed_at.tzinfo is None:
        raise ValueError(f"vocal-cache entry {path} has invalid computed_at")
    signature = entry["audio_signature"]
    if not isinstance(signature, Mapping) or set(signature) != {
        "device", "inode", "size", "mtime_ns"
    } or not all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0
        for value in signature.values()
    ):
        raise ValueError(f"vocal-cache entry {path} has invalid audio_signature")
    if not _is_finite_number(entry["fps"]) or entry["fps"] <= 0:
        raise ValueError(f"vocal-cache entry {path} has invalid fps")
    if not _is_finite_number(entry["duration_s"]) or entry["duration_s"] <= 0:
        raise ValueError(f"vocal-cache entry {path} has invalid duration_s")
    if (
        not _is_finite_number(entry["coverage_pct"])
        or not 0 <= entry["coverage_pct"] <= 100
    ):
        raise ValueError(f"vocal-cache entry {path} has invalid coverage_pct")
    if not isinstance(entry["params"], Mapping):
        raise ValueError(f"vocal-cache entry {path} has invalid params")
    if not isinstance(entry["worker"], Mapping):
        raise ValueError(f"vocal-cache entry {path} has invalid worker")
    if not isinstance(entry["regions"], list):
        raise ValueError(f"vocal-cache entry {path} has invalid regions")
    for index, region in enumerate(entry["regions"]):
        if not isinstance(region, Mapping) or set(region) != {
            "start_s", "end_s", "confidence", "intensity"
        }:
            raise ValueError(f"vocal-cache entry {path} has invalid region {index}")
        start_s, end_s, confidence, intensity = (
            region["start_s"], region["end_s"], region["confidence"], region["intensity"]
        )
        if (
            not _is_finite_number(start_s)
            or not _is_finite_number(end_s)
            or not 0 <= start_s < end_s <= entry["duration_s"]
            or not _is_finite_number(confidence)
            or not 0 <= confidence <= 1
            or not isinstance(intensity, int)
            or isinstance(intensity, bool)
            or not 1 <= intensity <= 4
            or intensity != intensity_of(float(confidence))
        ):
            raise ValueError(f"vocal-cache entry {path} has invalid region {index}")
    expected_confidence = max(
        (region["confidence"] for region in entry["regions"]), default=0.0,
    )
    if entry["confidence"] != expected_confidence:
        raise ValueError(f"vocal-cache entry {path} has invalid confidence")
    return entry


def intensity_of(confidence: float) -> int:
    """Map worker confidence (0..1) onto the PVDI intensity ramp (1..4)."""
    if not 0.0 <= confidence <= 1.0:
        raise ValueError(f"confidence out of range [0, 1]: {confidence}")
    return max(1, min(4, round(confidence * 4)))


def load_valid_entry(path: Path, audio_path: Path | None) -> dict[str, Any] | None:
    """Return the parsed entry when it is VALID for the given audio file.

    None means "no usable cache" (absent, schema-bumped, audio gone, or
    audio_mtime changed) - a real state the caller reports as todo /
    not_analyzed. Corrupt JSON or a missing contract field raises: that
    is a bug, not a cache miss.
    """
    if not path.is_file():
        return None
    entry = _validate_entry(json.loads(path.read_text(encoding="utf-8")), path)
    if entry["schema"] != VOCAL_CACHE_SCHEMA:
        return None  # old schema self-heals by recompute, never by guessing
    if audio_path is None or not audio_path.is_file():
        return None  # cannot validate mtime -> never serve unverifiable regions
    if not signature_matches(entry["audio_signature"], audio_signature(audio_path)):
        return None  # audio changed -> regions are for a different file
    return entry


#----- publication helpers

def _existing_stem_upload(path: Path, signature: dict[str, int]) -> Any | None:
    """Return the R2 mapping only when it belongs to this audio generation."""
    if not path.is_file():
        return None
    previous = _validate_entry(json.loads(path.read_text(encoding="utf-8")), path)
    if not signature_matches(previous["audio_signature"], signature):
        return None
    return previous["worker"].get("stem_upload")


def write_entry(
    path: Path,
    worker_result: dict[str, Any],
    audio_path: Path,
    audio_mtime: float | None = None,
    source_signature: dict[str, int] | None = None,
    preset: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Wrap a worker JSON result into a cache entry and write it atomically.

    ``audio_mtime`` should be the st_mtime captured BEFORE the worker ran
    (the file the regions were actually computed from); if the audio is
    replaced mid-analysis the recorded mtime then mismatches the new file
    and the entry self-invalidates instead of serving stale regions.
    None falls back to stat-now (hermetic tests where no race exists).

    ``preset`` stamps the separation config that produced the regions
    (see the module docstring). None omits the key entirely rather than
    writing a placeholder, so "unstamped" stays distinguishable from
    "stamped as the default".
    """
    missing = [f for f in _REQUIRED_WORKER_FIELDS if f not in worker_result]
    if missing:
        raise ValueError(f"worker result missing fields: {missing}")
    if worker_result["schema"] != VOCAL_CACHE_SCHEMA:
        raise ValueError(
            f"worker schema {worker_result['schema']} != {VOCAL_CACHE_SCHEMA}"
        )
    if worker_result["source"] != VOCAL_CACHE_SOURCE:
        raise ValueError(
            f"worker source {worker_result['source']!r} != {VOCAL_CACHE_SOURCE!r}"
        )
    regions = [
        {
            "start_s": r["start_s"],
            "end_s": r["end_s"],
            "confidence": r["confidence"],
            "intensity": intensity_of(r["confidence"]),
        }
        for r in worker_result["regions"]
    ]
    current_signature = audio_signature(audio_path)
    if source_signature is not None and current_signature != source_signature:
        raise RuntimeError(
            f"audio file changed before cache publication: {audio_path}; refusing cache write"
        )
    signature = source_signature if source_signature is not None else current_signature
    prior_stem_upload = _existing_stem_upload(path, signature)
    entry: dict[str, Any] = {
        "schema": VOCAL_CACHE_SCHEMA,
        "source": VOCAL_CACHE_SOURCE,
        "confidence": max((r["confidence"] for r in regions), default=0.0),
        "audio_mtime": audio_mtime if audio_mtime is not None else audio_path.stat().st_mtime,
        "audio_signature": signature,
        "computed_at": datetime.now(UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "fps": worker_result["fps"],
        "duration_s": worker_result["duration_s"],
        "coverage_pct": worker_result["coverage_pct"],
        "regions": regions,
        "params": worker_result["params"],
        "worker": {
            key: worker_result[key]
            for key in (
                "device", "timings", "source_sample_rate", "analysis_sample_rate",
                # Container-stamped [start, end] epoch pair. Without it the only
                # clock on disk is this file's mtime, which is PUBLICATION time,
                # and starmap yields in input order - so one slow call bunches
                # every fast call behind it and observed peak concurrency cannot
                # be reconstructed. See scripts/bench/kpi_derive.py.
                "wall_span",
                # Returned by the R2 destination: the content-addressed object
                # keys for the stem parts and the manifest, the ONLY durable
                # link from this stable_id to those opaque keys. Present only
                # when stems_dest == r2, so local/none runs are unchanged.
                "stem_upload",
            )
            if key in worker_result
        },
    }
    if "stem_upload" not in entry["worker"] and prior_stem_upload is not None:
        entry["worker"]["stem_upload"] = prior_stem_upload
    if preset is not None:
        if not isinstance(preset, Mapping) or not preset:
            raise ValueError(f"preset must be a non-empty mapping, got {preset!r}")
        entry["preset"] = dict(preset)
    _validate_entry(entry, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(entry, fh, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return entry


def anlz_vocals_of(entry: dict[str, Any]) -> dict[str, Any]:
    """The /anlz ``vocals`` field for a valid cache entry - the FOURTH
    status. Region shape matches PVDI regions exactly (start_s / end_s /
    intensity, plus confidence provenance) so renderers need no new code."""
    return {
        "status": "demucs",
        "fps": entry["fps"],
        "regions": [
            {
                "start_s": r["start_s"],
                "end_s": r["end_s"],
                "intensity": r["intensity"],
                "confidence": r["confidence"],
            }
            for r in entry["regions"]
        ],
    }
