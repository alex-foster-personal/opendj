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


@dataclass
class AudioSegment:
    """One rolled MP3 file under the session dir."""

    name: str
    start_t_s: float
    duration_s: float | None
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
    watermark: str = (
        "personal-review-only; contains copyrighted audio; do not distribute"
    )
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
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
    # tolerate absent fields from older writers
    data.setdefault("share_state", "private")
    data.setdefault("event_count", 0)
    data.setdefault("deck_sources", [])
    data.setdefault("schema_version", 1)
    data.setdefault(
        "watermark",
        "personal-review-only; contains copyrighted audio; do not distribute",
    )
    return Manifest(mp3_segments=segments, **data)


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


__all__ = ["AudioSegment", "Manifest", "write_manifest", "read_manifest", "now_iso"]
