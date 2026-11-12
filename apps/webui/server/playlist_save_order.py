"""Save this order (LIBUX-8678): make a displayed order the playlist's stored order.

One renumber of every live member's ``order_key`` in a single playlist
transaction, recorded as one ``memberships`` edit, so one undo puts the old
keys back. The caller names the order either as the exact list of live
``item_id`` values (what the UI shows after any column sort, sections included)
or, for agents, as a field to sort by. Section dividers follow their tracks via
the store's ``_sections_follow``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Literal

from apps.shared.state.order_key import renumbered_keys

from .backend import Track
from .playlist_add import _load_live_members
from .playlist_remove import _snapshot_with_members
from .track_features import camelot_or_none

if TYPE_CHECKING:
    from .playlist_store import PlaylistRow, PlaylistStore

SortField = Literal["bpm", "key", "title", "artist", "duration", "rating", "genre"]
SORT_FIELDS: tuple[str, ...] = ("bpm", "key", "title", "artist", "duration", "rating", "genre")


class SaveOrderError(ValueError):
    """The requested order is not a permutation of the playlist's live items."""


# ----- sort keys for ``by`` -------------------------------------------------
def _camelot_rank(track: Track) -> tuple[int, str] | None:
    camelot = camelot_or_none(track.key)
    return None if camelot is None else (int(camelot[:-1]), camelot[-1])


def _text(value: str | None) -> str | None:
    return value.casefold() if value else None


_SORT_VALUE: dict[str, Callable[[Track], object]] = {
    "bpm": lambda t: t.bpm,
    "key": _camelot_rank,
    "title": lambda t: _text(t.title),
    "artist": lambda t: _text(t.artist),
    "duration": lambda t: t.duration_ms,
    "rating": lambda t: t.rating,
    "genre": lambda t: _text(t.genre),
}


def item_ids_sorted_by(
    store: PlaylistStore,
    playlist_id: str,
    by: str,
    *,
    descending: bool,
    tracks: Callable[[list[str]], dict[str, Track]],
) -> list[str]:
    """Live item_ids sorted by ``by``; tracks with no value stay last, ties keep playlist order."""
    members = _load_live_members(store._conn, playlist_id)
    by_sid = tracks([m.stable_id for m in members])
    value = _SORT_VALUE[by]
    valued = [(value(by_sid[m.stable_id]), m.item_id) for m in members if m.stable_id in by_sid]
    present = [(v, iid) for v, iid in valued if v is not None]
    present.sort(key=lambda pair: pair[0], reverse=descending)  # type: ignore[arg-type,return-value]
    placed = {iid for _v, iid in present}
    return [iid for _v, iid in present] + [m.item_id for m in members if m.item_id not in placed]


# ----- the write ------------------------------------------------------------
def save_order(
    store: PlaylistStore,
    playlist_id: str,
    item_ids: list[str],
    *,
    expected_etag: str,
) -> PlaylistRow:
    """Renumber every live member into ``item_ids`` order; an unchanged order is a no-op."""
    with store._writer.playlist_transaction():
        row = store._load(playlist_id)
        store._check_etag(row, expected_etag)
        members = _load_live_members(store._conn, playlist_id)
        live = [m.item_id for m in members]
        if len(item_ids) != len(set(item_ids)) or sorted(item_ids) != sorted(live) or "" in live:
            raise SaveOrderError(f"item_ids must name each of the {len(live)} live items of {playlist_id} exactly once")
        if item_ids == live:
            return row
        before = _snapshot_with_members(store, row)
        store._writer.update_playlist_membership_order_keys(
            playlist_id,
            list(zip(item_ids, renumbered_keys(len(item_ids)), strict=True)),
            renumbered=True,
        )
        saved = store._load(playlist_id)
        store._record_edit("memberships", playlist_id, before, _snapshot_with_members(store, saved))
        return saved


__all__ = ["SORT_FIELDS", "SaveOrderError", "SortField", "item_ids_sorted_by", "save_order"]
