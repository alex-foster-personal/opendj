"""What is on this machine, read without touching it.

Detection stats files and asks pyrekordbox whether it holds a usable
SQLCipher key. It never opens the live ``master.db``, never copies it and
never writes anything: a wizard step that says "here is what I found" must
not have already changed the thing it found.

Every negative answer is a CODE, not a bare False. "no rekordbox here",
"rekordbox is here but the key is unreachable" and "the share dir with the
ANLZ analyses is missing" send an operator to three different places.
"""

from __future__ import annotations

import dataclasses
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from apps.shared import platform_paths, rekordbox_db
from apps.shared.fs_access import (  # re-exported: ONE access probe in the tree
    GRANT_INSTRUCTIONS,
    AccessProbe,
    denied_roots,
    music_folder_candidates,
    music_root_access,
    probe_all,
    probe_readable,
)
from apps.shared.paths import REKORDBOX_PLAIN_DB, REKORDBOX_WORKING_DB

# ----- refusal codes ------------------------------------------------------
CODE_REKORDBOX_NOT_FOUND: str = "rekordbox_not_found"
CODE_KEY_UNAVAILABLE: str = "rekordbox_key_unavailable"
CODE_SHARE_MISSING: str = "rekordbox_share_missing"
CODE_DECRYPT_FAILED: str = "rekordbox_decrypt_failed"
CODE_INGEST_FAILED: str = "rekordbox_ingest_failed"
CODE_IMPORT_ALREADY_RUNNING: str = "setup_import_already_running"
CODE_ACCESS_DENIED: str = "music_folder_access_denied"

CODES: tuple[str, ...] = (
    CODE_REKORDBOX_NOT_FOUND,
    CODE_KEY_UNAVAILABLE,
    CODE_SHARE_MISSING,
    CODE_DECRYPT_FAILED,
    CODE_INGEST_FAILED,
    CODE_IMPORT_ALREADY_RUNNING,
    CODE_ACCESS_DENIED,
)

# Names, not paths: the constants they come from are bound to the PROCESS
# data dir at import time, and detection is asked about an arbitrary one.
WORKING_COPY_NAME: str = REKORDBOX_WORKING_DB.name  # master.db.copy
PLAIN_COPY_NAME: str = REKORDBOX_PLAIN_DB.name  # master.plain.db
STATE_DB_NAME: str = "state.db"

#: A plain SQLite file starts with this. An encrypted (SQLCipher) one does
#: not, because its first page is ciphertext -- which is exactly how the
#: importer tells "already decrypted" from "still needs the key" without
#: guessing from the filename.
# Re-exported: the header check lives beside the decrypt routine it mirrors.
SQLITE_MAGIC = rekordbox_db.SQLITE_MAGIC


@dataclasses.dataclass(frozen=True)
class FileProbe:
    """One stat() call, reported whether or not the file is there."""

    path: str
    exists: bool
    size_bytes: int | None = None
    modified_at: str | None = None

    def to_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class RekordboxDetection:
    """Everything the "detect rekordbox" wizard step shows, with real paths."""

    installed: bool
    live_db: FileProbe
    share_dir: FileProbe
    working_copy: FileProbe
    plain_copy: FileProbe
    key_available: bool
    key_detail: str
    import_source: str | None
    import_source_encrypted: bool | None
    blockers: list[str]

    def to_dict(self) -> dict[str, object]:
        return {
            "installed": self.installed,
            "live_db": self.live_db.to_dict(),
            "share_dir": self.share_dir.to_dict(),
            "working_copy": self.working_copy.to_dict(),
            "plain_copy": self.plain_copy.to_dict(),
            "key_available": self.key_available,
            "key_detail": self.key_detail,
            "import_source": self.import_source,
            "import_source_encrypted": self.import_source_encrypted,
            "blockers": list(self.blockers),
        }


@dataclasses.dataclass(frozen=True)
class LibraryCounts:
    """Row counts straight out of state.db. ``present`` is the denominator."""

    state_db: FileProbe
    tracks: int
    playlists: int

    @property
    def empty(self) -> bool:
        return self.tracks == 0


# ----- probes -------------------------------------------------------------
def probe(path: Path) -> FileProbe:
    """stat() one path. A missing path is reported, never raised over."""
    try:
        stat = path.stat()
    except (OSError, ValueError):
        return FileProbe(path=str(path), exists=False)
    return FileProbe(
        path=str(path),
        exists=True,
        size_bytes=stat.st_size,
        modified_at=datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(
            timespec="seconds"
        ),
    )


def is_plain_sqlite(path: Path) -> bool:
    """True iff ``path`` opens as an unencrypted SQLite file.

    Kept as this module's public name because callers and ``__all__`` already
    use it; the implementation is :func:`apps.shared.rekordbox_db.is_plain_sqlite`,
    so "ready to ingest" and "open without SQLCipher" can never answer
    differently.
    """
    return rekordbox_db.is_plain_sqlite(path)


def key_status() -> tuple[bool, str]:
    """Can this process unlock an encrypted rekordbox DB right now?

    Two independent requirements, reported separately because they fail for
    different reasons: the ``sqlcipher3`` driver has to be importable, and
    pyrekordbox has to hold a key that looks like a rekordbox key. Neither
    is inferred from the other.
    """
    try:
        from pyrekordbox.db6 import database as rb_database
    except ImportError as exc:
        return False, f"pyrekordbox is not importable: {exc}"

    if not getattr(rb_database, "_sqlcipher_available", False):
        return False, (
            "the sqlcipher3 driver is not installed, so an encrypted "
            "rekordbox database cannot be opened at all"
        )
    try:
        key = rb_database.deobfuscate(rb_database.BLOB)
    except (AttributeError, ValueError, TypeError) as exc:
        return False, f"pyrekordbox did not yield a database key: {exc!r}"
    if not key:
        return False, "pyrekordbox yielded an empty database key"
    # pyrekordbox itself rejects anything without this prefix, so a key that
    # fails it here would fail there too -- better to say so before a copy.
    if not key.startswith("402fd"):
        return False, "pyrekordbox yielded a key that is not a rekordbox key"
    return True, f"pyrekordbox holds a {len(key)}-character key"


# ----- composition --------------------------------------------------------
def detect_rekordbox(data_dir: Path) -> RekordboxDetection:
    """Report the rekordbox install and the working copies in ``data_dir``.

    Read-only by construction: four stat() calls and one key probe.
    """
    live_db = probe(platform_paths.rekordbox_app_dir() / "master.db")
    share_dir = probe(platform_paths.compute_share_root())
    working_copy = probe(data_dir / WORKING_COPY_NAME)
    plain_copy = probe(data_dir / PLAIN_COPY_NAME)
    key_available, key_detail = key_status()

    source_path, source_encrypted = choose_import_source(
        live_db=live_db, working_copy=working_copy, plain_copy=plain_copy
    )

    blockers: list[str] = []
    if source_path is None:
        blockers.append(CODE_REKORDBOX_NOT_FOUND)
    if source_encrypted and not key_available:
        blockers.append(CODE_KEY_UNAVAILABLE)
    if not share_dir.exists:
        # Not fatal to the ingest: tracks land either way. It IS fatal to the
        # waveforms, so it is a blocker the wizard must show rather than a
        # surprise the user meets later on the performance page.
        blockers.append(CODE_SHARE_MISSING)

    return RekordboxDetection(
        installed=live_db.exists,
        live_db=live_db,
        share_dir=share_dir,
        working_copy=working_copy,
        plain_copy=plain_copy,
        key_available=key_available,
        key_detail=key_detail,
        import_source=str(source_path) if source_path is not None else None,
        import_source_encrypted=source_encrypted,
        blockers=blockers,
    )


def choose_import_source(
    *, live_db: FileProbe, working_copy: FileProbe, plain_copy: FileProbe
) -> tuple[Path | None, bool | None]:
    """Pick the file the import would read, and say whether it is encrypted.

    Order is cheapest-first: an already-decrypted copy needs no key, a
    working copy needs no touch of the live database, and the live database
    is the last resort because using it means taking a fresh snapshot.
    ``(None, None)`` means there is no rekordbox database anywhere.
    """
    for candidate in (plain_copy, working_copy):
        if candidate.exists:
            path = Path(candidate.path)
            return path, not is_plain_sqlite(path)
    if live_db.exists:
        # The live DB is never read here, so its encryption is asserted from
        # the header of the snapshot, not from this branch. rekordbox 6/7
        # ships encrypted; the importer re-checks after copying.
        path = Path(live_db.path)
        return path, not is_plain_sqlite(path)
    return None, None


def library_counts(data_dir: Path) -> LibraryCounts:
    """Count tracks and playlists in ``data_dir/state/state.db``.

    Read directly rather than through the backend: setup runs BEFORE the
    library exists, which is exactly when a backend has nothing to say. A
    missing state.db is an empty library, not an error.
    """
    state_db = data_dir / "state" / STATE_DB_NAME
    file_probe = probe(state_db)
    if not file_probe.exists:
        return LibraryCounts(state_db=file_probe, tracks=0, playlists=0)
    uri = f"file:{state_db}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        tracks = _count(conn, "tracks")
        playlists = _count(conn, "playlists")
    finally:
        conn.close()
    return LibraryCounts(
        state_db=file_probe, tracks=tracks, playlists=playlists
    )


def _count(conn: sqlite3.Connection, table: str) -> int:
    """COUNT(*) for a table that may not exist yet in a fresh state.db."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if row is None:
        return 0
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def resolve_share_root() -> Path:
    """The share root ANLZ analyses resolve under, recomputed not cached."""
    return platform_paths.compute_share_root()


def rekordbox_is_running() -> bool:
    """True while rekordbox itself holds the live database open.

    Reported, never acted on: the import only ever reads a COPY, so a running
    rekordbox is a caveat on freshness rather than a refusal.
    """
    try:
        from pyrekordbox.utils import get_rekordbox_pid
    except ImportError:
        return False
    return bool(get_rekordbox_pid())


def data_dir_from_env(default: Path) -> Path:
    """The data dir this process was booted against, or ``default``."""
    raw = os.environ.get("MDT_DATA_DIR", "").strip()
    return Path(raw) if raw else default


__all__ = [
    "CODES",
    "CODE_ACCESS_DENIED",
    "CODE_DECRYPT_FAILED",
    "CODE_IMPORT_ALREADY_RUNNING",
    "CODE_INGEST_FAILED",
    "CODE_KEY_UNAVAILABLE",
    "CODE_REKORDBOX_NOT_FOUND",
    "CODE_SHARE_MISSING",
    "GRANT_INSTRUCTIONS",
    "PLAIN_COPY_NAME",
    "SQLITE_MAGIC",
    "WORKING_COPY_NAME",
    "AccessProbe",
    "FileProbe",
    "LibraryCounts",
    "RekordboxDetection",
    "choose_import_source",
    "data_dir_from_env",
    "denied_roots",
    "detect_rekordbox",
    "is_plain_sqlite",
    "key_status",
    "library_counts",
    "music_folder_candidates",
    "music_root_access",
    "probe",
    "probe_all",
    "probe_readable",
    "rekordbox_is_running",
    "resolve_share_root",
]
