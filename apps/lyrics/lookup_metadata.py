"""Whether a track row carries what a lyrics lookup needs (title, artist, duration).

A lack is a fact about the ROW, which a tag re-read or an edit can change, so it
is checked live and never stored as a verdict keyed on the audio file. Kept
apart from :mod:`apps.lyrics.service` so the lookup pipeline's own hook stays a
few lines (PERFBATCH-08 correctness-only).

-Claude
"""
from __future__ import annotations

import sqlite3
from collections.abc import Sequence

#: What a lyrics lookup needs from the row, and the words for each lack. A
#: lack is a fact about the ROW, which a tag re-read or an edit can change, so
#: it must never be stored as a verdict keyed on the audio file.
METADATA_PROBLEMS: tuple[str, ...] = (
    "has no title", "has no artist", "has invalid artists_json", "has invalid duration_ms",
)


def lookup_metadata_problem(title: object, artists_json: object, duration_ms: object) -> str | None:
    """Why this row cannot be looked up (one of :data:`METADATA_PROBLEMS`), or None."""
    if not isinstance(title, str) or not title.strip():
        return METADATA_PROBLEMS[0]
    try:
        from apps.lyrics.service import _first_artist

        _first_artist(artists_json, "")
    except ValueError as error:
        return METADATA_PROBLEMS[2] if "artists_json" in str(error) else METADATA_PROBLEMS[1]
    if not isinstance(duration_ms, int) or duration_ms <= 0:
        return METADATA_PROBLEMS[3]
    return None


def is_metadata_reason(reason: str) -> bool:
    """True for a no-source reason that came from the row's metadata."""
    return any(reason.endswith(problem) for problem in METADATA_PROBLEMS)


def ids_without_lookup_metadata(connection: sqlite3.Connection, stable_ids: Sequence[str]) -> set[str]:
    """The subset of ``stable_ids`` whose row cannot be looked up right now."""
    if not stable_ids:
        return set()
    wanted = set(stable_ids)
    rows = connection.execute(
        "SELECT stable_id, title, artists_json, duration_ms FROM tracks WHERE deleted_at IS NULL"
    ).fetchall()
    return {
        sid for sid, title, artists, duration in rows
        if sid in wanted and lookup_metadata_problem(title, artists, duration) is not None
    }
