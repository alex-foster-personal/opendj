"""manifest.json read/write helpers for a set session.

Written by :mod:`apps.sets.record` at stop-time; consumed by
:mod:`apps.sets.sessions` and :mod:`apps.sets.api` for session discovery.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .watermark import PRIVACY_NOTE_DEFAULT


@dataclass
class AudioSegment:
    """One rolled MP3 file under the session dir."""

    name: str
    start_t_s: float
    duration_s: float | None
    size_bytes: int


@dataclass
class TrackFile:
    """One per-track WAV from :mod:`apps.sets.split_tracks`."""

    name: str
    path: str
    start_t_s: float
    duration_s: float
    size_bytes: int


@dataclass
class Manifest:
    """Session manifest. Serialises to ``manifest.json``.

    Per CONTEXT success criterion 3. Written at clean stop; resumable
    recordings re-read the prior manifest (if any) and append segments.
    """

    session_id: str
    started_at: str
    ended_at: str | None
    capture_device: str
    share_state: str = "private"
    event_count: int = 0
    deck_sources: list[str] = field(default_factory=list)
    mp3_segments: list[AudioSegment] = field(default_factory=list)
    # record.py rebuilds Manifest on stop/resume; a later stop drops track_files until re-split.
    track_files: list[TrackFile] = field(default_factory=list)
    watermark: str = PRIVACY_NOTE_DEFAULT
    schema_version: int = 1
    # Portable export marker (issue #10387); omitted from on-disk manifests when unset.
    audio: str | None = None
    # Master-mix level accounting (issue #10371); null without master-mix audio.
    audio_peak_dbfs: float | None = None
    clipped_samples: int | None = None
    clip_events: int | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if d.get("audio") is None:
            d.pop("audio", None)
        return d


def write_manifest(session_dir: Path, manifest: Manifest) -> Path:
    """Write ``manifest.json`` under ``session_dir`` and return the path."""
    session_dir.mkdir(parents=True, exist_ok=True)
    target = session_dir / "manifest.json"
    tmp = target.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(manifest.to_dict(), fh, indent=2, sort_keys=True)
        fh.write("\n")
    tmp.replace(target)
    return target


def read_manifest(session_dir: Path) -> Manifest:
    """Load ``manifest.json`` from ``session_dir``.

    Raises :class:`FileNotFoundError` if the file is missing.
    """
    source = session_dir / "manifest.json"
    with source.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    segments = [AudioSegment(**s) for s in data.pop("mp3_segments", [])]
    track_files = [TrackFile(**t) for t in data.pop("track_files", [])]
    # tolerate absent fields from older writers
    data.setdefault("share_state", "private")
    data.setdefault("event_count", 0)
    data.setdefault("deck_sources", [])
    data.setdefault("schema_version", 1)
    data.setdefault("watermark", PRIVACY_NOTE_DEFAULT)
    data.setdefault("audio", None)
    for key in ("audio_peak_dbfs", "clipped_samples", "clip_events"):
        data.setdefault(key, None)
    return Manifest(mp3_segments=segments, track_files=track_files, **data)


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


__all__ = [
    "AudioSegment",
    "Manifest",
    "TrackFile",
    "now_iso",
    "read_manifest",
    "write_manifest",
]
