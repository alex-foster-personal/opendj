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
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

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

    def _assert_safe_to_write(self) -> None:
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
        if self._backup_taken is None:
            self._backup_taken = _backup_rb_db(
                live_db=self.live_db_path, backup_dir=self.backup_dir,
            )

    def _find_playlist(self, name: str):
        for p in self.db.get_playlist():
            if (p.Name or "") == name:
                return p
        return None

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
    "build_rb_writer",
]
