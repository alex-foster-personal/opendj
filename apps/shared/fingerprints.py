"""Chromaprint fingerprint helpers (Phase 7 dedup).

Small, pure, reusable wrapper around pyacoustid. The real work is done by
the external ``fpcalc`` CLI (install via ``brew install chromaprint``);
pyacoustid's ``fingerprint_file`` shells out to it and returns
``(duration, fingerprint_str)``.

Design notes:

* We cache fingerprints in a SQLite file keyed by
  ``(abs_path, size, mtime)`` so re-scans are O(rows) instead of
  O(rows * ~1s-of-fpcalc). The cache also persists duration / bitrate
  because we use them for the cross-bitrate-twin similarity guard.
* ``compute`` raises :class:`ChromaprintMissing` when fpcalc is not on
  PATH. Callers surface the friendly install message from
  ``scripts/check-chromaprint.sh``.
* ``compare`` is a small Hamming-distance fraction over the raw
  fingerprint bytes. pyacoustid ships a similar helper but only for its
  Web API response; we reimplement the 32-bit-word Hamming distance
  here so we do not need an online account.

The module is intentionally side-effect-free: no prints, no logging
configuration; callers wire in ``rich`` progress at the CLI layer.
"""
from __future__ import annotations

import base64
import sqlite3
import struct
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

# pyacoustid import is lazy so the module can be imported even when the
# package is missing (useful for test environments that stub it out). The
# real ``compute`` call will raise ``ChromaprintMissing`` if fpcalc is
# missing, so we never silently return bogus data.


class ChromaprintMissing(RuntimeError):
    """Raised when the external ``fpcalc`` CLI is unavailable.

    pyacoustid surfaces this as ``acoustid.NoBackendError`` or an
    ``OSError`` depending on version. We normalise to a single typed
    exception so callers can show the install-remediation message.
    """


# ------------------------------------------------------------------ types


@dataclass(frozen=True)
class Fingerprint:
    """A computed chromaprint fingerprint + the bits needed to cache it.

    ``fp_str`` is the base64-ish string returned by ``fpcalc`` /
    pyacoustid. ``duration`` is in seconds (float). ``size`` and
    ``mtime`` pin the cache key. ``bitrate`` and ``computed_at`` are
    stored for reporting / debugging but are not part of the identity.
    """

    path: Path
    duration: float
    fp_str: str
    size: int
    mtime: float
    bitrate: int | None = None
    computed_at: str | None = None


# ---------------------------------------------------------------- compute


def _require_acoustid():
    """Import pyacoustid or raise ChromaprintMissing.

    Separated so tests can monkeypatch the import path.
    """
    try:
        import acoustid  # type: ignore
    except Exception as exc:  # pragma: no cover -- pip install gate
        raise ChromaprintMissing(
            "pyacoustid is not installed. Add it to requirements.txt and "
            "run `pip install -r requirements.txt`."
        ) from exc
    return acoustid


def compute(path: Path) -> Fingerprint:
    """Compute a chromaprint fingerprint for ``path``.

    Raises
    ------
    ChromaprintMissing
        When the external ``fpcalc`` CLI is unavailable.
    FileNotFoundError
        When ``path`` does not exist.
    """
    if not path.exists():
        raise FileNotFoundError(str(path))
    acoustid = _require_acoustid()
    try:
        duration, fp = acoustid.fingerprint_file(str(path))
    except Exception as exc:
        # NoBackendError is the documented path; OSError sometimes leaks
        # through depending on platform. Normalise either way.
        name = type(exc).__name__
        if name in {"NoBackendError", "FingerprintGenerationError"} or isinstance(
            exc, (OSError, FileNotFoundError)
        ):
            raise ChromaprintMissing(
                f"fpcalc not available: {exc}. "
                "Install with `brew install chromaprint`."
            ) from exc
        raise
    if isinstance(fp, bytes):
        fp_str = fp.decode("ascii")
    else:
        fp_str = str(fp)
    st = path.stat()
    bitrate = _safe_bitrate(path)
    return Fingerprint(
        path=path,
        duration=float(duration),
        fp_str=fp_str,
        size=st.st_size,
        mtime=st.st_mtime,
        bitrate=bitrate,
    )


def _safe_bitrate(path: Path) -> int | None:
    """Return bitrate in kbps, or ``None`` if the tag reader cannot decode."""
    from apps.shared import _tagreader

    if not _tagreader.HAS_TAG_READER:
        return None
    try:
        kbps = _tagreader.read(path).bitrate
    except _tagreader.TagReadError:
        return None
    return int(kbps) if kbps else None


# ---------------------------------------------------------------- compare


def _decode(fp_str: str) -> list[int]:
    """Decode a chromaprint base64 string into a list of 32-bit ints.

    pyacoustid's fingerprint strings are url-safe base64 with a leading
    algorithm byte; we use ``acoustid.chromaprint.decode_fingerprint``
    when available, else a local decoder.
    """
    try:
        from acoustid.chromaprint import decode_fingerprint  # type: ignore

        decoded, _algo = decode_fingerprint(fp_str.encode("ascii"))
        return list(decoded)
    except Exception:
        # Fallback: strip URL-safe base64, read 32-bit big-endian words.
        padded = fp_str + "=" * (-len(fp_str) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        # First byte is algorithm; drop it. Then fold into 32-bit words.
        body = raw[1:]
        n = len(body) // 4
        if n == 0:
            return []
        return list(struct.unpack(f">{n}I", body[: n * 4]))


def compare(a: Fingerprint | str, b: Fingerprint | str) -> float:
    """Return similarity in ``[0.0, 1.0]`` for two fingerprints.

    Uses bitwise Hamming distance over the 32-bit words emitted by
    chromaprint. Different-length fingerprints align to the shorter of
    the two.
    """
    sa = a.fp_str if isinstance(a, Fingerprint) else a
    sb = b.fp_str if isinstance(b, Fingerprint) else b
    if sa == sb:
        return 1.0
    wa = _decode(sa)
    wb = _decode(sb)
    n = min(len(wa), len(wb))
    if n == 0:
        return 0.0
    bits = n * 32
    diff = 0
    for i in range(n):
        diff += bin(wa[i] ^ wb[i]).count("1")
    return 1.0 - (diff / bits)


# ---------------------------------------------------------------- cache


_CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS fingerprints (
    path         TEXT PRIMARY KEY,
    stable_id    TEXT,
    fingerprint  TEXT NOT NULL,
    duration     REAL NOT NULL,
    size         INTEGER NOT NULL,
    mtime        REAL NOT NULL,
    bitrate      INTEGER,
    computed_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_fingerprints_stable_id
    ON fingerprints(stable_id);
"""


class FingerprintCache:
    """Thin SQLite wrapper backing ``load_or_compute``.

    The cache key is ``(abs_path, size, mtime)``. A row that was cached
    with different size/mtime is considered stale and overwritten on the
    next compute.
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_CACHE_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def get(self, path: Path) -> Fingerprint | None:
        """Return a cached :class:`Fingerprint` if size + mtime match."""
        st = path.stat()
        with self._conn() as c:
            row = c.execute(
                "SELECT fingerprint, duration, size, mtime, bitrate, computed_at "
                "FROM fingerprints WHERE path = ?",
                (str(path),),
            ).fetchone()
        if row is None:
            return None
        fp_str, duration, size, mtime, bitrate, computed_at = row
        if size != st.st_size or abs(mtime - st.st_mtime) > 1e-3:
            return None
        return Fingerprint(
            path=path,
            duration=float(duration),
            fp_str=fp_str,
            size=int(size),
            mtime=float(mtime),
            bitrate=int(bitrate) if bitrate is not None else None,
            computed_at=computed_at,
        )

    def put(self, fp: Fingerprint, *, stable_id: str | None = None) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO fingerprints "
                "(path, stable_id, fingerprint, duration, size, mtime, bitrate, computed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(path) DO UPDATE SET "
                "  stable_id=excluded.stable_id, "
                "  fingerprint=excluded.fingerprint, "
                "  duration=excluded.duration, "
                "  size=excluded.size, "
                "  mtime=excluded.mtime, "
                "  bitrate=excluded.bitrate, "
                "  computed_at=CURRENT_TIMESTAMP",
                (
                    str(fp.path),
                    stable_id,
                    fp.fp_str,
                    fp.duration,
                    fp.size,
                    fp.mtime,
                    fp.bitrate,
                ),
            )
            c.commit()

    def iter_all(self) -> Iterable[Fingerprint]:
        with self._conn() as c:
            for row in c.execute(
                "SELECT path, fingerprint, duration, size, mtime, bitrate, computed_at "
                "FROM fingerprints"
            ):
                path, fp_str, duration, size, mtime, bitrate, computed_at = row
                yield Fingerprint(
                    path=Path(path),
                    duration=float(duration),
                    fp_str=fp_str,
                    size=int(size),
                    mtime=float(mtime),
                    bitrate=int(bitrate) if bitrate is not None else None,
                    computed_at=computed_at,
                )


def load_or_compute(
    path: Path, cache: FingerprintCache, *, force: bool = False
) -> Fingerprint:
    """Cache-aware compute. Returns a cached hit when available."""
    if not force:
        hit = cache.get(path)
        if hit is not None:
            return hit
    fp = compute(path)
    cache.put(fp)
    return fp


__all__ = [
    "ChromaprintMissing",
    "Fingerprint",
    "FingerprintCache",
    "compare",
    "compute",
    "load_or_compute",
]
