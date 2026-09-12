"""O(k) playlist membership slice move (LIBM-22 ``:move`` algorithm)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from apps.shared.state.order_key import (
    PrecisionExhausted,
    allocate_keys,
    from_index,
)

from .backend import BackendError, NotFoundError
from .playlist_add import (
    MembershipRow,
    SmartlistImmutableError,
    _is_smartlist,
    _load_live_members,
)
from .playlist_remove import _snapshot_with_members

if TYPE_CHECKING:
    from .playlist_store import PlaylistRow, PlaylistStore


class SliceNotContiguousError(BackendError):
    def __init__(
        self, range_start: str, range_end: str, actual_end: str,
    ) -> None:
        self.range_start = range_start
        self.range_end = range_end
        self.actual_end = actual_end
        super().__init__(
            f"slice not contiguous: claimed range_end {range_end} but "
            f"item at offset is {actual_end} (range_start {range_start})"
        )


class TargetInsideSliceError(BackendError):
    def __init__(self, item_id: str) -> None:
        self.item_id = item_id
        super().__init__(
            f"target membership {item_id} is inside the moved slice",
        )


@dataclass(frozen=True)
class MoveResult:
    row: PlaylistRow
    renumbered: bool
    no_op: bool


def _find_member_index(members: list[MembershipRow], item_id: str) -> int:
    for i, member in enumerate(members):
        if member.item_id == item_id:
            return i
    raise NotFoundError(
        f"playlist has no membership {item_id}",
    )


def _resolve_slice(
    members: list[MembershipRow],
    playlist_id: str,
    range_start: str,
    range_length: int | None,
    range_end: str | None,
) -> tuple[int, int, list[MembershipRow]]:
    if range_length is None and range_end is None:
        raise BackendError("range_length and/or range_end is required")
    if range_length is not None and range_length < 1:
        raise BackendError(f"range_length must be >= 1, got {range_length}")

    try:
        start_idx = _find_member_index(members, range_start)
    except NotFoundError as exc:
        raise NotFoundError(
            f"playlist {playlist_id} has no membership {range_start}",
        ) from exc

    if range_length is not None and range_end is None:
        k = range_length
        if start_idx + k > len(members):
            raise BackendError(
                f"slice exceeds playlist: start index {start_idx} + "
                f"length {k} > {len(members)} live members",
            )
        end_idx = start_idx + k - 1
    elif range_length is None and range_end is not None:
        end_idx = _find_member_index(members, range_end)
        if end_idx < start_idx:
            raise BackendError(
                f"range_end {range_end} precedes range_start {range_start}",
            )
    else:
        assert range_length is not None and range_end is not None
        k = range_length
        if start_idx + k > len(members):
            raise BackendError(
                f"slice exceeds playlist: start index {start_idx} + "
                f"length {k} > {len(members)} live members",
            )
        end_idx = start_idx + k - 1
        actual = members[end_idx].item_id
        if actual != range_end:
            raise SliceNotContiguousError(range_start, range_end, actual)

    slice_members = members[start_idx : end_idx + 1]
    return start_idx, end_idx, slice_members


def _neighbor_before(
    members: list[MembershipRow], index: int, skip: set[str],
) -> MembershipRow | None:
    for i in range(index - 1, -1, -1):
        if members[i].item_id not in skip:
            return members[i]
    return None


def _neighbor_after(
    members: list[MembershipRow], index: int, skip: set[str],
) -> MembershipRow | None:
    for i in range(index + 1, len(members)):
        if members[i].item_id not in skip:
            return members[i]
    return None


def _insert_slice(
    members: list[MembershipRow],
    slice_members: list[MembershipRow],
    *,
    before_item_id: str | None,
    after_item_id: str | None,
) -> list[MembershipRow]:
    skip = {m.item_id for m in slice_members}
    remaining = [m for m in members if m.item_id not in skip]
    if before_item_id is not None:
        idx = _find_member_index(remaining, before_item_id)
        return remaining[:idx] + slice_members + remaining[idx:]
    assert after_item_id is not None
    idx = _find_member_index(remaining, after_item_id)
    return remaining[: idx + 1] + slice_members + remaining[idx + 1 :]


def move_memberships(
    store: PlaylistStore,
    playlist_id: str,
    *,
    range_start: str,
    range_length: int | None = None,
    range_end: str | None = None,
    before_item_id: str | None = None,
    after_item_id: str | None = None,
    expected_etag: str,
    record_edit: bool = True,
) -> MoveResult:
    """Relocate a contiguous membership slice without rewriting neighbors."""
    writer = store._writer
    conn = store._conn

    try:
        before_row = store._load(playlist_id)
    except NotFoundError:
        if _is_smartlist(conn, playlist_id):
            raise SmartlistImmutableError(
                "cannot reorder tracks in a smartlist",
            ) from None
        raise

    store._check_etag(before_row, expected_etag)
    members = _load_live_members(conn, playlist_id)

    if before_item_id is not None and after_item_id is not None:
        raise BackendError("exactly one of before_item_id or after_item_id is required")
    if before_item_id is None and after_item_id is None:
        raise BackendError("before_item_id or after_item_id is required")

    start_idx, end_idx, slice_members = _resolve_slice(
        members, playlist_id, range_start, range_length, range_end,
    )
    slice_ids = {m.item_id for m in slice_members}
    k = len(slice_members)

    neighbor_id = before_item_id if before_item_id is not None else after_item_id
    assert neighbor_id is not None
    try:
        neighbor_idx = _find_member_index(members, neighbor_id)
    except NotFoundError as exc:
        raise NotFoundError(
            f"playlist {playlist_id} has no membership {neighbor_id}",
        ) from exc

    if neighbor_id in slice_ids:
        raise TargetInsideSliceError(neighbor_id)

    skip = slice_ids
    if after_item_id is not None:
        prev = _neighbor_before(members, start_idx, skip)
        if prev is not None and prev.item_id == after_item_id:
            return MoveResult(before_row, renumbered=False, no_op=True)
        left = members[neighbor_idx].order_key
        nxt = _neighbor_after(members, neighbor_idx, skip)
        right = nxt.order_key if nxt is not None else None
    else:
        nxt = _neighbor_after(members, end_idx, skip)
        if nxt is not None and nxt.item_id == before_item_id:
            return MoveResult(before_row, renumbered=False, no_op=True)
        prev = _neighbor_before(members, neighbor_idx, skip)
        left = prev.order_key if prev is not None else None
        right = members[neighbor_idx].order_key

    renumbered = False
    try:
        new_keys = allocate_keys(left, right, k)
        updates = list(zip([m.item_id for m in slice_members], new_keys))
    except PrecisionExhausted:
        new_order = _insert_slice(
            members,
            slice_members,
            before_item_id=before_item_id,
            after_item_id=after_item_id,
        )
        updates = [
            (m.item_id, from_index(i)) for i, m in enumerate(new_order)
        ]
        renumbered = True

    before_snap = (
        _snapshot_with_members(store, before_row) if record_edit else None
    )

    with writer.playlist_transaction():
        writer.update_playlist_membership_order_keys(
            playlist_id, updates, renumbered=renumbered,
        )

    new_row = store._load(playlist_id)
    if record_edit:
        store._record_edit(
            "memberships",
            playlist_id,
            before_snap,
            _snapshot_with_members(store, new_row),
        )
    return MoveResult(new_row, renumbered=renumbered, no_op=False)


__all__ = [
    "MoveResult",
    "SliceNotContiguousError",
    "TargetInsideSliceError",
    "move_memberships",
]
