"""Paths for the set-recording data tree.

Every file the recorder writes lives under :data:`SETS_DIR`. A single
session occupies ``<SETS_DIR>/<session_id>/``:

  * ``audio_YYYY-MM-DDTHH-MM-SS.wav`` -- rolling 5-minute segments, 16-bit
    PCM from odj-audio's capture; ``.mp3`` from the ffmpeg capture a
    checkout without a capture-capable odj-audio build falls back to
  * ``<session_id>.wav``              -- joined recording (#10079)
  * ``NN Artist - Title.wav``         -- per-track splits (#10378)
  * ``timeline.jsonl``                -- append-only event log
  * ``manifest.json``                 -- summary written at stop
  * ``recorder.pid``                  -- live only while recording
  * ``transitions.jsonl``             -- Plan 12-02 output
  * ``labels.jsonl``                  -- Plan 12-02/12-03 relabels

``SETS_DB`` is the shim SQLite DB (schema in :mod:`apps.sets.state`). It is
local and derivative; delete-safe. If Phase 5's ``apps.shared.state``
grows a ``sets`` table compatible with ours, a follow-up migration moves
the rows over (see Plan 12-01 Open Question 2).
"""

from __future__ import annotations

import re
from pathlib import Path, PureWindowsPath

from apps.shared.paths import DATA_DIR

SETS_DIR: Path = DATA_DIR / "sets"
SETS_DB: Path = SETS_DIR / "sets.db"
MODELS_DIR: Path = Path(__file__).resolve().parent / "models"
DISALLOWED_FILENAME_CHARS: frozenset[str] = frozenset('<>:"/\\|?*')


def sanitize_filename_part(text: str) -> str:
    """Strip forbidden filename chars and collapse whitespace."""
    normalized = "".join(" " if ch.isspace() else ch for ch in text if ch not in DISALLOWED_FILENAME_CHARS)
    cleaned = "".join(ch for ch in normalized if ord(ch) >= 32)
    return re.sub(r" +", " ", cleaned).strip(" .")


def joined_recording_path(session_dir: Path, session_id: str) -> Path:
    """Path to the single joined WAV for a session (#10079)."""
    return session_dir / f"{session_id}.wav"


def resolve_sets_root(sets_root: Path | None = None) -> Path:
    """Return ``sets_root`` when injected, otherwise :data:`SETS_DIR`."""
    return Path(sets_root) if sets_root is not None else SETS_DIR


#: The segment suffixes the recorder writes and the media type each is served
#: as. Names are ``audio_<UTC start>`` plus one of these, so sorting by name
#: is sorting by start even in a session resumed on the other backend.
SEGMENT_MEDIA_TYPES: dict[str, str] = {".mp3": "audio/mpeg", ".wav": "audio/wav"}


def is_segment_name(name: str) -> bool:
    """Whether ``name`` is a recorder segment's file name (no directory part)."""
    return name.startswith("audio_") and Path(name).suffix in SEGMENT_MEDIA_TYPES


def segment_files(session_dir: Path) -> list[Path]:
    """The session's audio segments, oldest first."""
    return sorted(
        (p for p in session_dir.glob("audio_*") if is_segment_name(p.name)),
        key=lambda p: p.name,
    )


class SessionPathError(ValueError):
    """Raised when a session ID cannot name one contained session directory."""


def _validate_session_id_syntax(session_id: str) -> None:
    """Reject session ids with path syntax before any filesystem resolution."""
    if not isinstance(session_id, str) or not session_id:
        raise SessionPathError("session_id must be a non-empty string")
    if (
        session_id in {".", ".."}
        or any(char in DISALLOWED_FILENAME_CHARS for char in session_id)
        or any(ord(char) < 32 for char in session_id)
    ):
        raise SessionPathError(f"disallowed path syntax in session_id {session_id!r}")
    windows_path = PureWindowsPath(session_id)
    if windows_path.drive or windows_path.root:
        raise SessionPathError(f"absolute path not allowed for session_id {session_id!r}")


def session_dir(session_id: str, root: Path | None = None) -> Path:
    """Return the resolved, direct-child directory for ``session_id``.

    Session IDs are filesystem boundary inputs. They must name exactly one
    direct child of the resolved sets root, with no path separators, absolute
    paths, traversal segments, or Windows drive prefixes. Existing symlinks
    that resolve outside the root are also rejected.

    ``root`` lets tests inject a tmp dir in place of :data:`SETS_DIR`.
    """
    _validate_session_id_syntax(session_id)
    base = (Path(root) if root is not None else SETS_DIR).resolve()
    candidate = (base / session_id).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise SessionPathError(f"session_id {session_id!r} resolves outside sets root {base}") from exc
    if candidate.parent != base:
        raise SessionPathError(f"session_id {session_id!r} must resolve to a direct child of {base}")
    return candidate


__all__ = [
    "DISALLOWED_FILENAME_CHARS",
    "SETS_DIR",
    "SETS_DB",
    "MODELS_DIR",
    "SessionPathError",
    "joined_recording_path",
    "resolve_sets_root",
    "sanitize_filename_part",
    "session_dir",
]
