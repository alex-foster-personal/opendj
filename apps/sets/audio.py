"""Audio segment path resolution helpers.

Plan 12-03 Step 5. Keeps the audio-serving endpoint safe: resolves
``<session_dir>/audio_*`` segments by name only (no path traversal) and
enumerates segments from the filesystem + cached manifest metadata.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from . import paths as sets_paths
from .manifest import read_manifest

PathTraversalError = sets_paths.SessionPathError
"""Backward-compatible alias for rejected audio path inputs."""


@dataclass
class AudioSegmentView:
    """Projection of a single audio segment for API responses."""

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
    """Return an absolute segment path; reject any traversal attempt.

    The segment name MUST:
      * start with ``audio_`` and end with a suffix in
        :data:`apps.sets.paths.SEGMENT_MEDIA_TYPES` (``.wav`` from
        odj-audio's capture, ``.mp3`` from ffmpeg's; :mod:`apps.sets.capture`).
      * contain no path separators or ``..`` sequences.
      * resolve inside ``<SETS_DIR>/<session_id>/``.
    """
    if "/" in segment_name or "\\" in segment_name or ".." in segment_name:
        raise PathTraversalError(f"disallowed chars in segment name {segment_name!r}")
    if not sets_paths.is_segment_name(segment_name):
        raise PathTraversalError(f"unexpected segment name {segment_name!r}")
    root = Path(sets_root) if sets_root is not None else sets_paths.SETS_DIR
    session_dir = sets_paths.session_dir(session_id, root=root)
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
    """Return one :class:`AudioSegmentView` per audio segment in the session.

    Durations come from the manifest (written at stop time) when
    available; otherwise they are ``None`` and callers must not rely
    on them.
    """
    root = Path(sets_root) if sets_root is not None else sets_paths.SETS_DIR
    session_dir = sets_paths.session_dir(session_id, root=root)
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
    for seg_path in sets_paths.segment_files(session_dir):
        out.append(
            AudioSegmentView(
                name=seg_path.name,
                start_t_s=manifest_starts.get(seg_path.name, 0.0),
                duration_s=manifest_durations.get(seg_path.name),
                size_bytes=seg_path.stat().st_size,
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
