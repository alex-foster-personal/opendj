"""Pure membership-list helpers for cross-playlist add and move."""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Literal

from apps.shared.state.writer import StateWriter

from .backend import BackendError


def carry_edit_ids(
    old_items: list[str],
    old_edit_ids: list[str | None],
    new_items: list[str],
) -> list[str | None]:
    """Per-occurrence FIFO: each ``new_items`` row inherits the next prior edit_id for that stable_id."""
    queues: dict[str, deque[str | None]] = defaultdict(deque)
    for sid, edit_id in zip(old_items, old_edit_ids, strict=True):
        queues[sid].append(edit_id)
    out: list[str | None] = []
    for sid in new_items:
        queue = queues[sid]
        out.append(queue.popleft() if queue else None)
    return out


def dest_next(dest_items: list[str], stable_ids: list[str]) -> list[str]:
    """Set-union append: dest items, then request-order ids not already present."""
    dest_set = set(dest_items)
    next_items = list(dest_items)
    pending: set[str] = set()
    for sid in stable_ids:
        if sid in dest_set:
            continue
        if sid not in pending:
            next_items.append(sid)
            pending.add(sid)
    return next_items


def dest_next_with_edits(
    dest_items: list[str],
    dest_edit_ids: list[str | None],
    stable_ids: list[str],
) -> tuple[list[str], list[str | None]]:
    """Like :func:`dest_next`, keeping existing edit_ids and appending plain rows."""
    dest_set = set(dest_items)
    items = list(dest_items)
    edits = list(dest_edit_ids)
    pending: set[str] = set()
    for sid in stable_ids:
        if sid in dest_set:
            continue
        if sid not in pending:
            items.append(sid)
            edits.append(None)
            pending.add(sid)
    return items, edits


def source_next(source_items: list[str], stable_ids: list[str]) -> list[str]:
    """Drop every matching stable_id from source, preserving remaining order."""
    drop = set(stable_ids)
    return [sid for sid in source_items if sid not in drop]


def source_next_with_edits(
    source_items: list[str],
    source_edit_ids: list[str | None],
    stable_ids: list[str],
) -> tuple[list[str], list[str | None]]:
    """Move removes only plain (non-edit) memberships for each requested stable_id."""
    drop_plain = set(stable_ids)
    items: list[str] = []
    edits: list[str | None] = []
    for sid, edit_id in zip(source_items, source_edit_ids, strict=True):
        if sid in drop_plain and edit_id is None:
            continue
        items.append(sid)
        edits.append(edit_id)
    return items, edits


def validate_transfer(
    stable_ids: list[str],
    mode: Literal["add", "move"],
    source_id: str | None,
    source_etag: str | None,
    dest_id: str,
) -> None:
    if not stable_ids:
        raise BackendError("stable_ids must be a non-empty list")
    if mode == "move" and (source_id is None or source_etag is None):
        raise BackendError("mode=move requires source_playlist_id and source_etag")
    if source_id is not None and source_id == dest_id:
        raise BackendError("cannot transfer a playlist onto itself")


def require_source_members(source_items: list[str], stable_ids: list[str]) -> None:
    """Reject (422) a move naming any stable_id that is not a member of the source playlist."""
    present = set(source_items)
    missing = [sid for sid in dict.fromkeys(stable_ids) if sid not in present]
    if missing:
        raise BackendError(f"stable_ids not in source playlist ({len(missing)}): {missing[:5]}")


def membership_plan(
    dest_items: list[str],
    source_items: list[str] | None,
    stable_ids: list[str],
    mode: Literal["add", "move"],
    *,
    dest_edit_ids: list[str | None] | None = None,
    source_edit_ids: list[str | None] | None = None,
) -> tuple[list[str], list[str | None], list[str] | None, list[str | None] | None, bool, bool]:
    if dest_edit_ids is not None:
        dest_items_next, dest_edits_next = dest_next_with_edits(
            dest_items,
            dest_edit_ids,
            stable_ids,
        )
    else:
        dest_items_next = dest_next(dest_items, stable_ids)
        dest_edits_next = None
    if mode == "move" and source_items is not None:
        if source_edit_ids is not None:
            source_items_next, source_edits_next = source_next_with_edits(
                source_items,
                source_edit_ids,
                stable_ids,
            )
        else:
            source_items_next = source_next(source_items, stable_ids)
            source_edits_next = None
    else:
        source_items_next = list(source_items) if source_items is not None else None
        source_edits_next = list(source_edit_ids) if source_edit_ids is not None else None
    dest_unchanged = dest_items_next == list(dest_items) and (
        dest_edits_next is None or dest_edits_next == list(dest_edit_ids or [])
    )
    source_unchanged = source_items is None or (
        source_items_next == list(source_items)
        and (source_edits_next is None or source_edits_next == list(source_edit_ids or []))
    )
    return (
        dest_items_next,
        dest_edits_next,
        source_items_next,
        source_edits_next,
        dest_unchanged,
        source_unchanged,
    )


def skip_writes(mode: Literal["add", "move"], dest_unchanged: bool, source_unchanged: bool) -> bool:
    return dest_unchanged and (mode == "add" or source_unchanged)


def apply_dest_write(
    writer: StateWriter,
    dest_id: str,
    dest_items: list[str],
    dest_unchanged: bool,
    *,
    edit_ids: list[str | None] | None = None,
) -> None:
    if not dest_unchanged:
        writer.set_playlist_memberships(dest_id, dest_items, edit_ids)


def apply_source_write(
    writer: StateWriter,
    source_id: str | None,
    moved_source: list[str] | None,
    mode: Literal["add", "move"],
    source_present: bool,
    source_unchanged: bool,
    *,
    edit_ids: list[str | None] | None = None,
) -> None:
    if mode == "move" and source_present and not source_unchanged:
        assert source_id is not None
        assert moved_source is not None
        writer.set_playlist_memberships(source_id, moved_source, edit_ids)
