"""RB playlist writer wrapper (Phase 3 <-> Phase 8 bridge).

The Phase 8 materialiser speaks the :class:`apps.smartlists.writers.PlaylistWriter`
protocol (per-smartlist ``create_playlist`` / ``apply_diff`` ops keyed by
``stable_id`` tracks). Phase 3's underlying write-path is
``pyrekordbox.Rekordbox6Database`` which creates / modifies playlists in
the RB master.db directly.

This wrapper:

1. Resolves ``stable_id`` -> RB ``ContentID`` via the state-layer
   ``track_vendor_ids`` table (vendor="rekordbox").
2. Dispatches to ``db.create_playlist`` / ``db.add_to_playlist`` /
   ``db.remove_from_playlist`` for the actual mutation.
3. Commits per-call so a failure on one smartlist doesn't leave the DB in
   a half-written state.

Construction is lazy: callers pass factory callables so unit tests can
inject fakes without touching a live RB DB.

Safety rails (Phase 1 six-rail pattern) for live writes
-------------------------------------------------------
When ``live=True``, the writer refuses to mutate until:

1. ``pgrep -if rekordbox`` returns no running Rekordbox process
   (pyrekordbox holds an exclusive handle; writing while the app is
   open would corrupt master.db).
2. A timestamped copy of ``master.db`` has been stored under
   ``data/reconcile/backups/`` (mirrors :mod:`apps.reconcile.apply`).

Both guards run lazily once per writer instance, right before the first
mutation; repeated ``create_playlist`` / ``apply_diff`` calls on the
same writer reuse the cached backup so a multi-smartlist run takes a
single copy.
"""
from __future__ import annotations

import datetime as _dt
import datetime
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
from uuid import uuid4
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, ContextManager

from apps.shared import paths

# Default backup directory for live RB DB copies taken before smartlist
# playlist mutations. Mirrors apps.reconcile.apply's backup location so
# operators have one place to look.
DEFAULT_BACKUP_DIR: Path = paths.DATA_DIR / "reconcile" / "backups"


def _resolve_rb_id(state_conn: sqlite3.Connection, stable_id: str) -> str | None:
    """Return the rekordbox ``ContentID`` for ``stable_id`` or None."""
    row = state_conn.execute(
        "SELECT vendor_id FROM track_vendor_ids "
        "WHERE stable_id = ? AND vendor = 'rekordbox' LIMIT 1",
        (stable_id,),
    ).fetchone()
    return row[0] if row else None


def _resolve_stable_id_for_rb(
    state_conn: sqlite3.Connection, content_id: str,
) -> str | None:
    """Reverse of :func:`_resolve_rb_id`: ``ContentID`` -> stable_id."""
    row = state_conn.execute(
        "SELECT stable_id FROM track_vendor_ids "
        "WHERE vendor = 'rekordbox' AND vendor_id = ? LIMIT 1",
        (content_id,),
    ).fetchone()
    return row[0] if row else None


def _rekordbox_running() -> bool:
    """True if any process matches ``rekordbox`` via ``pgrep -if``.

    Matches the implementation in :mod:`apps.reconcile.apply` so the
    lock-safety behaviour is consistent across all live-DB writers.
    When ``pgrep`` is unavailable (non-POSIX host) we conservatively
    return False and let the caller emit a warning.
    """
    try:
        r = subprocess.run(
            ["pgrep", "-if", "rekordbox"],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        print(
            "warn: pgrep not available; cannot verify Rekordbox is closed.",
            file=sys.stderr,
        )
        return False
    return r.returncode == 0 and bool(r.stdout.strip())


def _backup_rb_db(
    live_db: Path = paths.REKORDBOX_LIVE_DB,
    backup_dir: Path = DEFAULT_BACKUP_DIR,
) -> Path:
    """Copy ``master.db`` to a timestamped file under ``backup_dir``.

    Returns the backup Path. Raises :class:`RuntimeError` if the backup
    file ends up empty (indicates a copy failure).
    """
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = _dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    dst = backup_dir / f"master.{ts}.smartlists.db"
    shutil.copy2(live_db, dst)
    size = dst.stat().st_size
    if size <= 0:
        raise RuntimeError(f"RB backup failed: {dst} has size {size}")
    return dst


@dataclass
class RBPlaylistWriter:
    """``PlaylistWriter`` backed by pyrekordbox's ORM.

    Attributes
    ----------
    db
        Open ``Rekordbox6Database`` (or any duck-typed fake with
        ``get_playlist``, ``create_playlist``, ``add_to_playlist``,
        ``remove_from_playlist``, ``commit``).
    state_conn
        SQLite connection to the shared state DB (for stable_id ->
        ContentID resolution via ``track_vendor_ids``).
    vendor
        Vendor tag surfaced in ``MaterializeResult.writers_applied``.
    live
        When ``True``, enforce the Phase 1 pgrep + backup safety rails
        before the first mutation. Tests and dry-run paths leave it
        ``False`` so duck-typed fake DBs continue to work without a
        real ``master.db`` on disk.
    live_db_path
        Override for the RB ``master.db`` path used by the backup step
        (defaults to :data:`apps.shared.paths.REKORDBOX_LIVE_DB`). Tests
        set this when they want the backup step exercised against a
        fixture file.
    backup_dir
        Override for the backup destination directory.
    """

    db: Any
    state_conn: sqlite3.Connection
    vendor: str = "rekordbox"
    live: bool = False
    live_db_path: Path = field(default_factory=lambda: paths.REKORDBOX_LIVE_DB)
    backup_dir: Path = field(default_factory=lambda: DEFAULT_BACKUP_DIR)
    _backup_taken: Path | None = field(default=None, init=False, repr=False)

    def _assert_safe_to_write(self, *, take_backup: bool = True) -> None:
        """Run the pgrep gate + take a one-shot backup.

        No-op when ``live=False`` (keeps unit tests with duck-typed DBs
        working unchanged). When ``live=True`` and Rekordbox is open we
        raise :class:`RuntimeError`; the materialiser catches it and
        records a per-writer failure so other writers still run.
        """
        if not self.live:
            return
        if _rekordbox_running():
            raise RuntimeError(
                "rekordbox: refusing to write -- Rekordbox appears to be "
                "running (pgrep -if rekordbox). Quit the app and retry."
            )
        if take_backup and self._backup_taken is None:
            self._backup_taken = _backup_rb_db(
                live_db=self.live_db_path, backup_dir=self.backup_dir,
            )

    def _find_playlist(self, name: str):
        for p in self.db.get_playlist():
            if (p.Name or "") == name:
                return p
        return None

    def _find_playlist_by_id(self, playlist_id: str):
        for playlist in self.db.get_playlist():
            if str(getattr(playlist, "ID", "")) == playlist_id:
                return playlist
        return None

    def list_playlists(self):
        """Return stable Rekordbox IDs for safe writeback target selection."""
        from apps.webui.server.playlist_writeback import VendorPlaylist

        return [
            VendorPlaylist(playlist_id=str(playlist.ID), name=playlist.Name or "")
            for playlist in self.db.get_playlist()
            if getattr(playlist, "ID", None) is not None
        ]

    def playlist_exists(self, name: str) -> bool:
        return self._find_playlist(name) is not None

    def _resolve_or_raise(self, stable_ids: list[str]) -> list[str]:
        rb_ids: list[str] = []
        missing: list[str] = []
        for sid in stable_ids:
            rb_id = _resolve_rb_id(self.state_conn, sid)
            if rb_id is None:
                missing.append(sid)
            else:
                rb_ids.append(str(rb_id))
        if missing:
            raise RuntimeError(
                f"rekordbox: no ContentID mapping for stable_ids "
                f"{missing[:5]}{'...' if len(missing) > 5 else ''}"
            )
        return rb_ids

    def create_playlist(self, name: str, track_ids: list[str]) -> None:
        self._assert_safe_to_write()
        rb_ids = self._resolve_or_raise(list(track_ids))
        pl = self.db.create_playlist(name)
        for rb_id in rb_ids:
            self.db.add_to_playlist(pl, rb_id)
        self.db.commit()

    def apply_diff(
        self, name: str, added: list[str], removed: list[str],
    ) -> None:
        self._assert_safe_to_write()
        pl = self._find_playlist(name)
        if pl is None:
            raise RuntimeError(
                f"rekordbox: playlist {name!r} not found; "
                "cannot apply diff"
            )
        add_ids = self._resolve_or_raise(list(added))
        remove_ids = self._resolve_or_raise(list(removed))
        for rb_id in remove_ids:
            self.db.remove_from_playlist(pl, rb_id)
        for rb_id in add_ids:
            self.db.add_to_playlist(pl, rb_id)
        self.db.commit()

    def read_members(self, name: str) -> list[str]:
        """Current membership as stable_ids, in native ``TrackNo`` order.

        A song whose ``ContentID`` has no reverse mapping in
        ``track_vendor_ids`` (an RB-side track the state layer has never
        ingested) is skipped rather than raised -- it is not this
        writer's place to fail a read over a track it cannot name; the
        caller sees a shorter list and the untracked id is simply not
        part of the diff.
        """
        pl = self._find_playlist(name)
        if pl is None:
            raise RuntimeError(f"rekordbox: playlist {name!r} not found")
        songs = list(getattr(pl, "Songs", []) or [])
        songs.sort(key=lambda s: (getattr(s, "TrackNo", 0) or 0))
        members: list[str] = []
        for s in songs:
            content_id = getattr(s, "ContentID", None)
            if content_id is None:
                continue
            sid = _resolve_stable_id_for_rb(self.state_conn, str(content_id))
            if sid is not None:
                members.append(sid)
        return members

    def read_members_by_id(self, playlist_id: str) -> list[str]:
        playlist = self._find_playlist_by_id(playlist_id)
        if playlist is None:
            raise RuntimeError(f"rekordbox: playlist ID {playlist_id!r} not found")
        songs = list(getattr(playlist, "Songs", []) or [])
        songs.sort(key=lambda song: (getattr(song, "TrackNo", 0) or 0))
        members: list[str] = []
        for song in songs:
            content_id = getattr(song, "ContentID", None)
            if content_id is None:
                raise RuntimeError(f"rekordbox: target {playlist_id!r} has a member without ContentID")
            stable_id = _resolve_stable_id_for_rb(self.state_conn, str(content_id))
            if stable_id is None:
                raise RuntimeError(f"rekordbox: target {playlist_id!r} has unmapped ContentID {content_id!r}")
            members.append(stable_id)
        return members

    def apply_with_backup_by_id(
        self, playlist_id: str, stable_members: list[str], expected_target_revision: str,
        expected_mapping_revision: str, source_transaction: Callable[[], ContextManager[None]],
    ):
        """CAS, WAL-safe backup, and mutation in one Rekordbox transaction."""
        from pyrekordbox.db6 import tables
        from sqlalchemy import text
        from apps.smartlists.writeback_backup import exclusive_target_lock, online_backup, write_reversal
        from apps.webui.server.playlist_writeback import WritebackBackup, WritebackConflict

        self._assert_safe_to_write(take_backup=False)
        session = getattr(self.db, "session", None)
        if session is None:
            raise RuntimeError("rekordbox: writer has no SQLAlchemy session for transactional writeback")
        with source_transaction():
            with exclusive_target_lock(self.live_db_path):
                session.execute(text("BEGIN IMMEDIATE"))
                try:
                    session.expire_all()
                    before = self.read_members_by_id(playlist_id)
                    before_playlist = self._find_playlist_by_id(playlist_id)
                    if before_playlist is None:
                        raise RuntimeError(f"rekordbox: playlist ID {playlist_id!r} not found")
                    before_songs = list(getattr(before_playlist, "Songs", []) or [])
                    before_songs.sort(key=lambda song: (getattr(song, "TrackNo", 0) or 0))
                    native_before = [str(song.ContentID) for song in before_songs]
                    actual = hashlib.sha256(json.dumps({"target_id": playlist_id, "members": before}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                    if actual != expected_target_revision:
                        raise WritebackConflict("rekordbox: target revision changed before transaction")
                    mapping_rows = self.state_conn.execute(
                        "SELECT stable_id, vendor_id FROM track_vendor_ids WHERE vendor = ? AND stable_id IN (" + ",".join("?" * len(set(stable_members))) + ")",
                        (self.vendor, *dict.fromkeys(stable_members)),
                    ).fetchall() if stable_members else []
                    mapping_revision = hashlib.sha256(json.dumps(sorted((str(stable_id), str(vendor_id)) for stable_id, vendor_id in mapping_rows), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                    if mapping_revision != expected_mapping_revision:
                        raise WritebackConflict("rekordbox: mapping changed inside vendor transaction")
                    native_mapping = {str(stable_id): str(vendor_id) for stable_id, vendor_id in mapping_rows}
                    missing = [stable_id for stable_id in stable_members if stable_id not in native_mapping]
                    if missing:
                        raise WritebackConflict(f"rekordbox: mapping lost desired IDs inside vendor transaction: {missing[:5]}")
                    native_members = [native_mapping[stable_id] for stable_id in stable_members]
                    with sqlite3.connect(f"file:{self.live_db_path}?mode=ro", uri=True) as snapshot:
                        backup = WritebackBackup(online_backup(snapshot, "rekordbox"))
                    post_revision = hashlib.sha256(json.dumps({"target_id": playlist_id, "members": stable_members}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                    write_reversal("rekordbox", backup.backup_id, self.live_db_path, playlist_id, before, native_before, post_revision)
                    playlist = self._find_playlist_by_id(playlist_id)
                    if playlist is None:
                        raise RuntimeError(f"rekordbox: playlist ID {playlist_id!r} not found")
                    for song in list(getattr(playlist, "Songs", []) or []):
                        self.db.delete(song)
                    session.flush()
                    now = datetime.datetime.now()
                    for number, content_id in enumerate(native_members, start=1):
                        self.db.add(tables.DjmdSongPlaylist.create(
                            ID=str(uuid4()), UUID=str(uuid4()), PlaylistID=str(playlist.ID),
                            ContentID=str(content_id), TrackNo=number, created_at=now, updated_at=now,
                        ))
                    self.db.commit()
                except Exception:
                    session.rollback()
                    raise
        return backup, post_revision

    def backup_target(self):
        """Snapshot the exact DB this writer opened, never the working copy."""
        from apps.webui.server.playlist_writeback import WritebackBackup
        from apps.smartlists.writeback_backup import online_backup

        with sqlite3.connect(f"file:{self.live_db_path}?mode=ro", uri=True) as source:
            return WritebackBackup(online_backup(source, "rekordbox"))

    def restore_backup(self, backup_id: str, target_id: str, expected_target_revision: str) -> str:
        """Atomically restore a writeback backup when the target still matches CAS."""
        from apps.webui.server.playlist_writeback import WritebackConflict

        from pyrekordbox.db6 import tables
        from sqlalchemy import text
        from apps.smartlists.writeback_backup import exclusive_target_lock, read_reversal
        self._assert_safe_to_write(take_backup=False)
        with exclusive_target_lock(self.live_db_path):
            session = getattr(self.db, "session", None)
            if session is None:
                raise RuntimeError("rekordbox: writer has no SQLAlchemy session for rollback")
            session.execute(text("BEGIN IMMEDIATE"))
            try:
                session.expire_all()
                current = self.read_members_by_id(target_id)
                actual = hashlib.sha256(json.dumps({"target_id": target_id, "members": current}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                if actual != expected_target_revision:
                    raise WritebackConflict("rekordbox: rollback target revision conflict")
                stable_preimage, native_preimage = read_reversal("rekordbox", backup_id, self.live_db_path, target_id, expected_target_revision)
                restored_revision = hashlib.sha256(json.dumps({"target_id": target_id, "members": stable_preimage}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                playlist = self._find_playlist_by_id(target_id)
                if playlist is None:
                    raise RuntimeError(f"rekordbox: playlist ID {target_id!r} not found")
                for song in list(getattr(playlist, "Songs", []) or []):
                    self.db.delete(song)
                session.flush()
                now = datetime.datetime.now()
                for number, content_id in enumerate(native_preimage, start=1):
                    self.db.add(tables.DjmdSongPlaylist.create(
                        ID=str(uuid4()), UUID=str(uuid4()), PlaylistID=str(playlist.ID),
                        ContentID=str(content_id), TrackNo=number, created_at=now, updated_at=now,
                    ))
                self.db.commit()
            except Exception:
                session.rollback()
                raise
        return restored_revision


def build_rb_writer(
    db_factory: Callable[[], Any] | None = None,
    state_conn_factory: Callable[[], sqlite3.Connection] | None = None,
    *,
    live: bool = False,
) -> RBPlaylistWriter | None:
    """Construct an :class:`RBPlaylistWriter` or return None on failure.

    ``None`` is returned when the RB DB or state DB can't be opened -- the
    materialiser treats writer absence as a soft-fail and moves on to the
    next writer in the list.

    ``live`` flips the Phase 1 safety rails on: the returned writer will
    refuse to mutate ``master.db`` until ``pgrep -if rekordbox`` is empty
    and a timestamped backup has been taken. Callers in dry-run paths
    leave it ``False`` (the default).
    """
    if db_factory is None:
        from apps.shared.rekordbox_db import open_db

        db_factory = open_db
    if state_conn_factory is None:
        from apps.shared.state.db import open_ro as _open_state_ro

        state_conn_factory = _open_state_ro
    try:
        db = db_factory()
        state_conn = state_conn_factory()
    except Exception:
        return None
    return RBPlaylistWriter(db=db, state_conn=state_conn, live=live)


__all__ = [
    "DEFAULT_BACKUP_DIR",
    "RBPlaylistWriter",
    "_backup_rb_db",
    "_rekordbox_running",
    "_resolve_rb_id",
    "_resolve_stable_id_for_rb",
    "build_rb_writer",
]
