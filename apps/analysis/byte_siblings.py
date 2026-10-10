"""Same-bytes siblings for one file path.

Tier-3 ids include mtime, so one file can have two rows (NATIVE-24). A later
row may be served an earlier row's own lane only when ``audio_hash`` proves
the bytes match. A missing hash does not match. This module does not read
Rekordbox fields.
"""

from __future__ import annotations

import sqlite3
import unicodedata
from collections.abc import Callable
from typing import Any


def sibling_path_key(path: str) -> str:
    """NFC path identity, the same spelling the ahead drain uses."""
    return unicodedata.normalize("NFC", path)


def same_bytes_sibling_ids(conn: sqlite3.Connection, stable_id: str) -> list[str]:
    """Other live rows of this path whose audio hash equals this row's hash.

    Path identity is NFC, so an NFD spelling of the same path still matches.
    A byte-for-byte ``file_path`` compare left that row missing in the
    readers after the drain had already skipped it. Ordered by ``stable_id``
    so two donors cannot race. Empty when this row has no path or no hash.
    """
    row = conn.execute(
        "SELECT file_path, audio_hash FROM tracks WHERE stable_id = ?",
        (stable_id,),
    ).fetchone()
    if row is None or not row[0] or not row[1]:
        return []
    key = sibling_path_key(str(row[0]))
    found = conn.execute(
        "SELECT stable_id, file_path FROM tracks "
        "WHERE deleted_at IS NULL AND audio_hash = ? AND stable_id != ? "
        "ORDER BY stable_id",
        (str(row[1]), stable_id),
    ).fetchall()
    return [
        str(sid)
        for sid, path in found
        if path and sibling_path_key(str(path)) == key
    ]


LANE_DONOR_STATUSES = frozenset({"ok", "failed", "missing"})


def choose_lane_donor(candidates: list[tuple[str, str]]) -> str | None:
    """One donor id. ``candidates`` are ``(stable_id, status)`` in stable_id order.

    The first ``ok`` wins. A failed lane is used only when no candidate is
    ``ok``, and then the first one in that same order. ``missing`` is not a
    donor. A lone failure is still a donor: NATIVE-30 serves it. Any other
    status is a broken lane contract and raises, and every candidate is
    checked before one is chosen: an early ``ok`` must not hide a later
    candidate that breaks the contract.
    """
    for sid, status in candidates:
        if status not in LANE_DONOR_STATUSES:
            raise ValueError(f"unknown lane status {status!r} for {sid}")
    fallback: str | None = None
    for sid, status in candidates:
        if status == "ok":
            return sid
        if status == "failed" and fallback is None:
            fallback = sid
    return fallback


def canonical_result_or_same_bytes_sibling(
    conn: sqlite3.Connection,
    stable_id: str,
    lane: str,
    lookup: Callable[[sqlite3.Connection, str, str], Any],
) -> Any:
    """This row's canonical lane, else one same-bytes sibling chosen by status.

    The sibling is the first ok lane in ``stable_id`` order, or the first
    failed lane when none is ok. The library projection borrow uses
    :func:`choose_lane_donor` with the same order, so the two surfaces cannot
    describe different siblings.

    A target lane whose status is ``missing`` is absent for this purpose, the
    same as no result at all: the library borrow treats it that way, so
    returning it here would let ``/anlz`` and the library scalars disagree.
    It is kept only when no sibling can donate.
    """
    result = lookup(conn, stable_id, lane)
    if result is not None and str(getattr(result, "status", "")) != "missing":
        return result
    found: list[tuple[str, Any]] = []
    for other in same_bytes_sibling_ids(conn, stable_id):
        candidate = lookup(conn, other, lane)
        if candidate is not None:
            found.append((other, candidate))
    chosen = choose_lane_donor(
        [(sid, str(getattr(item, "status", ""))) for sid, item in found]
    )
    for sid, item in found:
        if sid == chosen:
            return item
    return result


__all__ = [
    "LANE_DONOR_STATUSES",
    "canonical_result_or_same_bytes_sibling",
    "choose_lane_donor",
    "same_bytes_sibling_ids",
    "sibling_path_key",
]
