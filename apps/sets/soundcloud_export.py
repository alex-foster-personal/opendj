"""SET-06a: metadata-only SoundCloud tracklist export from a recorded set.

Builds a paste-ready pinned-comment tracklist with SoundCloud-clickable
timestamps from SET-01 ``timeline.jsonl``. Does not upload audio and does
not offer takeover or an oDj browser player.
"""
from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import paths as sets_paths
from .record import TimelineJsonl
from .sessions import get_session

LICENSING_REMINDER = (
    "You must own the rights to every recording you publish on SoundCloud, "
    "or have a license that allows this use. Open DJ does not grant those "
    "rights and does not upload audio. This export is a tracklist with "
    "timings only. Check SoundCloud's current terms before you post. "
    "Full-set playback and takeover are not offered: that needs a written "
    "rights position, which is not settled."
)

KIND = "metadata_only"
AUDIO_UPLOAD = "not_offered"
TAKEOVER = "not_offered"
RIGHTS_POSITION = "unsettled"

_EXPORT_KEY_ORDER = (
    "kind",
    "session_id",
    "audio_upload",
    "takeover",
    "rights_position",
    "licensing_reminder",
    "tracklist",
    "comment",
)


class SessionNotFound(LookupError):
    """Raised when a finalized session (manifest.json) cannot be resolved."""


class SoundcloudExportError(ValueError):
    """Raised when a timeline row cannot be turned into an export row."""


@dataclass(frozen=True)
class SoundcloudTracklistRow:
    timestamp_s: float
    timestamp_label: str
    title: str | None
    artist: str | None
    track_stable_id: str | None
    source: str | None
    deck: str | None
    display_name: str


@dataclass(frozen=True)
class SoundcloudExport:
    session_id: str
    tracklist: list[SoundcloudTracklistRow] = field(default_factory=list)
    comment: str = ""
    kind: str = KIND
    audio_upload: str = AUDIO_UPLOAD
    takeover: str = TAKEOVER
    rights_position: str = RIGHTS_POSITION
    licensing_reminder: str = LICENSING_REMINDER

    def to_dict(self, *, include_comment: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": self.kind,
            "session_id": self.session_id,
            "audio_upload": self.audio_upload,
            "takeover": self.takeover,
            "rights_position": self.rights_position,
            "licensing_reminder": self.licensing_reminder,
            "tracklist": [asdict(row) for row in self.tracklist],
            "comment": self.comment if include_comment else None,
        }
        return {key: payload[key] for key in _EXPORT_KEY_ORDER}


def format_soundcloud_timestamp(timestamp_s: float) -> str:
    """SoundCloud auto-link label: ``m:ss`` under one hour, else ``h:mm:ss``."""
    total = math.floor(timestamp_s)
    if total < 0:
        raise SoundcloudExportError(f"timestamp_s must be >= 0, got {timestamp_s!r}")
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def _optional_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped if stripped else None


def _stable_id(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise SoundcloudExportError(f"track_stable_id must not be a bool, got {value!r}")
    if isinstance(value, (int, float)):
        text = str(int(value) if isinstance(value, float) and value.is_integer() else value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise SoundcloudExportError(f"track_stable_id must be a string, got {type(value).__name__}")
    return text or None


def _display_name(artist: str | None, title: str | None, stable_id: str | None) -> str:
    if artist and title:
        return f"{artist} - {title}"
    if title:
        return title
    if artist:
        return artist
    if stable_id:
        return f"unresolved ({stable_id})"
    return "unresolved (no title)"


def _timestamp_s(event: dict[str, Any]) -> float:
    raw = event.get("timestamp_s")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise SoundcloudExportError(
            f"track_loaded row missing numeric timestamp_s: {raw!r}"
        )
    if raw < 0:
        raise SoundcloudExportError(f"timestamp_s must be >= 0, got {raw!r}")
    return float(raw)


def _iter_kept_plays(events: Iterator[dict[str, Any]]) -> Iterator[SoundcloudTracklistRow]:
    last_stable_id: str | None = None
    have_last = False
    for event in events:
        if event.get("action") != "track_loaded":
            continue
        value = event.get("value")
        if not isinstance(value, dict):
            value = {}
        title = _optional_string(value.get("title"))
        artist = _optional_string(value.get("artist"))
        stable_id = _stable_id(event.get("track_stable_id"))
        if stable_id is None and title is None:
            continue
        if have_last and stable_id is not None and stable_id == last_stable_id:
            continue
        timestamp_s = _timestamp_s(event)
        source = event.get("source")
        deck = event.get("deck")
        yield SoundcloudTracklistRow(
            timestamp_s=timestamp_s,
            timestamp_label=format_soundcloud_timestamp(timestamp_s),
            title=title,
            artist=artist,
            track_stable_id=stable_id,
            source=source if isinstance(source, str) else None,
            deck=deck if isinstance(deck, str) else None,
            display_name=_display_name(artist, title, stable_id),
        )
        last_stable_id = stable_id
        have_last = True


def _comment_from(rows: list[SoundcloudTracklistRow]) -> str:
    if not rows:
        return ""
    return "".join(f"{row.timestamp_label} {row.display_name}\n" for row in rows)


def build_soundcloud_export(
    session_id: str, *, sets_root: Path | None = None
) -> SoundcloudExport:
    """Read ``timeline.jsonl`` for a finalized session and build the export."""
    session = get_session(session_id, sets_root=sets_root)
    if session is None:
        raise SessionNotFound(f"session {session_id!r} not found")
    session_path = sets_paths.session_dir(session_id, root=sets_root)
    events = TimelineJsonl(session_path / "timeline.jsonl").iter_events()
    tracklist = list(_iter_kept_plays(events))
    return SoundcloudExport(
        session_id=session.summary.session_id,
        tracklist=tracklist,
        comment=_comment_from(tracklist),
        licensing_reminder=LICENSING_REMINDER,
    )


__all__ = [
    "AUDIO_UPLOAD",
    "KIND",
    "LICENSING_REMINDER",
    "RIGHTS_POSITION",
    "TAKEOVER",
    "SessionNotFound",
    "SoundcloudExport",
    "SoundcloudExportError",
    "SoundcloudTracklistRow",
    "build_soundcloud_export",
    "format_soundcloud_timestamp",
]
