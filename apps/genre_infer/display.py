"""What the library listing needs to show one genre per row (GENRE-04): one read per request.

:func:`load_for_rows` reads the configured precedence and the ``ok`` findings of
the providers in it for the rows being built, in one query per 500 ids. A
state.db without the genre tables (never inferred, or a read-only copy) yields
no findings and the default precedence, so rows fall back to the library tag.

-Claude
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from apps.shared.state import db as state_db

from . import findings


@dataclass(frozen=True)
class RowGenres:
    precedence: tuple[str, ...] = findings.DEFAULT_PRECEDENCE
    by_track: dict[str, list[findings.Finding]] = field(default_factory=dict)

    @property
    def shows_library_tag(self) -> bool:
        return findings.PROVIDER_LIBRARY_TAG in self.precedence

    def chosen(self, stable_id: str, *, user_genre: str | None, library_genre: str | None) -> findings.Chosen | None:
        return findings.choose(
            self.by_track.get(stable_id, ()), self.precedence, user_genre=user_genre, library_genre=library_genre
        )


def _read(conn: sqlite3.Connection, stable_ids: Sequence[str]) -> RowGenres:
    if not findings.has_schema(conn):
        return RowGenres()
    cfg = findings.read_config(conn)
    stored = [p for p in cfg.precedence if p not in findings.VIRTUAL_PROVIDERS]
    by_track: dict[str, list[findings.Finding]] = {}
    if stored:
        provider_marks = ",".join("?" * len(stored))
        for start in range(0, len(stable_ids), 500):
            chunk = list(stable_ids[start : start + 500])
            marks = ",".join("?" * len(chunk))
            for sid, provider, genre, confidence in conn.execute(
                f"SELECT stable_id, provider, genre, confidence FROM genre_findings "
                f"WHERE status = 'ok' AND provider IN ({provider_marks}) AND stable_id IN ({marks})",
                [*stored, *chunk],
            ):
                by_track.setdefault(sid, []).append(
                    findings.Finding(sid, provider, findings.STATUS_OK, genre, confidence)
                )
    return RowGenres(cfg.precedence, by_track)


def load_for_rows(state_db_path: Path, stable_ids: Sequence[str]) -> RowGenres:
    if not stable_ids or not state_db_path.is_file():
        return RowGenres()
    conn = state_db.open_ro(state_db_path)
    try:
        return _read(conn, stable_ids)
    finally:
        conn.close()


__all__ = ["RowGenres", "load_for_rows"]
