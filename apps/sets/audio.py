"""Audio segment path resolution helpers.

Plan 12-03 Step 5. Keeps the audio-serving endpoint safe: resolves
``<session_dir>/audio_*.mp3`` by name only (no path traversal) and
enumerates segments from the filesystem + cached manifest metadata.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from . import paths as sets_paths
from .manifest import read_manifest


class PathTraversalError(ValueError):
    """Raised when a segment name resolves outside its session dir."""


@dataclass
class AudioSegmentView:
    """Projection of a single MP3 segment for API responses."""

    name: str
    start_t_s: float
    duration_s: float | None
    size_bytes: int


def resolve_segment_path(
    session_id: str,
    segment_name: str,
    *,
    sets_root: Path | None = None,
) -> Path:
    """Return an absolute MP3 path; reject any traversal attempt.

    The segment name MUST:
      * start with ``audio_`` and end with ``.mp3`` (matches ffmpeg
        segmenter naming from :mod:`apps.sets.capture`).
      * contain no path separators or ``..`` sequences.
      * resolve inside ``<SETS_DIR>/<session_id>/``.
    """
    if "/" in segment_name or "\\" in segment_name or ".." in segment_name:
        raise PathTraversalError(f"disallowed chars in segment name {segment_name!r}")
    if not (segment_name.startswith("audio_") and segment_name.endswith(".mp3")):
        raise PathTraversalError(f"unexpected segment name {segment_name!r}")
    # Also validate session_id against the same traversal primitives; sibling
    # list_segments was hardened in commit cabebb9 but this function missed
    # the fix. See .planning/SECURITY-RED-TEAM-2026-04-17.md finding 1.
    if "/" in session_id or "\\" in session_id or ".." in session_id:
        raise PathTraversalError(f"disallowed chars in session_id {session_id!r}")

    root = Path(sets_root) if sets_root is not None else sets_paths.SETS_DIR
    root_resolved = root.resolve()
    session_dir = (root / session_id).resolve()
    # Defensive: reject any session_id that escapes the sets root even if the
    # substring filter above is bypassed (e.g. absolute path session_id).
    try:
        session_dir.relative_to(root_resolved)
    except ValueError as exc:
        raise PathTraversalError(
            f"session_id {session_id!r} escapes {root_resolved}"
        ) from exc
    candidate = (session_dir / segment_name).resolve()
    try:
        candidate.relative_to(session_dir)
    except ValueError as exc:
        raise PathTraversalError(f"{segment_name} escapes {session_dir}") from exc
    return candidate


def list_segments(
    session_id: str,
    *,
    sets_root: Path | None = None,
) -> list[AudioSegmentView]:
    """Return one :class:`AudioSegmentView` per MP3 in the session.

    Durations come from the manifest (written at stop time) when
    available; otherwise they are ``None`` and callers must not rely
    on them.
    """
    root = Path(sets_root) if sets_root is not None else sets_paths.SETS_DIR
    root_resolved = root.resolve()
    session_dir = (root / session_id).resolve()
    # Defensive: reject any session_id that escapes the sets root.
    # Validation runs unconditionally -- even before root exists on disk.
    try:
        session_dir.relative_to(root_resolved)
    except ValueError as exc:
        raise PathTraversalError(
            f"session_id {session_id!r} escapes {root_resolved}"
        ) from exc
    if not session_dir.exists():
        return []
    manifest_durations: dict[str, float | None] = {}
    manifest_starts: dict[str, float] = {}
    try:
        manifest = read_manifest(session_dir)
        for seg in manifest.mp3_segments:
            manifest_durations[seg.name] = seg.duration_s
            manifest_starts[seg.name] = seg.start_t_s
    except FileNotFoundError:
        pass
    out: list[AudioSegmentView] = []
    for mp3 in sorted(session_dir.glob("audio_*.mp3")):
        out.append(
            AudioSegmentView(
                name=mp3.name,
                start_t_s=manifest_starts.get(mp3.name, 0.0),
                duration_s=manifest_durations.get(mp3.name),
                size_bytes=mp3.stat().st_size,
            )
        )
    return out


def segment_view_to_dict(view: AudioSegmentView) -> dict[str, Any]:
    return asdict(view)


__all__ = [
    "PathTraversalError",
    "AudioSegmentView",
    "resolve_segment_path",
    "list_segments",
    "segment_view_to_dict",
]
