"""SHA-256 file hashing + persistent cache.

Used by:

* Phase 10 (USB sync) -- ``apps/sync/usb/diff.py``, ``apply.py``, and
  ``verify.py`` all compute the ``content_hash`` field defined in the
  open-dj v0 strawman (``sha256:<64-hex>``).
* Phase 11 (cloud sync) -- will reuse the same hashing + cache surface.
* ``scripts/build_waveform_bundle.py`` hashes each sampled track's AUDIO
  PAYLOAD (tags stripped) into the waveform fixture bundle's identity, so a
  repaired, relinked or re-encoded track changes ``bundle_id`` even when its
  path and rekordbox row do not (NATIVE-06, PR #1536).

Contract
--------

``sha256_file(path)`` -> ``"sha256:<64-hex>"`` (lowercase). Reads the
file in 1 MiB chunks via :func:`hashlib.file_digest` (Python 3.11+).

``sha256_audio_payload(path)`` -> ``"sha256:<64-hex>"``. Same shape, but it
hashes audio payload bytes while excluding tag/container metadata for MP3,
AIFF, FLAC, M4A and WAV. Unknown or malformed formats hash whole.

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
from typing import BinaryIO

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


def _id3v2_len(handle: BinaryIO) -> int:
    """Byte length of a leading ID3v2 tag at the handle's current position, or 0.

    ``handle`` is left at its original position; callers seek explicitly.
    Synchsafe size per the ID3v2 spec (each of the 4 size bytes uses only its
    low 7 bits), plus a 10-byte footer when the flags byte's bit 4 is set.
    """
    header = handle.read(10)
    if len(header) < 10 or header[:3] != b"ID3":
        return 0
    size = (
        ((header[6] & 0x7F) << 21)
        | ((header[7] & 0x7F) << 14)
        | ((header[8] & 0x7F) << 7)
        | (header[9] & 0x7F)
    )
    footer = 10 if (header[5] & 0x10) else 0
    return 10 + size + footer


def sha256_audio_payload(path: Path | str) -> str:
    """``sha256:<64-hex>`` of the audio payload, excluding container tags.

    A repair, relink or re-encode that only rewrites ID3 tags (rekordbox and
    Mixed In Key both do this on analysis) must not look like a different
    recording; a raw :func:`sha256_file` would count the tag bytes and change
    identity on every retag. mp3 is the one format here carrying tags INSIDE
    the audio file (ID3v2 leading, ID3v1 trailing 128 bytes starting ``TAG``);
    every other format (wav, flac, aiff, m4a, ...) is hashed whole, because
    their metadata lives in dedicated chunks/atoms this repo does not rewrite
    and does not need to look inside to skip. Same ID3-stripping convention as
    ``data/reference/mik/20260908/payload_hash.py``. Handles nested leading
    ID3v2 tags (a second tagger writing after one already did leaves two).
    """
    p = Path(path)
    with p.open("rb") as fh:
        data = fh.read()
    payload: bytes | None = None
    suffix = p.suffix.lower()
    if suffix == ".mp3":
        start = 0
        while start + 10 <= len(data) and data[start : start + 3] == b"ID3":
            size = sum(
                (data[start + 6 + index] & 0x7F) << (7 * (3 - index))
                for index in range(4)
            )
            start += 10 + size + (10 if data[start + 5] & 0x10 else 0)
        end = len(data) - 128 if data[-128:-125] == b"TAG" else len(data)
        payload = data[start:end]
    elif suffix == ".flac" and data[:4] == b"fLaC":
        offset = 4
        while offset + 4 <= len(data):
            last = bool(data[offset] & 0x80)
            block_len = int.from_bytes(data[offset + 1 : offset + 4], "big")
            offset += 4 + block_len
            if last:
                break
        payload = data[offset:]
    elif (
        suffix in {".aiff", ".aif", ".aifc"}
        and data[:4] == b"FORM"
        and data[8:12] in {b"AIFF", b"AIFC"}
    ):
        payload = _chunk_payload(data, b"SSND", skip=8, byteorder="big")
    elif suffix == ".wav" and data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        payload = _chunk_payload(data, b"data", byteorder="little")
    elif suffix in {".m4a", ".mp4", ".m4b"} and data[4:8] == b"ftyp":
        payload = _atom_payload(data, b"mdat")
    if payload is None:
        return sha256_file(p)
    return content_hash_bytes(payload)


def _chunk_payload(
    data: bytes, wanted: bytes, *, skip: int = 0, byteorder: str
) -> bytes | None:
    """Return concatenated payloads for RIFF/FORM chunks of ``wanted``."""
    offset = 12
    payloads: list[bytes] = []
    while offset + 8 <= len(data):
        name = data[offset : offset + 4]
        length = int.from_bytes(data[offset + 4 : offset + 8], byteorder)
        end = offset + 8 + length
        if end > len(data):
            return None
        if name == wanted and length >= skip:
            payloads.append(data[offset + 8 + skip : end])
        offset = end + (length & 1)
    return b"".join(payloads) if payloads else None


def _atom_payload(data: bytes, wanted: bytes) -> bytes | None:
    """Return all ``mdat`` atom bodies from an MP4/M4A file."""
    offset = 0
    payloads: list[bytes] = []
    while offset + 8 <= len(data):
        size = int.from_bytes(data[offset : offset + 4], "big")
        atom = data[offset + 4 : offset + 8]
        header = 16 if size == 1 else 8
        if size == 0:
            end = len(data)
        elif size == 1 and offset + 16 <= len(data):
            size = int.from_bytes(data[offset + 8 : offset + 16], "big")
            end = offset + size
        else:
            end = offset + size
        if end > len(data) or end < offset + header:
            return None
        if atom == wanted:
            payloads.append(data[offset + header : end])
        offset = end
    return b"".join(payloads) if payloads else None


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

    def __enter__(self) -> HashCache:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


__all__ = [
    "FS_FORBIDDEN",
    "HASH_PREFIX",
    "HashCache",
    "content_hash_bytes",
    "sha256_audio_payload",
    "sha256_file",
]
