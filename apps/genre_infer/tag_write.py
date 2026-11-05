"""Write a track's chosen genre into its audio file, with a backup and an undo journal.

ADR-0122 removed mutagen (GPL) and with it every audio-file write. This writes ONE
field, the genre, through the in-house Apache-2.0 container writers
(``apps.shared.id3v2``, ``flac_meta``, ``mp4_meta``, ``ogg_comment``). Every other
tag in the file is left as it was.

Safety, all enforced here (fail-fast, explicit error, file untouched):

* ``write_file_tags`` must be on in the genre config: off by default.
* The resolved path must sit under one of the configured ``write_roots``: an
  empty list refuses every write, so turning the flag on alone writes nothing.
* rekordbox and djay must not be running (they cache and rewrite tags).
* A byte copy is taken to ``<data>/state/genre/tag-backups/<date>/`` first and
  never overwritten; the tag is read back after the write and a mismatch puts
  the backup back.
* Each write is a ``genre_tag_writes`` row; :func:`undo` restores the backup only
  while the file still has the exact bytes this write produced.

-Claude
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil

from apps.shared import flac_meta, id3v2, mp4_meta, ogg_comment
from apps.shared.hashing import sha256_file_hex
from apps.shared.tag_writer import read_tags

from . import findings, store

# Raw ADTS .aac has no MP4 container to hold a tag: not supported (refused, never guessed).
SUPPORTED_SUFFIXES: tuple[str, ...] = (".mp3", ".flac", ".m4a", ".mp4", ".ogg", ".opus")
BLOCKING_APPS: tuple[str, ...] = ("rekordbox", "djay")


class TagWriteRefused(RuntimeError):
    """The write was not attempted; ``reason`` says why (HTTP 409)."""


@dataclass(frozen=True)
class WriteResult:
    write_id: int
    stable_id: str
    path: str
    previous_genre: str | None
    written_genre: str
    backup_path: str


def _running_blocker(apps: tuple[str, ...]) -> str | None:
    """The first of ``apps`` (lowercase substrings of a process name) running now, else None."""
    if not apps:
        return None
    for proc in psutil.process_iter(["name"]):
        name = (proc.info.get("name") or "").lower()
        hit = next((app for app in apps if app in name), None)
        if hit is not None:
            return hit
    return None


def _under_roots(path: Path, roots: tuple[str, ...]) -> bool:
    resolved = path.resolve()
    return any(resolved.is_relative_to(Path(root).resolve()) for root in roots)


def _set_genre(path: Path, genre: str) -> None:
    suffix = path.suffix.lower()
    if suffix == ".mp3":
        tag = id3v2.load_or_new(path)
        tag.set_text("TCON", genre)
        id3v2.save(path, tag)
    elif suffix == ".flac":
        flac = flac_meta.read(path)
        flac.set("GENRE", genre)
        flac_meta.save(path, flac)
    elif suffix in (".m4a", ".mp4"):
        mp4 = mp4_meta.read(path)
        mp4.set_text("\xa9gen", genre)
        mp4_meta.save(path, mp4)
    elif suffix in (".ogg", ".opus"):
        ogg = ogg_comment.read(path)
        ogg.set("GENRE", genre)
        ogg_comment.save(path, ogg)
    else:
        raise TagWriteRefused(f"{suffix or 'no suffix'} files are not supported; supported: {SUPPORTED_SUFFIXES}")


def _ensure_journal_columns(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(genre_tag_writes)")}
    if "post_sha256" not in columns:
        conn.execute("ALTER TABLE genre_tag_writes ADD COLUMN post_sha256 TEXT")


def _refuse_unless_writable(cfg: findings.GenreConfig, path: Path, *, blocking_apps: tuple[str, ...]) -> None:
    """Raise :class:`TagWriteRefused` naming the first reason ``path`` may not be written."""
    if not cfg.write_file_tags:
        raise TagWriteRefused("write_file_tags is off in the genre config; nothing was written")
    if not cfg.write_roots:
        raise TagWriteRefused("write_roots is empty in the genre config; no file is allowed to be written")
    if not path.is_file():
        raise TagWriteRefused(f"{path} is not a file here")
    if not _under_roots(path, cfg.write_roots):
        raise TagWriteRefused(f"{path} is outside every configured write root {list(cfg.write_roots)}")
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise TagWriteRefused(f"{path.suffix} files are not supported; supported: {SUPPORTED_SUFFIXES}")
    if (app := _running_blocker(blocking_apps)) is not None:
        raise TagWriteRefused(f"{app} is running; close it before writing tags into its files")


def _sibling(path: Path, tag: str) -> Path:
    """A hidden file next to ``path`` with the same suffix, so the tag writers recognize its format."""
    return path.with_name(f".{path.stem}.{tag}{path.suffix}")


def _atomic_copy(src: Path, dst: Path) -> None:
    """Copy ``src`` over ``dst`` through a sibling and a rename: a crash leaves the old or the new file, never half of one."""
    partial = _sibling(dst, "genre-partial")
    shutil.copy2(src, partial)
    os.replace(partial, dst)


def _verified_backup(path: Path, backup_dir: Path) -> Path:
    """A byte copy of ``path`` named by its hash.

    A backup is trusted only when its bytes match its name; it is written
    through a temporary file and renamed, so a crash never leaves half of one.
    """
    backup_dir.mkdir(parents=True, exist_ok=True)
    digest = sha256_file_hex(path)
    backup = backup_dir / f"{digest}{path.suffix.lower()}"
    if not backup.exists() or sha256_file_hex(backup) != digest:
        _atomic_copy(path, backup)
    return backup


def _write_staged(path: Path, staged: Path, genre: str) -> None:
    """Copy ``path`` to ``staged`` and set its genre; refused unless it reads back."""
    shutil.copy2(path, staged)
    _set_genre(staged, genre)
    read_back = read_tags(staged).genre
    if read_back != genre:
        raise TagWriteRefused(f"read back {read_back!r} after writing {genre!r}; the original is untouched")


def write_genre(
    conn: sqlite3.Connection,
    *,
    data_dir: Path,
    stable_id: str,
    path: Path,
    genre: str,
    provider: str,
    blocking_apps: tuple[str, ...] = BLOCKING_APPS,
) -> WriteResult:
    """Write ``genre`` into ``path``'s genre tag; returns the journal entry. Commits."""
    findings.ensure_schema(conn)
    _ensure_journal_columns(conn)
    _refuse_unless_writable(findings.read_config(conn), path, blocking_apps=blocking_apps)
    previous = read_tags(path).genre
    backup = _verified_backup(path, store.genre_dir(data_dir) / "tag-backups" / datetime.now(UTC).strftime("%Y-%m-%d"))
    # Tag a sibling copy and check it; journal the write; only then rename the
    # copy over the file. The file is the original or the verified result at
    # every instant, and a changed file always has its journal row (undo path).
    staged = _sibling(path, "genre-write")
    try:
        _write_staged(path, staged, genre)
        cur = conn.execute(
            "INSERT INTO genre_tag_writes (stable_id, path, previous_genre, written_genre, provider, backup_path, written_at, post_sha256) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                stable_id,
                str(path),
                previous,
                genre,
                provider,
                str(backup),
                datetime.now(UTC).isoformat(timespec="seconds"),
                sha256_file_hex(staged),
            ),
        )
        conn.commit()
        try:
            os.replace(staged, path)
        except BaseException:
            # The file was never changed: the journal row must not claim it was.
            conn.execute("DELETE FROM genre_tag_writes WHERE id = ?", (cur.lastrowid,))
            conn.commit()
            raise
    except BaseException:
        conn.rollback()
        raise
    finally:
        staged.unlink(missing_ok=True)
    return WriteResult(int(cur.lastrowid or 0), stable_id, str(path), previous, genre, str(backup))


def _mark_undone(conn: sqlite3.Connection, write_id: int) -> None:
    conn.execute(
        "UPDATE genre_tag_writes SET undone_at = ? WHERE id = ?",
        (datetime.now(UTC).isoformat(timespec="seconds"), write_id),
    )
    conn.commit()


def undo(conn: sqlite3.Connection, write_id: int, *, blocking_apps: tuple[str, ...] = BLOCKING_APPS) -> WriteResult:
    """Put the pre-write bytes back for journal entry ``write_id``. Commits."""
    findings.ensure_schema(conn)
    _ensure_journal_columns(conn)
    row = conn.execute(
        "SELECT stable_id, path, previous_genre, written_genre, backup_path, undone_at, post_sha256 FROM genre_tag_writes WHERE id = ?",
        (write_id,),
    ).fetchone()
    if row is None:
        raise TagWriteRefused(f"no genre tag write {write_id}")
    stable_id, path_s, previous, written, backup_s, undone_at, post_sha = row
    if undone_at is not None:
        raise TagWriteRefused(f"write {write_id} was already undone at {undone_at}")
    path, backup = Path(path_s), Path(backup_s)
    if not backup.is_file():
        raise TagWriteRefused(f"backup {backup} is gone; cannot undo write {write_id}")
    current = sha256_file_hex(path) if path.is_file() else None
    if current is not None and current == backup.stem:
        # The write was journaled but never renamed into place (a crash between
        # the two): the file already holds the original bytes.
        _mark_undone(conn, write_id)
        return WriteResult(write_id, stable_id, path_s, previous, written, backup_s)
    if current != post_sha:
        raise TagWriteRefused(f"{path} changed after write {write_id}; refusing to overwrite the newer file")
    if (app := _running_blocker(blocking_apps)) is not None:
        raise TagWriteRefused(f"{app} is running; close it before restoring tags in its files")
    # Keep the tagged bytes until the journal says "undone": a journal that
    # cannot be updated puts them back, so the row and the file never disagree.
    tagged = _sibling(path, "genre-undo")
    shutil.copy2(path, tagged)
    try:
        _atomic_copy(backup, path)
        _mark_undone(conn, write_id)
    except Exception:
        conn.rollback()
        _atomic_copy(tagged, path)
        raise
    finally:
        tagged.unlink(missing_ok=True)
    return WriteResult(write_id, stable_id, path_s, previous, written, backup_s)


def journal(conn: sqlite3.Connection, stable_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    if not findings.has_schema(conn):
        return []
    sql = "SELECT id, stable_id, path, previous_genre, written_genre, provider, backup_path, written_at, undone_at FROM genre_tag_writes"
    params: tuple[object, ...] = ()
    if stable_id is not None:
        sql += " WHERE stable_id = ?"
        params = (stable_id,)
    keys = (
        "id",
        "stable_id",
        "path",
        "previous_genre",
        "written_genre",
        "provider",
        "backup_path",
        "written_at",
        "undone_at",
    )
    return [dict(zip(keys, r, strict=True)) for r in conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*params, limit))]


__all__ = ["SUPPORTED_SUFFIXES", "TagWriteRefused", "WriteResult", "journal", "undo", "write_genre"]
