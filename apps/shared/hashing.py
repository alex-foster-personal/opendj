"""SHA-256 file hashing + persistent cache.

Used by:

* Phase 10 (USB sync) -- ``apps/sync/usb/diff.py``, ``apply.py``, and
  ``verify.py`` all compute the ``content_hash`` field defined in the
  open-dj v0 strawman (``sha256:<64-hex>``).
* Phase 11 (cloud sync) -- will reuse the same hashing + cache surface.

Contract
--------

``sha256_file(path)`` -> ``"sha256:<64-hex>"`` (lowercase). Reads the
file in 1 MiB chunks via :func:`hashlib.file_digest` (Python 3.11+).

``HashCache(db_path)`` wraps a small SQLite table keyed on
``(path, size, mtime_ns)``. Any of those three changing invalidates the
cached hash. Path comparison is exact-string (callers pass absolute
paths).

Design notes
~~~~~~~~~~~~

* The cache is intentionally per-host (it keys on the host's absolute
  path). A drive that roams between Macs will re-hash on each new host.
  That's fine: the cache is a speed-up, never an authority.
* Crash safety: every ``put()`` commits immediately, so an interrupted
  verify loses at most the last in-flight hash.
* Schema is versioned via ``user_version``; bumping invalidates the
  cache.
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path

HASH_PREFIX = "sha256:"
_CACHE_VERSION = 1
_CHUNK = 1 << 20  # 1 MiB

# Characters forbidden on FAT/exFAT + Mac Finder quirks; used by the
# playlist writer + diff planner. Kept here because it is cheap to share
# across the package.
FS_FORBIDDEN: frozenset[str] = frozenset('/\\:*?"<>|')


def sha256_file(path: Path | str) -> str:
    """Compute ``sha256:<64-hex>`` for ``path``.

    Raises :class:`FileNotFoundError` if the file is missing. Uses
    :func:`hashlib.file_digest` so the C implementation can release the
    GIL during disk reads.
    """
    p = Path(path)
    with p.open("rb") as fh:
        digest = hashlib.file_digest(fh, "sha256")
    return HASH_PREFIX + digest.hexdigest()


def content_hash_bytes(data: bytes) -> str:
    """Small helper for tests / in-memory payloads."""
    return HASH_PREFIX + hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class _Entry:
    path: str
    size: int
    mtime_ns: int
    digest: str


class HashCache:
    """SQLite-backed hash cache keyed on ``(path, size, mtime_ns)``.

    Thread-safety: a single instance is safe from one thread at a time.
    Parallel workers should each open their own instance (SQLite's
    check-same-thread rule). The backing DB file is shared.
    """

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._ensure_schema()
        except Exception:
            self._conn.close()
            raise

    def _ensure_schema(self) -> None:
        cur = self._conn.execute("PRAGMA user_version")
        (version,) = cur.fetchone()
        if version != _CACHE_VERSION:
            self._conn.executescript(
                """
                DROP TABLE IF EXISTS file_hashes;
                CREATE TABLE file_hashes (
                    path      TEXT NOT NULL PRIMARY KEY,
                    size      INTEGER NOT NULL,
                    mtime_ns  INTEGER NOT NULL,
                    digest    TEXT NOT NULL
                );
                """
            )
            self._conn.execute(f"PRAGMA user_version = {_CACHE_VERSION}")
            self._conn.commit()

    # ------------------------------------------------------------------
    def get(self, path: Path | str) -> str | None:
        """Return cached digest for ``path`` iff its size+mtime match.

        Returns None on cache miss or file-not-found.
        """
        p = Path(path)
        try:
            st = p.stat()
        except FileNotFoundError:
            return None
        row = self._conn.execute(
            "SELECT size, mtime_ns, digest FROM file_hashes WHERE path = ?",
            (str(p),),
        ).fetchone()
        if row is None:
            return None
        size, mtime_ns, digest = row
        if size != st.st_size or mtime_ns != st.st_mtime_ns:
            return None
        return digest

    def put(self, path: Path | str, digest: str) -> None:
        p = Path(path)
        st = p.stat()
        self._conn.execute(
            "INSERT OR REPLACE INTO file_hashes(path, size, mtime_ns, digest) "
            "VALUES (?, ?, ?, ?)",
            (str(p), st.st_size, st.st_mtime_ns, digest),
        )
        self._conn.commit()

    def hash_with_cache(self, path: Path | str) -> str:
        """Return digest for ``path`` using the cache when possible."""
        cached = self.get(path)
        if cached is not None:
            return cached
        digest = sha256_file(path)
        self.put(path, digest)
        return digest

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    def __enter__(self) -> "HashCache":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


__all__ = [
    "HASH_PREFIX",
    "FS_FORBIDDEN",
    "sha256_file",
    "content_hash_bytes",
    "HashCache",
]
