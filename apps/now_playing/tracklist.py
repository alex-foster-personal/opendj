"""Tonight's tracklist assembled from the play log (NOWP, issue #10341).

Pure functions: plays + library metadata + hidden set -> ordered rows, and the
plain-text rendering ``opendj now-playing tracklist`` prints. No FastAPI here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import tzinfo
from typing import Any

from apps.now_playing.contract import UNKNOWN_LABEL, TrackLike
from apps.shared.state.play_log import PlayRow
from apps.shared.state.sync_stamp import parse_canonical
from apps.shared.state.tracklist_redaction import HIDDEN_LABEL


def build_rows(
    plays: Sequence[PlayRow],
    tracks: Mapping[str, TrackLike],
    hidden: set[str],
) -> list[dict[str, Any]]:
    """One row per play, in play order; missing tracks keep null metadata, hidden ones read ``ID``."""
    rows: list[dict[str, Any]] = []
    for play in plays:
        track = tracks.get(play.stable_id)
        is_hidden = play.stable_id in hidden
        artist = None if track is None else track.artist
        title = None if track is None else track.title
        if is_hidden:
            artist = title = HIDDEN_LABEL
        rows.append(
            {
                "stable_id": play.stable_id,
                "played_at": play.played_at,
                "artist": artist,
                "title": title,
                "key": None if track is None else track.key,
                "bpm": None if track is None else track.bpm,
                "hidden": is_hidden,
                "in_library": track is not None,
                "deck": play.deck,
                "source": play.source,
            }
        )
    return rows


def format_txt(rows: Sequence[Mapping[str, Any]], tz: tzinfo | None) -> str:
    """``HH:MM  Artist - Title`` per row in ``tz`` (machine local when None).

    A row with null artist or title prints ``Unknown`` for it plus the stable_id,
    e.g. ``00:40  Unknown - Unknown (sid)``. No rows gives an empty string.
    """
    lines = []
    for row in rows:
        played = parse_canonical(row["played_at"])
        local = played.astimezone(tz) if tz is not None else played.astimezone()
        artist, title = row["artist"], row["title"]
        line = f"{local:%H:%M}  {artist or UNKNOWN_LABEL} - {title or UNKNOWN_LABEL}"
        if artist is None or title is None:
            line = f"{line} ({row['stable_id']})"
        lines.append(line)
    return "\n".join(lines)


__all__ = ["build_rows", "format_txt"]
