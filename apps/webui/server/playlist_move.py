"""O(k) playlist membership slice move (LIBM-22 ``:move`` algorithm)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from apps.shared.state.order_key import (
    PrecisionExhausted,
    allocate_keys,
    renumbered_keys,
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
class MoveSliceSpec:
    range_start: str
    range_length: int | None = None
    range_end: str | None = None


@dataclass(frozen=True)
class MoveAnchor:
    before_item_id: str | None = None
    after_item_id: str | None = None


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


def _slice_end_by_length(
    members: list[MembershipRow], start_idx: int, range_length: int,
) -> int:
    if start_idx + range_length > len(members):
        raise BackendError(
            f"slice exceeds playlist: start index {start_idx} + "
            f"length {range_length} > {len(members)} live members",
        )
    return start_idx + range_length - 1


def _slice_end_by_end_id(
    members: list[MembershipRow], start_idx: int, range_end: str,
) -> int:
    end_idx = _find_member_index(members, range_end)
    if end_idx < start_idx:
        raise BackendError(
            f"range_end {range_end} precedes range_start at index {start_idx}",
        )
    return end_idx


def _slice_end_by_length_and_end(
    members: list[MembershipRow],
    start_idx: int,
    range_length: int,
    range_start: str,
    range_end: str,
) -> int:
    end_idx = _slice_end_by_length(members, start_idx, range_length)
    actual = members[end_idx].item_id
    if actual != range_end:
        raise SliceNotContiguousError(range_start, range_end, actual)
    return end_idx


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
        end_idx = _slice_end_by_length(members, start_idx, range_length)
    elif range_length is None and range_end is not None:
        end_idx = _slice_end_by_end_id(members, start_idx, range_end)
    else:
        assert range_length is not None and range_end is not None
        end_idx = _slice_end_by_length_and_end(
            members, start_idx, range_length, range_start, range_end,
        )

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


def _validate_move_anchors(
    before_item_id: str | None, after_item_id: str | None,
) -> None:
    if before_item_id is not None and after_item_id is not None:
        raise BackendError("exactly one of before_item_id or after_item_id is required")
    if before_item_id is None and after_item_id is None:
        raise BackendError("before_item_id or after_item_id is required")


def _order_keys_after_neighbor(
    members: list[MembershipRow],
    start_idx: int,
    neighbor_idx: int,
    slice_members: list[MembershipRow],
    slice_ids: set[str],
    after_item_id: str,
) -> tuple[list[tuple[str, str]], bool, bool]:
    prev = _neighbor_before(members, start_idx, slice_ids)
    if prev is not None and prev.item_id == after_item_id:
        return [], False, True
    left = members[neighbor_idx].order_key
    nxt = _neighbor_after(members, neighbor_idx, slice_ids)
    right = nxt.order_key if nxt is not None else None
    return _allocate_or_renumber(
        members, slice_members, left, right, before_item_id=None, after_item_id=after_item_id,
    )


def _order_keys_before_neighbor(
    members: list[MembershipRow],
    end_idx: int,
    neighbor_idx: int,
    slice_members: list[MembershipRow],
    slice_ids: set[str],
    before_item_id: str,
) -> tuple[list[tuple[str, str]], bool, bool]:
    nxt = _neighbor_after(members, end_idx, slice_ids)
    if nxt is not None and nxt.item_id == before_item_id:
        return [], False, True
    prev = _neighbor_before(members, neighbor_idx, slice_ids)
    left = prev.order_key if prev is not None else None
    right = members[neighbor_idx].order_key
    return _allocate_or_renumber(
        members, slice_members, left, right, before_item_id=before_item_id, after_item_id=None,
    )


def _allocate_or_renumber(
    members: list[MembershipRow],
    slice_members: list[MembershipRow],
    left: str | None,
    right: str | None,
    *,
    before_item_id: str | None,
    after_item_id: str | None,
) -> tuple[list[tuple[str, str]], bool, bool]:
    k = len(slice_members)
    try:
        new_keys = allocate_keys(left, right, k)
        updates = list(zip([m.item_id for m in slice_members], new_keys, strict=True))
        return updates, False, False
    except PrecisionExhausted:
        new_order = _insert_slice(
            members,
            slice_members,
            before_item_id=before_item_id,
            after_item_id=after_item_id,
        )
        updates = list(
            zip([m.item_id for m in new_order], renumbered_keys(len(new_order)), strict=True)
        )
        return updates, True, False


def _load_move_context(
    store: PlaylistStore,
    playlist_id: str,
    expected_etag: str,
    range_start: str,
    range_length: int | None,
    range_end: str | None,
    before_item_id: str | None,
    after_item_id: str | None,
) -> tuple[PlaylistRow, list[MembershipRow], int, int, list[MembershipRow], set[str], int]:
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
    _validate_move_anchors(before_item_id, after_item_id)

    start_idx, end_idx, slice_members = _resolve_slice(
        members, playlist_id, range_start, range_length, range_end,
    )
    slice_ids = {m.item_id for m in slice_members}

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

    return (
        before_row, members, start_idx, end_idx, slice_members, slice_ids, neighbor_idx,
    )


def move_memberships(
    store: PlaylistStore,
    playlist_id: str,
    *,
    slice_spec: MoveSliceSpec,
    anchor: MoveAnchor,
    expected_etag: str,
    record_edit: bool = True,
) -> MoveResult:
    """Relocate a contiguous membership slice without rewriting neighbors."""
    writer = store._writer
    (
        before_row,
        members,
        start_idx,
        end_idx,
        slice_members,
        slice_ids,
        neighbor_idx,
    ) = _load_move_context(
        store,
        playlist_id,
        expected_etag,
        slice_spec.range_start,
        slice_spec.range_length,
        slice_spec.range_end,
        anchor.before_item_id,
        anchor.after_item_id,
    )

    if anchor.after_item_id is not None:
        updates, renumbered, no_op = _order_keys_after_neighbor(
            members, start_idx, neighbor_idx, slice_members, slice_ids, anchor.after_item_id,
        )
    else:
        assert anchor.before_item_id is not None
        updates, renumbered, no_op = _order_keys_before_neighbor(
            members, end_idx, neighbor_idx, slice_members, slice_ids, anchor.before_item_id,
        )

    if no_op:
        return MoveResult(before_row, renumbered=False, no_op=True)

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
    "MoveAnchor",
    "MoveResult",
    "MoveSliceSpec",
    "SliceNotContiguousError",
    "TargetInsideSliceError",
    "move_memberships",
]
