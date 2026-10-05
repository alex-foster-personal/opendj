"""Shared rig for the stem cache budget tests (STEM-39 .. STEM-43).

Builds strict-loadable stem bundles on disk and injects the disk measurement,
so no test asserts on whatever the machine happened to have free that day.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import wave
from pathlib import Path
from typing import Any

from apps.cloud import stem_cache_budget as budget
from apps.cloud.stem_cache_budget import GIB, DiskUsage, EnforceReport

VOLUME_BYTES: int = 460 * GIB
#: 5% of a 460 GiB volume, which is larger than the 20 GiB absolute floor.
FLOOR_BYTES: int = 23 * GIB


def wav_bytes(*, frames: int = 8, seed: int = 0) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(44_100)
        out.writeframes(bytes([seed % 256, 0]) * frames * 2)
    return buf.getvalue()


def make_bundle(stems_dir: Path, stable_id: str, *, atime: float) -> dict[str, str]:
    """Write one strict-loadable bundle and return its {filename: sha256}
    entry, i.e. exactly what the push rail would have journaled for it."""
    bundle_dir = stems_dir / stable_id
    bundle_dir.mkdir(parents=True)
    manifest = {
        "schema_version": 1,
        "stable_id": stable_id,
        "model": {"name": "htdemucs", "version": "4.0.1"},
        "source": {"path": "/music/x.wav", "sha256": "a" * 64},
        "files": {p: f"{p}.wav" for p in ("vocals", "drums", "bass", "other")},
    }
    files = {"manifest.json": (json.dumps(manifest) + "\n").encode("utf-8")}
    for part in ("vocals", "drums", "bass", "other"):
        files[f"{part}.wav"] = wav_bytes()
    entry: dict[str, str] = {}
    for filename, body in files.items():
        path = bundle_dir / filename
        path.write_bytes(body)
        os.utime(path, (atime, atime))
        entry[filename] = hashlib.sha256(body).hexdigest()
    return entry


def bundle_bytes(stems_dir: Path, stable_id: str) -> int:
    return sum(f.stat().st_size for f in (stems_dir / stable_id).iterdir())


def disk_usage(*, free: int) -> DiskUsage:
    return DiskUsage(total_bytes=VOLUME_BYTES, free_bytes=free)


def enforce(stems_dir: Path, data_dir: Path, **kwargs: Any) -> EnforceReport:
    kwargs.setdefault("protected", frozenset())
    kwargs.setdefault("can_rehydrate", True)
    return budget.enforce(stems_dir, data_dir=data_dir, **kwargs)
