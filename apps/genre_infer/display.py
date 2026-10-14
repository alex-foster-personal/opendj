"""What the library listing needs to show one genre per row (GENRE-04): one read per request.

:func:`load_for_rows` reads the configured precedence and the ``ok`` findings of
the providers in it for the rows being built, in one query per 500 ids. A
state.db without the genre tables (never inferred, or a read-only copy) yields
no findings and the default precedence, so rows fall back to the library tag.

GENRE-08: a row shows its duplicate GROUP's findings (:mod:`apps.genre_infer.groups`),
so a copy that was never asked about shows the genre its duplicate got.

-Claude
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from apps.shared.state import db as state_db

from . import findings, groups


@dataclass(frozen=True)
class RowGenres:
    precedence: tuple[str, ...] = findings.DEFAULT_PRECEDENCE
    by_track: dict[str, list[findings.Finding]] = field(default_factory=dict)
    #: GENRE-08: stable_id -> the genre typed in the Library on any copy of the track.
    typed: dict[str, str] = field(default_factory=dict)

    @property
    def shows_library_tag(self) -> bool:
        return findings.PROVIDER_LIBRARY_TAG in self.precedence

    def chosen(self, stable_id: str, *, user_genre: str | None, library_genre: str | None) -> findings.Chosen | None:
        """The one genre the row shows. A genre typed on a duplicate counts as typed here."""
        return findings.choose(
            self.by_track.get(stable_id, ()),
            self.precedence,
            user_genre=self.typed.get(stable_id) or user_genre,
            library_genre=library_genre,
        )


def _read(conn: sqlite3.Connection, stable_ids: Sequence[str]) -> RowGenres:
    grp = groups.load(conn)
    typed = groups.user_genres(conn, grp, stable_ids)
    if not findings.has_schema(conn):
        return RowGenres(typed=typed)
    cfg = findings.read_config(conn)
    stored = [p for p in cfg.precedence if p not in findings.VIRTUAL_PROVIDERS]
    by_track = groups.findings_for(conn, grp, stable_ids, providers=stored) if stored else {}
    ok = {sid: [f for f in rows if f.status == findings.STATUS_OK] for sid, rows in by_track.items()}
    return RowGenres(cfg.precedence, {sid: rows for sid, rows in ok.items() if rows}, typed)


def load_for_rows(state_db_path: Path, stable_ids: Sequence[str]) -> RowGenres:
    if not stable_ids or not state_db_path.is_file():
        return RowGenres()
    conn = state_db.open_ro(state_db_path)
    try:
        return _read(conn, stable_ids)
    finally:
        conn.close()


__all__ = ["RowGenres", "load_for_rows"]
