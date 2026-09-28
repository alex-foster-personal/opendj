"""Playlist WRITE store over the shared state DB (``data/state/state.db``).

CONTRACT -- playlists-router lane owner (see also ``routes/playlist_write``)
=============================================================================
Mutations go through :class:`PlaylistStore` -> :class:`StateWriter` (events +
bus fanout, never raw INSERTs). :class:`PlaylistRow` is the write read-model;
etag = ``compute_etag(playlist_id, updated_at)``. Stale ``If-Match`` ->
:class:`ConflictError` (409).

Membership primitives: ``PUT .../tracks`` full replace; ``POST .../items:add``
O(1) insert; ``DELETE .../items/{item_id}`` O(1) remove; ``POST .../items:move``
O(k) slice reorder (LIBM-22). One store lock + rw connection per process.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from apps.shared.state import db as _state_db
from apps.shared.state.events import EventBus, FakeEventBus
from apps.shared.state.writer import StateWriter, compute_playlist_id
from apps.shared.state.writer_playlists import (
    PlaylistNotDeletedError,
    PlaylistNotFoundError,
)

from .backend import BackendError, ConflictError, NotFoundError
from .etag import compute_etag, strip_quotes
from .playlist_add import MEMBERSHIP_ORDER_BY, AlreadyExistsError
from .playlist_add import add_memberships as _add_memberships
from .playlist_dupes import first_repeated_stable_id
from .playlist_history import (
    HISTORY_LIMIT,
    PlaylistEditCommand,
    PlaylistHistoryEmptyError,
    PlaylistSnapshot,
    invert,
    label_for,
    rebuild_stack,
    snapshots_match,
)
from .playlist_move import MoveResult
from .playlist_move import move_memberships as _move_memberships
from .playlist_remove import (
    apply_membership_snapshot,
)
from .playlist_remove import (
    remove_memberships as _remove_memberships,
)
from .playlist_transfer import (
    apply_dest_write,
    apply_source_write,
    membership_plan,
    skip_writes,
    validate_transfer,
)

WEBUI_VENDOR: str = "webui"
"""Vendor tag for playlists created through the web UI / agent API."""


@dataclass
class PlaylistRow:
    """Post-write read model returned by every mutating call."""
    playlist_id: str
    name: str
    vendor: str
    vendor_pl_id: str
    items: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    forbid_duplicates: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def etag(self) -> str:
        return compute_etag(self.playlist_id, self.updated_at)


class PlaylistStore:
    """Serialised playlist mutation facade. See module docstring for contract."""

    def __init__(
        self,
        state_db_path: str | Path,
        *,
        bus: EventBus | FakeEventBus | None = None,
        actor: str = "webui",
        clock: Callable[..., Any] | None = None,
    ) -> None:
        path = Path(state_db_path)
        if not path.is_file():
            raise FileNotFoundError(
                f"state DB not found at {path}; playlist writes require an "
                f"initialised state.db (python -m apps.shared.state.cli init)"
            )
        # Single connection, serialised by self._lock; FastAPI's sync
        # executor calls in from varying threadpool threads, hence the
        # check_same_thread opt-out (safe because every public method
        # takes the lock).
        self._conn: sqlite3.Connection = _state_db.open_rw(
            path, check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._actor = actor
        self._writer = StateWriter(self._conn, bus, clock=clock, actor=actor)
        self._lock = threading.RLock()
        self._closed = False
        self._stack: list[PlaylistEditCommand] = []
        self._cursor = 0
        self._rebuild_from_events()

    # --- lifecycle --------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._writer.close()
            self._conn.close()

    def __enter__(self) -> PlaylistStore:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # --- internal helpers -------------------------------------------------
    def _load(self, playlist_id: str) -> PlaylistRow:
        row = self._load_header(playlist_id)
        row.items = [
            r[0] for r in self._conn.execute(
                "SELECT stable_id FROM playlist_memberships "
                f"WHERE playlist_id = ? AND deleted_at IS NULL "
                f"ORDER BY {MEMBERSHIP_ORDER_BY}",
                (playlist_id,),
            )
        ]
        return row

    def _load_header(self, playlist_id: str) -> PlaylistRow:
        """The live playlist row with ``items`` still EMPTY, or NotFoundError.

        Split from :meth:`_load` so a write that must not read every member
        (the ``:add`` path, #3963) can read the header alone.
        """
        row = self._conn.execute(
            "SELECT playlist_id, name, vendor, vendor_pl_id, "
            "       created_at, updated_at, forbid_duplicates "
            "FROM playlists WHERE playlist_id = ? AND deleted_at IS NULL",
            (playlist_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"playlist not found: {playlist_id}")
        return PlaylistRow(
            playlist_id=row["playlist_id"], name=row["name"],
            vendor=row["vendor"], vendor_pl_id=row["vendor_pl_id"],
            items=[], created_at=row["created_at"],
            updated_at=row["updated_at"],
            forbid_duplicates=bool(row["forbid_duplicates"]),
        )

    def _check_etag(self, row: PlaylistRow, expected_etag: str) -> None:
        if strip_quotes(row.etag) != strip_quotes(expected_etag):
            raise ConflictError(current=row.to_dict(), etag=row.etag)

    def _validated_name(self, name: str) -> str:
        cleaned = name.strip()
        if not cleaned:
            raise BackendError("playlist name must be a non-empty string")
        return cleaned

    def _snapshot(self, row: PlaylistRow) -> PlaylistSnapshot:
        return PlaylistSnapshot(
            playlist_id=row.playlist_id, name=row.name,
            vendor=row.vendor, vendor_pl_id=row.vendor_pl_id,
            items=list(row.items),
        )

    def _try_load(self, playlist_id: str) -> PlaylistRow | None:
        try:
            return self._load(playlist_id)
        except NotFoundError:
            return None

    def _rebuild_from_events(self) -> None:
        rows = self._conn.execute(
            "SELECT kind, payload_json FROM events "
            "WHERE actor = ? AND kind IN "
            "('playlist.edit','playlist.undo','playlist.redo') "
            "ORDER BY id",
            (self._actor,),
        ).fetchall()
        events = [
            {"kind": row["kind"], "payload": json.loads(row["payload_json"])}
            for row in rows
        ]
        self._stack, self._cursor = rebuild_stack(events)

    def _record_edit(
        self,
        op: str,
        playlist_id: str,
        before: PlaylistSnapshot | None,
        after: PlaylistSnapshot | None,
    ) -> None:
        command = PlaylistEditCommand(
            command_id=uuid.uuid4().hex,
            op=op,  # type: ignore[arg-type]
            playlist_id=playlist_id,
            ts=self._writer._now_iso(),
            before=before,
            after=after,
        )
        self._writer.append_playlist_history("playlist.edit", command.to_dict())
        self._stack = self._stack[: self._cursor]
        self._stack.append(command)
        if len(self._stack) > HISTORY_LIMIT:
            self._stack = self._stack[-HISTORY_LIMIT:]
        self._cursor = len(self._stack)

    def _conflict(self, live: PlaylistRow | None) -> None:
        raise ConflictError(
            current=live.to_dict() if live is not None else {},
            etag=live.etag if live is not None else '""',
        )

    def _restore_playlist(self, snap: PlaylistSnapshot) -> PlaylistRow:
        with self._writer.playlist_transaction():
            self._writer.insert_playlist(
                playlist_id=snap.playlist_id, name=snap.name,
                vendor=snap.vendor, vendor_pl_id=snap.vendor_pl_id,
            )
            self._writer.set_playlist_memberships(
                snap.playlist_id, list(snap.items),
            )
            return self._load(snap.playlist_id)

    def _apply_inverse(
        self,
        op: str,
        snap: PlaylistSnapshot | None,
        live: PlaylistRow | None,
    ) -> PlaylistRow | None:
        if op == "delete":
            if live is not None:
                self.delete_playlist(
                    live.playlist_id, expected_etag=live.etag, record_edit=False,
                )
            return None
        if snap is None:
            raise BackendError(f"{op} inverse is missing the snapshot")
        if op == "create":
            return self._restore_playlist(snap)
        if live is None:
            raise BackendError(f"{op} inverse is missing live row")
        if op == "rename":
            return self.rename_playlist(
                live.playlist_id, snap.name,
                expected_etag=live.etag, record_edit=False,
            )
        if op == "memberships":
            return apply_membership_snapshot(self, snap, live)
        raise BackendError(f"unknown playlist history op: {op}")

    def _forward_op(self, command: PlaylistEditCommand) -> str:
        if command.op in ("create", "duplicate"):
            return "create"
        return command.op

    def _require_known_tracks(self, stable_ids: list[str]) -> None:
        """Fail fast (422) when any stable_id has no ``tracks`` row."""
        unique = list(dict.fromkeys(stable_ids))
        known: set[str] = set()
        chunk = 500
        for i in range(0, len(unique), chunk):
            sub = unique[i : i + chunk]
            placeholders = ",".join("?" * len(sub))
            known.update(
                r[0] for r in self._conn.execute(
                    f"SELECT stable_id FROM tracks "
                    f"WHERE stable_id IN ({placeholders}) AND deleted_at IS NULL",
                    tuple(sub),
                )
            )
        missing = [sid for sid in unique if sid not in known]
        if missing:
            raise BackendError(
                f"unknown stable_ids ({len(missing)}): {missing[:5]}"
            )

    # --- mutations --------------------------------------------------------
    def create_playlist(
        self, name: str, *, record_edit: bool = True,
    ) -> PlaylistRow:
        """Create an empty playlist owned by the ``webui`` vendor."""
        cleaned = self._validated_name(name)
        with self._lock:
            with self._writer.playlist_transaction():
                vendor_pl_id = uuid.uuid4().hex
                playlist_id = compute_playlist_id(WEBUI_VENDOR, vendor_pl_id)
                self._writer.insert_playlist(
                    playlist_id=playlist_id, name=cleaned,
                    vendor=WEBUI_VENDOR, vendor_pl_id=vendor_pl_id,
                )
                row = self._load(playlist_id)
                if record_edit:
                    self._record_edit("create", playlist_id, None, self._snapshot(row))
                return row

    def update_playlist(
        self,
        playlist_id: str,
        *,
        expected_etag: str,
        name: str | None = None,
        forbid_duplicates: bool | None = None,
        record_edit: bool = True,
    ) -> PlaylistRow:
        """Rename and/or toggle forbid_duplicates."""
        cleaned = self._validated_name(name) if name is not None else None
        with self._lock:
            with self._writer.playlist_transaction():
                row = self._load(playlist_id)
                self._check_etag(row, expected_etag)
                changed = False
                if cleaned is not None:
                    changed = self._writer.insert_playlist(
                        playlist_id=playlist_id, name=cleaned,
                        vendor=row.vendor, vendor_pl_id=row.vendor_pl_id,
                    ) or changed
                if forbid_duplicates is not None:
                    changed = (
                        self._writer.set_playlist_forbid_duplicates(
                            playlist_id, forbid_duplicates,
                        )
                        or changed
                    )
                if not changed:
                    return row
                new_row = self._load(playlist_id)
                if record_edit and cleaned is not None:
                    self._record_edit(
                        "rename", playlist_id,
                        self._snapshot(row), self._snapshot(new_row),
                    )
                return new_row

    def rename_playlist(
        self, playlist_id: str, name: str, *, expected_etag: str,
        record_edit: bool = True,
    ) -> PlaylistRow:
        """Rename; renaming to the current name is a no-op (same etag)."""
        return self.update_playlist(
            playlist_id,
            expected_etag=expected_etag,
            name=name,
            record_edit=record_edit,
        )

    def delete_playlist(
        self, playlist_id: str, *, expected_etag: str,
        record_edit: bool = True,
    ) -> None:
        with self._lock:
            with self._writer.playlist_transaction():
                row = self._load(playlist_id)
                self._check_etag(row, expected_etag)
                deleted = self._writer.delete_playlist(playlist_id)
                if not deleted:  # pragma: no cover - guarded by the lock
                    raise NotFoundError(f"playlist not found: {playlist_id}")
                if record_edit:
                    self._record_edit(
                        "delete", playlist_id, self._snapshot(row), None,
                    )

    def undelete_playlist(self, playlist_id: str) -> PlaylistRow:
        with self._lock:
            with self._writer.playlist_transaction():
                try:
                    self._writer.undelete_playlist(playlist_id)
                except PlaylistNotFoundError as exc:
                    raise NotFoundError(f"playlist not found: {playlist_id}") from exc
                except PlaylistNotDeletedError:
                    raise
                return self._load(playlist_id)

    def duplicate_playlist(
        self,
        playlist_id: str,
        *,
        name: str | None = None,
        expected_etag: str | None = None,
        record_edit: bool = True,
    ) -> PlaylistRow:
        """Copy name (or ``name``) + full membership into a new webui playlist.

        ``expected_etag`` is optional (POST creates a new resource); when
        provided it is CAS-checked against the SOURCE playlist so callers
        can guarantee they duplicated the version they were looking at.
        """
        with self._lock:
            with self._writer.playlist_transaction():
                source = self._load(playlist_id)
                if expected_etag is not None:
                    self._check_etag(source, expected_etag)
                new_name = self._validated_name(
                    name if name is not None else f"{source.name} (copy)"
                )
                vendor_pl_id = uuid.uuid4().hex
                new_id = compute_playlist_id(WEBUI_VENDOR, vendor_pl_id)
                self._writer.insert_playlist(
                    playlist_id=new_id, name=new_name,
                    vendor=WEBUI_VENDOR, vendor_pl_id=vendor_pl_id,
                )
                if source.forbid_duplicates:
                    self._writer.set_playlist_forbid_duplicates(
                        new_id, True,
                    )
                if source.items:
                    self._writer.set_playlist_memberships(new_id, list(source.items))
                copy = self._load(new_id)
                if record_edit:
                    self._record_edit(
                        "duplicate", new_id, None, self._snapshot(copy),
                    )
                return copy

    def replace_memberships(
        self, playlist_id: str, stable_ids: list[str], *, expected_etag: str,
        record_edit: bool = True,
    ) -> PlaylistRow:
        """Full membership replace = add/remove/reorder in one idempotent op.

        Replaying the identical list is a true no-op: no event, no
        ``updated_at`` bump, same etag back.
        """
        with self._lock:
            with self._writer.playlist_transaction():
                row = self._load(playlist_id)
                self._check_etag(row, expected_etag)
                self._require_known_tracks(stable_ids)
                if row.forbid_duplicates:
                    repeated = first_repeated_stable_id(stable_ids)
                    if repeated is not None:
                        raise AlreadyExistsError(repeated, playlist_id)
                if list(row.items) == list(stable_ids):
                    return row
                self._writer.set_playlist_memberships(playlist_id, list(stable_ids))
                new_row = self._load(playlist_id)
                if record_edit:
                    self._record_edit(
                        "memberships", playlist_id,
                        self._snapshot(row), self._snapshot(new_row),
                    )
                return new_row

    def add_memberships(
        self,
        playlist_id: str,
        stable_ids: list[str],
        *,
        position: int | None = None,
        record_edit: bool = True,
    ) -> PlaylistRow:
        """O(1) append/insert via ``:add`` (LIBM-20)."""
        with self._lock:
            return _add_memberships(
                self, playlist_id, stable_ids,
                position=position, record_edit=record_edit,
            )

    def remove_memberships(
        self,
        playlist_id: str,
        item_ids: list[str],
        *,
        record_edit: bool = True,
    ) -> PlaylistRow:
        """O(1) remove via POST :remove or DELETE by item_id (LIBM-21)."""
        with self._lock:
            return _remove_memberships(
                self, playlist_id, item_ids, record_edit=record_edit,
            )

    def remove_membership(
        self, playlist_id: str, item_id: str, *, record_edit: bool = True,
    ) -> PlaylistRow:
        """O(1) remove via DELETE by item_id (LIBM-21)."""
        return self.remove_memberships(
            playlist_id, [item_id], record_edit=record_edit,
        )

    def move_memberships(
        self,
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
        """O(k) slice reorder via ``:move`` (LIBM-22)."""
        from .playlist_move import MoveAnchor, MoveSliceSpec

        with self._lock:
            return _move_memberships(
                self,
                playlist_id,
                slice_spec=MoveSliceSpec(range_start, range_length, range_end),
                anchor=MoveAnchor(before_item_id, after_item_id),
                expected_etag=expected_etag,
                record_edit=record_edit,
            )

    def transfer_memberships(
        self,
        dest_id: str,
        stable_ids: list[str],
        *,
        dest_etag: str,
        mode: Literal["add", "move"],
        source_id: str | None = None,
        source_etag: str | None = None,
    ) -> tuple[PlaylistRow, PlaylistRow | None]:
        """Atomic cross-playlist add (copy) or move of track identities.

        Add appends missing stable_ids to dest (set-union, request-order).
        Move also removes every matching stable_id from source. Both writes
        happen inside one ``playlist_transaction`` so an interrupt rolls back.
        """
        validate_transfer(stable_ids, mode, source_id, source_etag, dest_id)

        with self._lock:
            with self._writer.playlist_transaction():
                self._require_known_tracks(stable_ids)
                dest = self._load(dest_id)
                self._check_etag(dest, dest_etag)

                source: PlaylistRow | None = None
                if source_id is not None:
                    source = self._load(source_id)
                    if source_etag is not None:
                        self._check_etag(source, source_etag)

                source_items = list(source.items) if source is not None else None
                dest_items, moved_source, dest_unchanged, source_unchanged = (
                    membership_plan(list(dest.items), source_items, stable_ids, mode)
                )
                if skip_writes(mode, dest_unchanged, source_unchanged):
                    return dest, source if mode == "move" else None

                apply_dest_write(self._writer, dest_id, dest_items, dest_unchanged)
                apply_source_write(
                    self._writer,
                    source_id,
                    moved_source,
                    mode,
                    source is not None,
                    source_unchanged,
                )
                dest_row = self._load(dest_id)
                source_row = (
                    self._load(source_id) if mode == "move" and source_id else None
                )
                return dest_row, source_row

    # --- reads (for router symmetry) --------------------------------------
    def get_playlist_row(self, playlist_id: str) -> PlaylistRow:
        with self._lock:
            return self._load(playlist_id)

    def verify_etag(self, playlist_id: str, expected_etag: str) -> PlaylistRow:
        """CAS-check without writing; raises ConflictError on mismatch."""
        with self._lock:
            row = self._load(playlist_id)
            self._check_etag(row, expected_etag)
            return row

    def history(self) -> dict[str, Any]:
        """Live undo window: entries oldest-first, cursor at next-redo."""
        with self._lock:
            return {
                "cursor": self._cursor,
                "limit": HISTORY_LIMIT,
                "can_undo": self._cursor > 0,
                "can_redo": self._cursor < len(self._stack),
                "entries": [
                    {
                        "command_id": cmd.command_id,
                        "op": cmd.op,
                        "playlist_id": cmd.playlist_id,
                        "ts": cmd.ts,
                        "label": label_for(cmd),
                    }
                    for cmd in self._stack
                ],
            }

    def undo(self) -> tuple[PlaylistEditCommand, PlaylistRow | None]:
        with self._lock:
            if self._cursor <= 0:
                raise PlaylistHistoryEmptyError("undo")
            command = self._stack[self._cursor - 1]
            live = self._try_load(command.playlist_id)
            if not snapshots_match(
                None if live is None else self._snapshot(live), command.after,
            ):
                self._conflict(live)
            op, snap = invert(command)
            current = self._apply_inverse(op, snap, live)
            self._writer.append_playlist_history(
                "playlist.undo", {"command_id": command.command_id},
            )
            self._cursor -= 1
            return command, current

    def redo(self) -> tuple[PlaylistEditCommand, PlaylistRow | None]:
        with self._lock:
            if self._cursor >= len(self._stack):
                raise PlaylistHistoryEmptyError("redo")
            command = self._stack[self._cursor]
            live = self._try_load(command.playlist_id)
            if not snapshots_match(
                None if live is None else self._snapshot(live), command.before,
            ):
                self._conflict(live)
            current = self._apply_inverse(
                self._forward_op(command), command.after, live,
            )
            self._writer.append_playlist_history(
                "playlist.redo", {"command_id": command.command_id},
            )
            self._cursor += 1
            return command, current


__all__ = ["WEBUI_VENDOR", "PlaylistRow", "PlaylistStore"]
