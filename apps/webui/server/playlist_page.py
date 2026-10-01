"""One ``limit``/``offset`` window of a playlist's live membership (LIBM-133).

``GET /api/v1/playlists/{id}/tracks`` used to read and sort EVERY live member
to serve a 30-row page: 166 ms per page at 10,042 members on agentbox, and a
335-page fill of 76 s (LIBM-120 re-measure, main ``2ecfde5d7``). This reads
the playlist header, the live member count and only the requested window.

The window's ORDER BY is :data:`MEMBERSHIP_ORDER_BY`, the same text
``SqliteBackend.get_playlist`` sorts by, so a page is exactly the matching
slice of the full read. ``(playlist_id, position)`` is the table's primary
key, so that order is total and a row cannot move between pages. The text
also matches ``idx_playlist_memberships_live_order`` (schema v22), which
serves the ORDER BY without a temp b-tree sort; the window then costs its
``offset`` index steps plus ``limit`` rows, not the whole membership.

All three reads run in ONE read transaction, so the header's ETag, ``total``
and the rows describe the same committed state even while a writer commits.
"""

from __future__ import annotations

import sqlite3

from .backend import NotFoundError, Playlist, PlaylistPage
from .playlist_add import MEMBERSHIP_ORDER_BY

HEADER_SQL = (
    "SELECT playlist_id, name, vendor, vendor_pl_id, "
    "       created_at, updated_at, forbid_duplicates "
    "FROM playlists WHERE playlist_id = ? AND deleted_at IS NULL"
)

LIVE_COUNT_SQL = (
    "SELECT COUNT(*) FROM playlist_memberships "
    "INDEXED BY idx_playlist_memberships_live_stable_id "
    "WHERE playlist_id = ? AND deleted_at IS NULL"
)
"""Counts on the narrow ``(playlist_id, stable_id)`` index, never the order index.

Without ``INDEXED BY`` sqlite counts on the order index, whose entries carry
each row's order_key. Pre-LIBM-132 appends grew that key by one character per
append (mean 5,007, max 10,027 characters on the 10k fixture), so the count
walked about 50 MB of index per page: 807 of 1,118 busy samples of this
function at 10k. ``INDEXED BY`` also fails loudly if the v22 index is absent.
"""

WINDOW_SQL = (
    "SELECT stable_id, item_id FROM playlist_memberships "
    "WHERE playlist_id = ? AND deleted_at IS NULL "
    f"ORDER BY {MEMBERSHIP_ORDER_BY} LIMIT ? OFFSET ?"
)


def playlist_from_header(row: sqlite3.Row, items: list[str], item_ids: list[str]) -> Playlist:
    return Playlist(
        playlist_id=row["playlist_id"], name=row["name"],
        vendor=row["vendor"], vendor_pl_id=row["vendor_pl_id"], items=items,
        item_ids=item_ids,
        created_at=row["created_at"], updated_at=row["updated_at"],
        forbid_duplicates=bool(row["forbid_duplicates"]),
    )


def read_playlist_header(conn: sqlite3.Connection, playlist_id: str) -> sqlite3.Row:
    """The live playlist row, or NotFoundError. ``conn`` must use ``sqlite3.Row``."""
    row = conn.execute(HEADER_SQL, (playlist_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"playlist not found: {playlist_id}")
    return row


def read_playlist_page(
    conn: sqlite3.Connection,
    playlist_id: str,
    *,
    limit: int,
    offset: int,
) -> PlaylistPage:
    """Header, live count and one ordered window, in one read transaction.

    A state.db without ``playlist_memberships`` or its v22 indexes raises
    ``sqlite3.OperationalError``: a missing schema is never an empty playlist.
    """
    conn.execute("BEGIN")
    try:
        header = read_playlist_header(conn, playlist_id)
        total = conn.execute(LIVE_COUNT_SQL, (playlist_id,)).fetchone()[0]
        rows = conn.execute(WINDOW_SQL, (playlist_id, limit, offset)).fetchall()
    finally:
        conn.execute("COMMIT")
    return PlaylistPage(
        playlist=playlist_from_header(header, [r[0] for r in rows], [r[1] or "" for r in rows]),
        total=total,
    )


__all__ = [
    "LIVE_COUNT_SQL",
    "WINDOW_SQL",
    "playlist_from_header",
    "read_playlist_header",
    "read_playlist_page",
]
