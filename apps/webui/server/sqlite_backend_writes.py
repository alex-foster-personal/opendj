"""Track, tag and pairing writes mixed into :class:`.sqlite_backend.SqliteBackend`."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from apps.shared.pairings.schema_sql import ensure_phase08_tables
from apps.shared.state import db as _state_db
from apps.shared.state.writer import StateWriter

from .backend import (
    AppliedState,
    BackendError,
    BatchConflictError,
    CommittedReadbackError,
    ConflictError,
    InMemoryBackend,
    MyTagMergeConfirmationRequiredError,
    MyTagScopeConflictError,
    NotFoundError,
    Pairing,
    Source,
    Track,
    TrackUpdate,
    artists_from_json,
    compute_mytag_catalog_revision,
    identity_patch,
    tag_cas_conflicts,
)
from .etag import compute_etag, strip_quotes
from .pairings_sqlite import (
    create_http_pairing,
    delete_http_pairing,
)
from .sqlite_backend_rows import (
    _applied_state_in_txn,
    _delete_graph_pairing,
    _fetch_fields,
    _field_writes,
    _row_to_track,
    _utcnow_iso,
)


class _WritesMixin:
    """Track, tag and pairing writes of :class:`SqliteBackend`."""

    if TYPE_CHECKING:
        _path: Path
        _fallback: InMemoryBackend
        _write_lock: threading.RLock
        _sqlite_last_writer: tuple[str, str] | None

        def get_track(self, stable_id: str) -> Track: ...

    # --- writes -----------------------------------------------------------
    def update_track(
        self,
        stable_id: str,
        patch: dict[str, Any],
        *,
        expected_etag: str,
        source: Source = "webui",
        mutation_guard: Callable[[], None] | None = None,
    ) -> Track:
        """Persist a rating / notes / tags patch through ``StateWriter``.

        Contract (ratings-persistence-restart):

          * CAS: ``expected_etag`` must match the etag derived from the
            current effective ``updated_at`` or ``ConflictError`` raises.
          * Each changed field lands in ``track_fields`` with provenance
            ``source=<source>`` (webui for UI PATCHes), ``confidence=1.0``
            and one ``track.field.set`` event row -- via the Phase 5
            single-writer chokepoint, never direct SQL.
          * No in-memory fallback: a locked DB / writer conflict raises
            ``sqlite3.OperationalError`` after the 5s busy_timeout.
          * A fresh backend instance (daemon restart) re-reads the same
            values and re-derives the same etag.
        """
        try:
            return self.update_tracks(
                [TrackUpdate(stable_id, patch, expected_etag)],
                source=source,
                mutation_guard=mutation_guard,
            )[0]
        except BatchConflictError as exc:
            current = self.get_track(stable_id)
            raise ConflictError(current.to_dict(), exc.conflicts[0]["current_etag"]) from exc

    def update_tracks(
        self,
        updates: Sequence[TrackUpdate],
        *,
        source: Source = "webui",
        mutation_guard: Callable[[], None] | None = None,
        applied_state: dict[str, AppliedState] | None = None,
    ) -> list[Track]:
        """Atomically compare-and-swap and persist every requested update.

        The complete ETag preflight and all mutations happen inside one
        ``StateWriter.transaction``. Nested ``_tx`` SAVEPOINTs compose with
        that unit, so a later write failure cannot leave earlier rows
        committed, and bus events flush only after COMMIT.
        """
        if not updates:
            return []
        stable_ids = [update.stable_id for update in updates]
        if len(stable_ids) != len(set(stable_ids)):
            raise BackendError("batch contains duplicate stable_ids")

        with self._write_lock:
            conn = _state_db.open_rw(self._path)
            conn.row_factory = sqlite3.Row
            try:
                with StateWriter(conn, actor=source) as writer, writer.transaction():
                    current_rows: list[Track] = []
                    conflicts: list[dict[str, str]] = []
                    for update in updates:
                        row = conn.execute(
                            "SELECT stable_id, stable_id_tier, title, artists_json, album, isrc, "
                            "duration_ms, file_path, content_hash, created_at, updated_at "
                            "FROM tracks WHERE stable_id = ?",
                            (update.stable_id,),
                        ).fetchone()
                        if row is None:
                            raise NotFoundError(f"track not found: {update.stable_id}")
                        fields = _fetch_fields(conn, [update.stable_id])
                        current = _row_to_track(row, fields.get(update.stable_id, {}))
                        current_rows.append(current)
                        current_etag = compute_etag(current.stable_id, current.updated_at, current.selection_tag)
                        if strip_quotes(current_etag) != strip_quotes(update.expected_etag) or tag_cas_conflicts(
                            update, current
                        ):
                            conflicts.append(
                                {
                                    "stable_id": update.stable_id,
                                    "current_etag": current_etag,
                                }
                            )
                    if conflicts:
                        raise BatchConflictError(conflicts)

                    writes = [
                        _field_writes(current, update.patch)
                        for current, update in zip(current_rows, updates, strict=False)
                    ]
                    identity_patches = [
                        identity_patch(
                            update.patch,
                            allow_blank_title=update.allow_blank_title,
                            allow_blank_artist_members=update.allow_blank_artist_members,
                        )
                        for update in updates
                    ]
                    now = datetime.now(UTC).isoformat()
                    if mutation_guard is not None:
                        mutation_guard()
                    for current, update, field_writes, identity in zip(
                        current_rows,
                        updates,
                        writes,
                        identity_patches,
                        strict=False,
                    ):
                        for field_name, value in field_writes.items():
                            writer.set_field(
                                update.stable_id,
                                field_name,
                                value,
                                source=source,
                                modified_at=now,
                                confidence=1.0,
                            )
                        if identity:
                            row = conn.execute(
                                "SELECT stable_id_tier, title, artists_json, album, isrc, "
                                "duration_ms, file_path, content_hash, audio_hash FROM tracks "
                                "WHERE stable_id = ? AND deleted_at IS NULL",
                                (current.stable_id,),
                            ).fetchone()
                            if row is None:
                                raise NotFoundError(f"track not found: {current.stable_id}")
                            writer.upsert_track(
                                stable_id=current.stable_id,
                                stable_id_tier=row["stable_id_tier"],
                                title=identity.get("title", row["title"]),
                                artists=identity.get("artists", artists_from_json(row["artists_json"])),
                                album=row["album"],
                                isrc=row["isrc"],
                                duration_ms=row["duration_ms"],
                                file_path=identity.get("file_path", row["file_path"]),
                                content_hash=row["content_hash"],
                                audio_hash=row["audio_hash"],
                            )
                    if applied_state is not None:
                        applied_state.update(_applied_state_in_txn(conn, [u.stable_id for u in updates]))
                    if mutation_guard is not None:
                        mutation_guard()
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

            self._sqlite_last_writer = (source, now)
            try:
                return [self.get_track(update.stable_id) for update in updates]
            except Exception as exc:
                raise CommittedReadbackError(f"the write committed but re-reading its result failed: {exc}") from exc

    def update_tag_members(
        self,
        old_name: str,
        new_name: str | None,
        *,
        expected_catalog_revision: str,
        expected_track_count: int,
        confirm_merge: bool = False,
        source: Source = "webui",
    ) -> int:
        """Atomically rename or delete every current member of a tag.

        Discovery occurs only after ``StateWriter.transaction`` has acquired
        the SQLite writer lock. The discovered ETags are then prechecked
        before any field write, so a tag added between an HTTP request
        arriving and this transaction acquiring the lock is part of the
        same sweep.
        """
        with self._write_lock:
            conn = _state_db.open_rw(self._path)
            conn.row_factory = sqlite3.Row
            try:
                with StateWriter(conn, actor="webui") as writer, writer.transaction():
                    rows = list(
                        conn.execute(
                            "SELECT stable_id, title, artists_json, album, duration_ms, "
                            "file_path, created_at, updated_at FROM tracks "
                            "WHERE deleted_at IS NULL ORDER BY stable_id",
                        )
                    )
                    fields_by_id = _fetch_fields(conn, [row["stable_id"] for row in rows])
                    tracks = [_row_to_track(row, fields_by_id.get(row["stable_id"], {})) for row in rows]
                    catalog_revision = compute_mytag_catalog_revision(tracks)
                    members = [track for track in tracks if old_name in track.tags]
                    if catalog_revision != expected_catalog_revision or len(members) != expected_track_count:
                        raise MyTagScopeConflictError(catalog_revision, len(members))
                    if new_name is not None and any(new_name in track.tags for track in tracks) and not confirm_merge:
                        raise MyTagMergeConfirmationRequiredError(new_name)
                    updates = [
                        TrackUpdate(
                            track.stable_id,
                            {"tags_remove": [old_name]}
                            if new_name is None
                            else {
                                "tags_add": [new_name],
                                "tags_remove": [old_name],
                            },
                            compute_etag(track.stable_id, track.updated_at, track.selection_tag),
                        )
                        for track in members
                    ]

                    conflicts: list[dict[str, str]] = []
                    for track, update in zip(members, updates, strict=False):
                        current_etag = compute_etag(track.stable_id, track.updated_at, track.selection_tag)
                        if strip_quotes(current_etag) != strip_quotes(update.expected_etag):
                            conflicts.append({"stable_id": track.stable_id, "current_etag": current_etag})
                    if conflicts:
                        raise BatchConflictError(conflicts)

                    now = datetime.now(UTC).isoformat()
                    for track, update in zip(members, updates, strict=False):
                        for field_name, value in _field_writes(track, update.patch).items():
                            writer.set_field(
                                update.stable_id,
                                field_name,
                                value,
                                source=source,
                                modified_at=now,
                                confidence=1.0,
                            )
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()
            if updates:
                self._sqlite_last_writer = (source, now)
            return len(updates)

    @contextmanager
    def _pairings_rw(self) -> Iterator[StateWriter]:
        """One serialized ``StateWriter`` unit over the durable pairings table.

        The table belongs to :mod:`apps.shared.pairings` (the same rows the
        ``apps.pairings`` CLI and the suggester's stage-2 rerank read), so its
        own idempotent DDL creates or upgrades it before the write. Graph
        edge writes join the yielded writer's unit, so each appends its
        ``pairing.*`` events row in the same transaction.
        """
        with self._write_lock:
            conn = _state_db.open_rw(self._path)
            try:
                ensure_phase08_tables(conn)
                with StateWriter(conn, actor="webui") as writer, writer.transaction():
                    yield writer
            finally:
                conn.close()

    def create_pairing(self, pairing: Pairing) -> Pairing:
        """Persist ``pairing``; a repeat of the same edge merges into it.

        The HTTP row keeps the wire id, direction and write-once snapshot.
        The same transaction mirrors an edge this pairing owns into the
        durable graph table. Notes append, and a snapshot already stored is
        kept. Same contract as :meth:`InMemoryBackend.create_pairing`. A
        direct edge this pairing does not own (CLI or other graph tooling)
        is left unchanged. A reverse-stored edge that reads as this capture
        is updated in place and the returned id is that stored key's id.
        """
        with self._pairings_rw() as writer:
            conn = writer.raw_conn
            conn.row_factory = sqlite3.Row
            created = create_http_pairing(conn, pairing, writer=writer)
        self._sqlite_last_writer = (pairing.source, pairing.created_at)
        return created

    def delete_pairing(self, pairing_id: str, *, expected_etag: str) -> None:
        with self._pairings_rw() as writer:
            conn = writer.raw_conn
            conn.row_factory = sqlite3.Row
            try:
                delete_http_pairing(
                    conn,
                    pairing_id,
                    expected_etag=expected_etag,
                    writer=writer,
                )
            except NotFoundError:
                _delete_graph_pairing(
                    conn,
                    pairing_id,
                    expected_etag=expected_etag,
                    writer=writer,
                )
        self._sqlite_last_writer = ("webui", _utcnow_iso())

    def last_writer(self) -> tuple[str, str] | None:
        if self._sqlite_last_writer is not None:
            return self._sqlite_last_writer
        return self._fallback.last_writer()
