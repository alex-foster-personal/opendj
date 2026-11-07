"""Read-only snapshots for the ANALYSE-ME planner (RBXAN-8322).

Rekordbox side: byte-copy the live ``master.db``, fingerprint that copy, decrypt
it and read the collection and playlists with plain SQLite. The live file is
only ever read as bytes, so this is safe while Rekordbox is running.

Open DJ side: tracks with their file paths and the names of the Open DJ
playlists that hold them, from ``state.db`` opened read-only.

-Claude
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from apps.rekordbox_analyse.plan import OdjTrack, RbPlaylist, RbTrack
from apps.shared.hashing import sha256_file_hex as sha256_file
from apps.shared.rekordbox_db import decrypt_to_plain, is_plain_sqlite

#: djmdPlaylist.Attribute: 0 playlist, 1 folder, 4 smart playlist.
_FOLDER_ATTRIBUTE = 1


@dataclass(frozen=True)
class RbSnapshot:
    master_db_sha256: str
    tracks: tuple[RbTrack, ...]
    playlists: tuple[RbPlaylist, ...]


# -----------------------------------------------------------------------------
# Helpers


def _connect_ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def read_plain_rekordbox(plain_db: Path, master_db_sha256: str) -> RbSnapshot:
    """Read tracks and playlists from an already decrypted Rekordbox database."""
    with _connect_ro(plain_db) as conn:
        tracks = tuple(
            RbTrack(content_id=str(cid), folder_path=folder, analysed=bool(analysed) and bool(anlz))
            for cid, folder, analysed, anlz in conn.execute(
                "SELECT ID, FolderPath, COALESCE(Analysed, 0), COALESCE(AnalysisDataPath, '') "
                "FROM djmdContent WHERE COALESCE(rb_local_deleted, 0) = 0 AND FolderPath IS NOT NULL"
            )
        )
        members: dict[str, list[str]] = defaultdict(list)
        for pid, cid in conn.execute(
            "SELECT PlaylistID, ContentID FROM djmdSongPlaylist "
            "WHERE COALESCE(rb_local_deleted, 0) = 0 ORDER BY PlaylistID, TrackNo"
        ):
            members[str(pid)].append(str(cid))
        playlists = tuple(
            RbPlaylist(
                playlist_id=str(pid),
                name=name,
                is_folder=attribute == _FOLDER_ATTRIBUTE,
                member_ids=tuple(members.get(str(pid), ())),
            )
            for pid, name, attribute in conn.execute(
                "SELECT ID, Name, COALESCE(Attribute, 0) FROM djmdPlaylist WHERE COALESCE(rb_local_deleted, 0) = 0"
            )
        )
    return RbSnapshot(master_db_sha256=master_db_sha256, tracks=tracks, playlists=playlists)


def integrity_ok(plain_db: Path) -> str:
    """``PRAGMA integrity_check`` on a plain copy; returns the first line ('ok' when sound)."""
    with _connect_ro(plain_db) as conn:
        row = conn.execute("PRAGMA integrity_check").fetchone()
    return str(row[0]) if row else "no result"


def copy_with_sidecars(live_db: Path, folder: Path) -> Path:
    """Copy master.db plus any -wal/-shm beside it, so uncheckpointed commits are not lost."""
    copy = folder / live_db.name
    shutil.copy2(live_db, copy)
    for suffix in ("-wal", "-shm"):
        side = live_db.with_name(live_db.name + suffix)
        if side.exists():
            shutil.copy2(side, folder / side.name)
    return copy


def snapshot_rekordbox(live_db: Path) -> RbSnapshot:
    """Copy, fingerprint, decrypt and read ``live_db`` without opening it."""
    if not live_db.is_file():
        raise FileNotFoundError(f"Rekordbox database not found: {live_db}")
    with tempfile.TemporaryDirectory(prefix="rbx-analyse-") as tmp:
        copy = copy_with_sidecars(live_db, Path(tmp))
        sha = sha256_file(copy)
        plain = copy if is_plain_sqlite(copy) else decrypt_to_plain(copy, Path(tmp) / "master.plain.db")
        return read_plain_rekordbox(plain, sha)


def snapshot_open_dj(state_db: Path) -> tuple[OdjTrack, ...]:
    """Live (not tombstoned) Open DJ tracks, with the names of the playlists holding them."""
    with _connect_ro(state_db) as conn:
        names: dict[str, list[str]] = defaultdict(list)
        for sid, name in conn.execute(
            "SELECT m.stable_id, p.name FROM playlist_memberships m JOIN playlists p USING (playlist_id)"
            " WHERE m.deleted_at IS NULL AND p.deleted_at IS NULL"
        ):
            names[sid].append(name)
        return tuple(
            OdjTrack(stable_id=sid, file_path=path, playlists=tuple(sorted(set(names.get(sid, ())))))
            for sid, path in conn.execute(
                "SELECT stable_id, file_path FROM tracks WHERE deleted_at IS NULL ORDER BY stable_id"
            )
        )


__all__ = [
    "RbSnapshot",
    "copy_with_sidecars",
    "integrity_ok",
    "read_plain_rekordbox",
    "sha256_file",
    "snapshot_open_dj",
    "snapshot_rekordbox",
]
