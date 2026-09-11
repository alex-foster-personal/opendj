"""Pure membership-list helpers for cross-playlist add and move."""

from __future__ import annotations

from typing import Literal

from apps.shared.state.writer import StateWriter

from .backend import BackendError


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


def source_next(source_items: list[str], stable_ids: list[str]) -> list[str]:
    """Drop every matching stable_id from source, preserving remaining order."""
    drop = set(stable_ids)
    return [sid for sid in source_items if sid not in drop]


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
        raise BackendError(
            "mode=move requires source_playlist_id and source_etag"
        )
    if source_id is not None and source_id == dest_id:
        raise BackendError("cannot transfer a playlist onto itself")


def membership_plan(
    dest_items: list[str],
    source_items: list[str] | None,
    stable_ids: list[str],
    mode: Literal["add", "move"],
) -> tuple[list[str], list[str] | None, bool, bool]:
    dest_items_next = dest_next(dest_items, stable_ids)
    if mode == "move" and source_items is not None:
        source_items_next: list[str] | None = source_next(source_items, stable_ids)
    else:
        source_items_next = list(source_items) if source_items is not None else None
    dest_unchanged = dest_items_next == list(dest_items)
    source_unchanged = (
        source_items is None or source_items_next == list(source_items)
    )
    return dest_items_next, source_items_next, dest_unchanged, source_unchanged


def skip_writes(
    mode: Literal["add", "move"], dest_unchanged: bool, source_unchanged: bool
) -> bool:
    return dest_unchanged and (mode == "add" or source_unchanged)


def apply_dest_write(
    writer: StateWriter, dest_id: str, dest_items: list[str], dest_unchanged: bool
) -> None:
    if not dest_unchanged:
        writer.set_playlist_memberships(dest_id, dest_items)


def apply_source_write(
    writer: StateWriter,
    source_id: str | None,
    moved_source: list[str] | None,
    mode: Literal["add", "move"],
    source_present: bool,
    source_unchanged: bool,
) -> None:
    if mode == "move" and source_present and not source_unchanged:
        assert source_id is not None
        assert moved_source is not None
        writer.set_playlist_memberships(source_id, moved_source)
