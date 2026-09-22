"""djay playlist writer wrapper (Phase 3 <-> Phase 8 bridge).

Wraps Phase 3's ``apps.sync.playlist_apply._apply_single_op`` so the Phase 8
:class:`apps.smartlists.writers.PlaylistWriter` protocol can drive per-
smartlist ``create`` / ``update`` ops against djay's ``MediaLibrary.db``.

Design:
  * ``playlist_exists`` reads the canonical name from ``database2`` where
    collection='mediaItemPlaylists'.
  * ``create_playlist`` emits a Phase 3 plan op ("create") and applies it.
  * ``apply_diff`` resolves current membership -> target-after-diff and
    emits an "update" op.
  * stable_id -> djay_uuid resolution goes through the state-layer
    ``track_vendor_ids`` table (vendor="djay").
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import ContextManager


def _resolve_djay_uuid(
    state_conn: sqlite3.Connection, stable_id: str
) -> str | None:
    """Return the djay UUID for ``stable_id`` or None."""
    row = state_conn.execute(
        "SELECT vendor_id FROM track_vendor_ids "
        "WHERE stable_id = ? AND vendor = 'djay' LIMIT 1",
        (stable_id,),
    ).fetchone()
    return row[0] if row else None


def _resolve_stable_id_for_djay(
    state_conn: sqlite3.Connection, djay_uuid: str
) -> str | None:
    """Reverse of :func:`_resolve_djay_uuid`: djay UUID -> stable_id."""
    row = state_conn.execute(
        "SELECT stable_id FROM track_vendor_ids "
        "WHERE vendor = 'djay' AND vendor_id = ? LIMIT 1",
        (djay_uuid,),
    ).fetchone()
    return row[0] if row else None


def _find_djay_playlist(
    djay_conn: sqlite3.Connection, name: str
) -> tuple[str, list[int]] | None:
    """Find exactly one djay playlist by display name.

    This legacy name helper refuses duplicate names. New writeback calls use
    :func:`_find_djay_playlist_by_id`, never a name lookup.
    """
    from apps.sync.playlist_tsaf import parse_playlist_blob

    rows = djay_conn.execute(
        "SELECT key, data FROM database2 "
        "WHERE collection = 'mediaItemPlaylists'"
    ).fetchall()
    matches: list[str] = []
    for key, blob in rows:
        if blob is None:
            continue
        if parse_playlist_blob(bytes(blob)).get("name") == name:
            matches.append(str(key))
    if not matches:
        return None
    if len(matches) > 1:
        raise RuntimeError(f"djay: playlist name {name!r} is ambiguous; select a vendor ID")
    return _find_djay_playlist_by_id(djay_conn, matches[0])


def _find_djay_playlist_by_id(
    djay_conn: sqlite3.Connection, playlist_id: str,
) -> tuple[str, list[int]] | None:
    """Find a playlist by its native database key, never by blob substring."""
    exists = djay_conn.execute(
        "SELECT 1 FROM database2 WHERE collection = 'mediaItemPlaylists' AND key = ?",
        (playlist_id,),
    ).fetchone()
    if exists is None:
        return None
    pages = djay_conn.execute(
        'SELECT data FROM view_mediaItemPlaylistView_page WHERE "group" = ?',
        (playlist_id,),
    ).fetchall()
    from apps.sync import playlist_tsaf as ptsaf

    rowids: list[int] = []
    for (blob,) in pages:
        rowids.extend(ptsaf.parse_page_data(blob))
    return playlist_id, rowids


@dataclass
class DjayPlaylistWriter:
    """``PlaylistWriter`` that drives Phase 3 ops against djay's DB.

    Attributes
    ----------
    djay_db_path
        Path to the djay ``MediaLibrary.db`` (live or staged copy).
    state_conn
        SQLite connection to the shared state DB (for stable_id ->
        djay_uuid resolution via ``track_vendor_ids``).
    vendor
        Vendor tag surfaced in ``MaterializeResult.writers_applied``.
    leaf_type_byte
        Passed through to ``playlist_apply._apply_single_op``. None uses
        the Phase 3 default.
    """

    djay_db_path: Path
    state_conn: sqlite3.Connection
    vendor: str = "djay"
    leaf_type_byte: int | None = None
    # P1-B self-guard: callers that have already opened a
    # ``apps.sync.safety.LiveWriteSession`` (rails 1-3 run) may set this
    # to the active session — or any truthy sentinel — to acknowledge the
    # harness is wrapping this writer. If left None, ``_apply_op`` will
    # inline-re-run the djay + Rekordbox pgrep gate before every write.
    safety_session: "object | None" = None

    def _open_djay(self) -> sqlite3.Connection:
        return sqlite3.connect(
            f"file:{self.djay_db_path}?mode=rwc", uri=True,
            isolation_level=None,
        )

    def _assert_safe_to_write(self) -> None:
        """Defense-in-depth rail 2 re-check before opening rwc to djay.

        Forensics audit PR #105 flagged that every ``_open_djay`` call
        path relied solely on caller-side safety rails
        (``apps.smartlists.writers.SafePlaylistWriter`` +
        ``safe_writer_session``). A future direct caller of
        :class:`DjayPlaylistWriter` could silently bypass the process
        gate and write into ``MediaLibrary.db`` while djay Pro (or
        Rekordbox for paranoia) is running, corrupting the DB on the
        next CloudKit sync.
        """
        if self.safety_session is not None:
            return  # caller has already run the full harness
        from apps.sync.safety import SafetyAbort, assert_target_not_running

        try:
            assert_target_not_running("djay")
            assert_target_not_running("rekordbox")
        except SafetyAbort as exc:
            from apps.sync.playlist_apply import PlaylistApplyError

            raise PlaylistApplyError(
                f"DjayPlaylistWriter self-guard refuses live write: {exc}. "
                "Wrap this writer in apps.smartlists.writers.SafePlaylistWriter "
                "+ safe_writer_session(), or set safety_session= to an "
                "active LiveWriteSession to acknowledge the harness ran."
            ) from exc

    def playlist_exists(self, name: str) -> bool:
        con = self._open_djay()
        try:
            return _find_djay_playlist(con, name) is not None
        finally:
            con.close()

    def list_playlists(self):
        """Enumerate native djay playlist UUIDs for explicit target choice."""
        from apps.sync.playlist_tsaf import parse_playlist_blob
        from apps.webui.server.playlist_writeback import VendorPlaylist

        con = self._open_djay()
        try:
            rows = con.execute(
                "SELECT key, data FROM database2 WHERE collection = 'mediaItemPlaylists'"
            ).fetchall()
            return [
                VendorPlaylist(playlist_id=str(key), name=str(parsed["name"]))
                for key, blob in rows if blob is not None
                if (parsed := parse_playlist_blob(bytes(blob))).get("name") is not None
            ]
        finally:
            con.close()

    def _resolve_or_raise(self, stable_ids: list[str]) -> list[str]:
        uuids: list[str] = []
        missing: list[str] = []
        for sid in stable_ids:
            u = _resolve_djay_uuid(self.state_conn, sid)
            if u is None:
                missing.append(sid)
            else:
                uuids.append(u)
        if missing:
            raise RuntimeError(
                f"djay: no djay_uuid mapping for stable_ids "
                f"{missing[:5]}{'...' if len(missing) > 5 else ''}"
            )
        return uuids

    def _apply_op(self, op: dict) -> None:
        from apps.sync.playlist_apply import (
            PlaylistApplyError,
            _apply_single_op,
        )

        self._assert_safe_to_write()
        con = self._open_djay()
        try:
            con.execute("BEGIN IMMEDIATE")
            try:
                result = _apply_single_op(
                    con, op, leaf_type_byte=self.leaf_type_byte
                )
                if result.status == "failed":
                    con.execute("ROLLBACK")
                    raise PlaylistApplyError(result.message)
                con.execute("COMMIT")
            except PlaylistApplyError:
                raise
            except Exception:
                try:
                    con.execute("ROLLBACK")
                except sqlite3.DatabaseError:
                    pass
                raise
        finally:
            con.close()

    def create_playlist(self, name: str, track_ids: list[str]) -> None:
        # P1-B self-guard: refuse BEFORE any resolution work so a future
        # direct caller cannot silently race a live djay / Rekordbox.
        self._assert_safe_to_write()
        uuids = self._resolve_or_raise(list(track_ids))
        op = {
            "rb_id": "",
            "rb_name": name,
            "op": "create",
            "djay_uuid": None,
            "target_members": [{"djay_uuid": u} for u in uuids],
        }
        self._apply_op(op)

    def apply_diff(
        self, name: str, added: list[str], removed: list[str],
    ) -> None:
        # P1-B self-guard: refuse BEFORE opening rwc / reading djay DB.
        self._assert_safe_to_write()
        con = self._open_djay()
        try:
            found = _find_djay_playlist(con, name)
            if found is None:
                raise RuntimeError(
                    f"djay: playlist {name!r} not found; cannot apply diff"
                )
            uuid, current_rowids = found
            row_to_uuid = {
                int(rowid): key
                for rowid, key in con.execute(
                    "SELECT rowid, key FROM database2 "
                    "WHERE collection = 'mediaItemUserData'"
                )
            }
            current_uuids = [row_to_uuid.get(r, "") for r in current_rowids]
        finally:
            con.close()

        add_uuids = self._resolve_or_raise(list(added))
        remove_uuids = self._resolve_or_raise(list(removed))
        remove_set = set(remove_uuids)
        target = [u for u in current_uuids if u and u not in remove_set]
        # P08-03: dedupe while preserving order. On a force-adopt retry the
        # caller may re-submit UUIDs that still live in ``current_uuids``;
        # without this guard they would appear twice and djay surfaces them
        # as duplicate rows in the playlist.
        seen = set(target)
        for u in add_uuids:
            if u in seen:
                continue
            seen.add(u)
            target.append(u)

        op = {
            "rb_id": "",
            "rb_name": name,
            "op": "update",
            "djay_uuid": uuid,
            "target_members": [{"djay_uuid": u} for u in target],
        }
        self._apply_op(op)

    def read_members(self, name: str) -> list[str]:
        """Current membership as stable_ids, in page order.

        A row whose djay UUID has no reverse mapping in
        ``track_vendor_ids`` (a djay-side track the state layer has
        never ingested) is skipped rather than raised, mirroring
        :meth:`apps.smartlists.rb_writer.RBPlaylistWriter.read_members`.
        """
        con = self._open_djay()
        try:
            found = _find_djay_playlist(con, name)
            if found is None:
                raise RuntimeError(f"djay: playlist {name!r} not found")
            _uuid, rowids = found
            row_to_uuid = {
                int(rowid): key
                for rowid, key in con.execute(
                    "SELECT rowid, key FROM database2 "
                    "WHERE collection = 'mediaItemUserData'"
                )
            }
        finally:
            con.close()
        members: list[str] = []
        for rowid in rowids:
            djay_uuid = row_to_uuid.get(rowid)
            if djay_uuid is None:
                continue
            sid = _resolve_stable_id_for_djay(self.state_conn, djay_uuid)
            if sid is not None:
                members.append(sid)
        return members

    def read_members_by_id(self, playlist_id: str) -> list[str]:
        con = self._open_djay()
        try:
            found = _find_djay_playlist_by_id(con, playlist_id)
            if found is None:
                raise RuntimeError(f"djay: playlist ID {playlist_id!r} not found")
            _uuid, rowids = found
            row_to_uuid = {
                int(rowid): key for rowid, key in con.execute(
                    "SELECT rowid, key FROM database2 WHERE collection = 'mediaItemUserData'"
                )
            }
        finally:
            con.close()
        members: list[str] = []
        for rowid in rowids:
            djay_uuid = row_to_uuid.get(rowid)
            if djay_uuid is None:
                raise RuntimeError(f"djay: target {playlist_id!r} has an unknown member row {rowid}")
            stable_id = _resolve_stable_id_for_djay(self.state_conn, djay_uuid)
            if stable_id is None:
                raise RuntimeError(f"djay: target {playlist_id!r} has unmapped UUID {djay_uuid!r}")
            members.append(stable_id)
        return members

    def apply_with_backup_by_id(
        self, playlist_id: str, stable_members: list[str], expected_target_revision: str,
        expected_mapping_revision: str, source_transaction: Callable[[], ContextManager[None]],
    ):
        """Hold one SQLite write lock for CAS, online backup, and mutation."""
        from apps.smartlists.writeback_backup import (
            exclusive_target_lock,
            online_backup,
            write_reversal,
        )
        from apps.sync.playlist_apply import PlaylistApplyError, _apply_single_op
        from apps.webui.server.playlist_writeback import WritebackBackup, WritebackConflict

        self._assert_safe_to_write()
        with source_transaction():
            with exclusive_target_lock(self.djay_db_path):
                con = self._open_djay()
                try:
                    con.execute("BEGIN IMMEDIATE")
                    found = _find_djay_playlist_by_id(con, playlist_id)
                    if found is None:
                        raise RuntimeError(f"djay: playlist ID {playlist_id!r} not found")
                    _uuid, rowids = found
                    row_to_uuid = {int(rowid): key for rowid, key in con.execute(
                        "SELECT rowid, key FROM database2 WHERE collection = 'mediaItemUserData'"
                    )}
                    current: list[str] = []
                    native_current: list[str] = []
                    for rowid in rowids:
                        djay_uuid = row_to_uuid.get(rowid)
                        if djay_uuid is None:
                            raise RuntimeError(f"djay: target {playlist_id!r} has an unknown member row {rowid}")
                        stable_id = _resolve_stable_id_for_djay(self.state_conn, djay_uuid)
                        if stable_id is None:
                            raise RuntimeError(f"djay: target {playlist_id!r} has unmapped UUID {djay_uuid!r}")
                        current.append(stable_id)
                        native_current.append(str(djay_uuid))
                    actual = hashlib.sha256(json.dumps({"target_id": playlist_id, "members": current}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                    if actual != expected_target_revision:
                        raise WritebackConflict("djay: target revision changed before transaction")
                    mapping_rows = self.state_conn.execute(
                        "SELECT stable_id, vendor_id FROM track_vendor_ids WHERE vendor = ? AND stable_id IN (" + ",".join("?" * len(set(stable_members))) + ")",
                        (self.vendor, *dict.fromkeys(stable_members)),
                    ).fetchall() if stable_members else []
                    mapping_revision = hashlib.sha256(json.dumps(sorted((str(stable_id), str(vendor_id)) for stable_id, vendor_id in mapping_rows), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                    if mapping_revision != expected_mapping_revision:
                        raise WritebackConflict("djay: mapping changed inside vendor transaction")
                    native_mapping = {str(stable_id): str(vendor_id) for stable_id, vendor_id in mapping_rows}
                    missing = [stable_id for stable_id in stable_members if stable_id not in native_mapping]
                    if missing:
                        raise WritebackConflict(f"djay: mapping lost desired IDs inside vendor transaction: {missing[:5]}")
                    native_members = [native_mapping[stable_id] for stable_id in stable_members]
                    with sqlite3.connect(f"file:{self.djay_db_path}?mode=ro", uri=True) as snapshot:
                        backup = WritebackBackup(online_backup(snapshot, "djay"))
                    target = list(native_members)
                    post_revision = hashlib.sha256(json.dumps({"target_id": playlist_id, "members": stable_members}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                    write_reversal("djay", backup.backup_id, self.djay_db_path, playlist_id, current, native_current, post_revision)
                    result = _apply_single_op(con, {
                        "rb_id": "", "rb_name": "", "op": "update", "djay_uuid": playlist_id,
                        "target_members": [{"djay_uuid": item} for item in target],
                    }, leaf_type_byte=self.leaf_type_byte)
                    if result.status == "failed":
                        raise PlaylistApplyError(result.message)
                    con.execute("COMMIT")
                except Exception:
                    try:
                        con.execute("ROLLBACK")
                    except sqlite3.DatabaseError:
                        pass
                    raise
                finally:
                    con.close()
        return backup, post_revision

    def backup_target(self):
        """Back up exactly ``djay_db_path`` before a writeback transaction."""
        from apps.smartlists.writeback_backup import online_backup
        from apps.webui.server.playlist_writeback import WritebackBackup

        if not self.djay_db_path.exists():
            raise RuntimeError(f"djay: target disappeared before backup: {self.djay_db_path}")
        with self._open_djay() as source:
            return WritebackBackup(online_backup(source, "djay"))

    def restore_backup(self, backup_id: str, target_id: str, expected_target_revision: str) -> str:
        from apps.smartlists.writeback_backup import exclusive_target_lock, read_reversal
        from apps.sync.playlist_apply import PlaylistApplyError, _apply_single_op
        from apps.webui.server.playlist_writeback import WritebackConflict
        self._assert_safe_to_write()
        with exclusive_target_lock(self.djay_db_path):
            con = self._open_djay()
            try:
                con.execute("BEGIN IMMEDIATE")
                found = _find_djay_playlist_by_id(con, target_id)
                if found is None:
                    raise RuntimeError(f"djay: playlist ID {target_id!r} not found")
                _uuid, rowids = found
                row_to_uuid = {int(rowid): key for rowid, key in con.execute(
                    "SELECT rowid, key FROM database2 WHERE collection = 'mediaItemUserData'"
                )}
                current: list[str] = []
                for rowid in rowids:
                    djay_uuid = row_to_uuid.get(rowid)
                    if djay_uuid is None:
                        raise RuntimeError(f"djay: target {target_id!r} has an unknown member row {rowid}")
                    stable_id = _resolve_stable_id_for_djay(self.state_conn, djay_uuid)
                    if stable_id is None:
                        raise RuntimeError(f"djay: target {target_id!r} has unmapped UUID {djay_uuid!r}")
                    current.append(stable_id)
                actual = hashlib.sha256(json.dumps({"target_id": target_id, "members": current}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                if actual != expected_target_revision:
                    raise WritebackConflict("djay: rollback target revision conflict")
                stable_preimage, native_preimage = read_reversal("djay", backup_id, self.djay_db_path, target_id, expected_target_revision)
                restored_revision = hashlib.sha256(json.dumps({"target_id": target_id, "members": stable_preimage}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                result = _apply_single_op(con, {
                    "rb_id": "", "rb_name": "", "op": "update", "djay_uuid": target_id,
                    "target_members": [{"djay_uuid": item} for item in native_preimage],
                }, leaf_type_byte=self.leaf_type_byte)
                if result.status == "failed":
                    raise PlaylistApplyError(result.message)
                con.execute("COMMIT")
            except Exception:
                try:
                    con.execute("ROLLBACK")
                except sqlite3.DatabaseError:
                    pass
                raise
            finally:
                con.close()
        return restored_revision


def build_djay_writer(
    djay_db_path: Path | None = None,
    state_conn: sqlite3.Connection | None = None,
) -> DjayPlaylistWriter | None:
    """Construct a :class:`DjayPlaylistWriter` or return None on failure."""
    if djay_db_path is None:
        try:
            from apps.shared import paths

            djay_db_path = getattr(paths, "DJAY_WORKING_DB", None)
        except Exception:
            return None
    if not djay_db_path or not Path(djay_db_path).exists():
        return None
    if state_conn is None:
        try:
            from apps.shared.state.db import open_ro as _open_state_ro

            state_conn = _open_state_ro()
        except Exception:
            return None
    return DjayPlaylistWriter(
        djay_db_path=Path(djay_db_path), state_conn=state_conn
    )


__all__ = [
    "DjayPlaylistWriter",
    "build_djay_writer",
    "_resolve_djay_uuid",
    "_resolve_stable_id_for_djay",
    "_find_djay_playlist",
    "_find_djay_playlist_by_id",
]
