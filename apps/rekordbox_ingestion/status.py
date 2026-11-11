"""Read-only: which batch files rekordbox has imported, analyzed, and playlisted.

Never opens the live master.db. Every read copies the DB set (master.db, -wal,
-shm, masterPlaylists6.xml) into a temp dir and opens THAT, so it is safe while
rekordbox is running and writing. A torn copy (rekordbox checkpointing mid-copy)
fails ``PRAGMA quick_check`` and is reported as unreadable, never as a result.

Completion signal (verified Thu 29 Oct 2026 against the live library, see the
skill): a file is ANALYZED when its djmdContent row has ``Analysed != 0``, a
non-empty ``AnalysisDataPath``, and ANLZ0000.DAT/.EXT/.2EX exist non-empty under
the share root. Imported-but-unanalyzed rows carry Analysed=0, BPM=0 and no
AnalysisDataPath.
"""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

from apps.rekordbox_ingestion.config import CFG
from apps.shared import platform_paths
from apps.shared.rekordbox_db import is_plain_sqlite

FileState = Literal["MISSING", "IMPORTED", "ANALYZED"]


class SnapshotUnreadable(RuntimeError):
    """The copied DB set did not pass quick_check (usually a torn copy)."""


@dataclass(frozen=True)
class TrackRow:
    content_id: str
    folder_path: str
    analysed: int
    analysis_data_path: str


@dataclass
class FileStatus:
    path: str
    state: FileState
    content_id: str | None = None
    analysed: int | None = None
    missing_anlz: list[str] = field(default_factory=list)
    in_playlists: list[str] = field(default_factory=list)
    unusual_code: bool = False


@dataclass
class BatchStatus:
    batch_dir: str
    denominator: int
    counts: dict[str, int]
    in_playlist_count: int
    playlists: dict[str, int]
    files: list[FileStatus]

    @property
    def complete(self) -> bool:
        return self.denominator > 0 and self.counts["ANALYZED"] == self.denominator

    @property
    def in_rekordbox_count(self) -> int:
        """Tracks present in djmdContent (imported or fully analyzed)."""
        return self.counts["IMPORTED"] + self.counts["ANALYZED"]

    def to_dict(self) -> dict:
        return {**asdict(self), "complete": self.complete}


# ----- helpers ---------------------------------------------------------------


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def list_batch_audio(batch_dir: Path) -> list[Path]:
    """Audio files directly or nested under ``batch_dir``; the honest denominator."""
    if not batch_dir.is_dir():
        raise FileNotFoundError(f"batch dir does not exist: {batch_dir}")
    return sorted(
        p for p in batch_dir.rglob("*")
        if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in CFG.AUDIO_SUFFIXES
    )


def missing_anlz_files(analysis_data_path: str, share_root: Path) -> list[str]:
    """Required ANLZ suffixes absent or empty for one row (empty path -> all missing)."""
    if not analysis_data_path:
        return list(CFG.REQUIRED_ANLZ_SUFFIXES)
    dat = share_root / analysis_data_path.lstrip("/")
    missing: list[str] = []
    for suffix in CFG.REQUIRED_ANLZ_SUFFIXES:
        candidate = dat.with_suffix(suffix)
        if not candidate.is_file() or candidate.stat().st_size == 0:
            missing.append(suffix)
    return missing


def classify(row: TrackRow | None, share_root: Path) -> tuple[FileState, list[str]]:
    """Pure decision: one file's state from its row and the ANLZ files on disk."""
    if row is None:
        return "MISSING", []
    missing = missing_anlz_files(row.analysis_data_path, share_root)
    if row.analysed != 0 and row.analysis_data_path and not missing:
        return "ANALYZED", []
    return "IMPORTED", missing


@contextmanager
def snapshot_db(db_path: Path) -> Iterator[Path]:
    """Copy the DB set into a temp dir and yield the copied master.db path."""
    if not db_path.is_file():
        raise FileNotFoundError(f"rekordbox DB not found: {db_path}")
    with tempfile.TemporaryDirectory(prefix="rbx-ingest-snap-") as tmp:
        tmp_dir = Path(tmp)
        # -shm is deliberately NOT copied: it is a live process's shared-memory
        # index, and a stale copy can disagree with the copied WAL. Without it
        # SQLite rebuilds the index from the WAL file itself.
        for suffix in ("", "-wal"):
            source = Path(f"{db_path}{suffix}")
            if source.exists():
                try:
                    shutil.copy2(source, tmp_dir / f"{db_path.name}{suffix}")
                except FileNotFoundError as exc:
                    # SQLite deletes the WAL when the last connection closes, which
                    # can land between exists() and the copy: a transient miss the
                    # watch loop retries, not a missing database.
                    if suffix == "":
                        raise
                    raise SnapshotUnreadable(f"{source.name} vanished while copying: {exc}") from exc
        xml = db_path.parent / CFG.PLAYLIST_XML_NAME
        if xml.exists():
            shutil.copy2(xml, tmp_dir / CFG.PLAYLIST_XML_NAME)
        yield tmp_dir / db_path.name


def _read_rows(snapshot: Path, folder: str) -> tuple[dict[str, TrackRow], dict[str, list[str]], dict[str, int]]:
    """Rows keyed by NFC FolderPath, playlist names per ContentID, member counts."""
    from pyrekordbox import Rekordbox6Database
    from sqlalchemy import text
    from sqlalchemy.exc import DatabaseError

    try:
        db = Rekordbox6Database(path=str(snapshot), unlock=not is_plain_sqlite(snapshot))
    except (DatabaseError, sqlite3.DatabaseError) as exc:
        raise SnapshotUnreadable(f"snapshot did not open: {exc}") from exc
    try:
        session = db.session
        check = session.execute(text("PRAGMA quick_check")).scalar()
        if check != "ok":
            raise SnapshotUnreadable(f"quick_check on snapshot returned {check!r}")
        rows: dict[str, TrackRow] = {}
        for cid, fpath, analysed, adp in session.execute(text(
            "SELECT ID, FolderPath, COALESCE(Analysed, 0), COALESCE(AnalysisDataPath, '') "
            "FROM djmdContent WHERE COALESCE(rb_local_deleted, 0) = 0"
        )):
            if fpath:
                rows[_nfc(fpath)] = TrackRow(str(cid), fpath, int(analysed), adp)
        membership: dict[str, list[str]] = {}
        counts: dict[str, int] = {}
        for name, cid in session.execute(text(
            "SELECT p.Name, sp.ContentID FROM djmdPlaylist p "
            "JOIN djmdPlaylist f ON p.ParentID = f.ID "
            "LEFT JOIN djmdSongPlaylist sp ON sp.PlaylistID = p.ID "
            "AND COALESCE(sp.rb_local_deleted, 0) = 0 "
            "WHERE f.Name = :folder AND f.Attribute = 1 "
            "AND COALESCE(p.rb_local_deleted, 0) = 0"
        ), {"folder": folder}):
            counts.setdefault(name, 0)
            if cid is not None:
                counts[name] += 1
                membership.setdefault(str(cid), []).append(name)
        return rows, membership, counts
    except (DatabaseError, sqlite3.DatabaseError) as exc:
        raise SnapshotUnreadable(f"snapshot read failed: {exc}") from exc
    finally:
        db.close()
        db.engine.dispose()


# ----- public ----------------------------------------------------------------


def read_batch_status(
    batch_dir: Path,
    *,
    db_path: Path | None = None,
    share_root: Path | None = None,
    folder: str = CFG.PLAYLIST_FOLDER,
) -> BatchStatus:
    """Status of every audio file in ``batch_dir`` against a snapshot of the DB."""
    live_db = db_path if db_path is not None else platform_paths.REKORDBOX_LIVE_DB
    share = share_root if share_root is not None else platform_paths.SHARE_ROOT
    audio = list_batch_audio(batch_dir)
    with snapshot_db(live_db) as snapshot:
        rows, membership, playlist_counts = _read_rows(snapshot, folder)
    files: list[FileStatus] = []
    counts = {"MISSING": 0, "IMPORTED": 0, "ANALYZED": 0}
    in_playlist = 0
    for path in audio:
        row = rows.get(_nfc(str(path)))
        state, missing = classify(row, share)
        counts[state] += 1
        playlists = membership.get(row.content_id, []) if row else []
        in_playlist += bool(playlists)
        files.append(FileStatus(
            path=str(path), state=state,
            content_id=row.content_id if row else None,
            analysed=row.analysed if row else None,
            missing_anlz=missing, in_playlists=playlists,
            unusual_code=bool(row and state == "ANALYZED" and row.analysed != CFG.LIBRARY_NORM_ANALYSED_CODE),
        ))
    return BatchStatus(
        batch_dir=str(batch_dir), denominator=len(audio), counts=counts,
        in_playlist_count=in_playlist, playlists=playlist_counts, files=files,
    )
