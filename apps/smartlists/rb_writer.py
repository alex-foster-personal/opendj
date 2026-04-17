"""RB playlist writer wrapper (Phase 3 <-> Phase 8 bridge).

The Phase 8 materialiser speaks the :class:`apps.smartlists.writers.PlaylistWriter`
protocol (per-smartlist ``create_playlist`` / ``apply_diff`` ops keyed by
``stable_id`` tracks). Phase 3's underlying write-path is
``pyrekordbox.Rekordbox6Database`` which creates / modifies playlists in
the RB master.db directly.

This wrapper:

1. Resolves ``stable_id`` -> RB ``ContentID`` via the state-layer
   ``track_vendor_ids`` table (vendor="rekordbox").
2. Dispatches to ``db.create_playlist`` / ``db.add_to_playlist`` /
   ``db.remove_from_playlist`` for the actual mutation.
3. Commits per-call so a failure on one smartlist doesn't leave the DB in
   a half-written state.

Construction is lazy: callers pass factory callables so unit tests can
inject fakes without touching a live RB DB.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Callable


def _resolve_rb_id(state_conn: sqlite3.Connection, stable_id: str) -> str | None:
    """Return the rekordbox ``ContentID`` for ``stable_id`` or None."""
    row = state_conn.execute(
        "SELECT vendor_id FROM track_vendor_ids "
        "WHERE stable_id = ? AND vendor = 'rekordbox' LIMIT 1",
        (stable_id,),
    ).fetchone()
    return row[0] if row else None


@dataclass
class RBPlaylistWriter:
    """``PlaylistWriter`` backed by pyrekordbox's ORM.

    Attributes
    ----------
    db
        Open ``Rekordbox6Database`` (or any duck-typed fake with
        ``get_playlist``, ``create_playlist``, ``add_to_playlist``,
        ``remove_from_playlist``, ``commit``).
    state_conn
        SQLite connection to the shared state DB (for stable_id ->
        ContentID resolution via ``track_vendor_ids``).
    vendor
        Vendor tag surfaced in ``MaterializeResult.writers_applied``.
    """

    db: Any
    state_conn: sqlite3.Connection
    vendor: str = "rekordbox"

    def _find_playlist(self, name: str):
        for p in self.db.get_playlist():
            if (p.Name or "") == name:
                return p
        return None

    def playlist_exists(self, name: str) -> bool:
        return self._find_playlist(name) is not None

    def _resolve_or_raise(self, stable_ids: list[str]) -> list[str]:
        rb_ids: list[str] = []
        missing: list[str] = []
        for sid in stable_ids:
            rb_id = _resolve_rb_id(self.state_conn, sid)
            if rb_id is None:
                missing.append(sid)
            else:
                rb_ids.append(str(rb_id))
        if missing:
            raise RuntimeError(
                f"rekordbox: no ContentID mapping for stable_ids "
                f"{missing[:5]}{'...' if len(missing) > 5 else ''}"
            )
        return rb_ids

    def create_playlist(self, name: str, track_ids: list[str]) -> None:
        rb_ids = self._resolve_or_raise(list(track_ids))
        pl = self.db.create_playlist(name)
        for rb_id in rb_ids:
            self.db.add_to_playlist(pl, rb_id)
        self.db.commit()

    def apply_diff(
        self, name: str, added: list[str], removed: list[str],
    ) -> None:
        pl = self._find_playlist(name)
        if pl is None:
            raise RuntimeError(
                f"rekordbox: playlist {name!r} not found; "
                "cannot apply diff"
            )
        add_ids = self._resolve_or_raise(list(added))
        remove_ids = self._resolve_or_raise(list(removed))
        for rb_id in remove_ids:
            self.db.remove_from_playlist(pl, rb_id)
        for rb_id in add_ids:
            self.db.add_to_playlist(pl, rb_id)
        self.db.commit()


def build_rb_writer(
    db_factory: Callable[[], Any] | None = None,
    state_conn_factory: Callable[[], sqlite3.Connection] | None = None,
) -> RBPlaylistWriter | None:
    """Construct an :class:`RBPlaylistWriter` or return None on failure.

    ``None`` is returned when the RB DB or state DB can't be opened -- the
    materialiser treats writer absence as a soft-fail and moves on to the
    next writer in the list.
    """
    if db_factory is None:
        from apps.shared.rekordbox_db import open_db

        db_factory = open_db
    if state_conn_factory is None:
        from apps.shared.state.db import open_ro as _open_state_ro

        state_conn_factory = _open_state_ro
    try:
        db = db_factory()
        state_conn = state_conn_factory()
    except Exception:
        return None
    return RBPlaylistWriter(db=db, state_conn=state_conn)


__all__ = ["RBPlaylistWriter", "build_rb_writer", "_resolve_rb_id"]
