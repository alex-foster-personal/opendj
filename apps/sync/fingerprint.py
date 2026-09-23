"""Lazy + cached Chromaprint fingerprint layer for SYNC-02 signal 6.

The fingerprint compare is the strongest "same-audio-different-encoding"
signal but it's expensive (~1 track/s on M2) and requires ``fpcalc`` +
``libchromaprint`` to be installed system-wide. Per Phase 2 CONTEXT D4 we:

* Cache fingerprints in ``data/sync/fingerprints.sqlite`` keyed by absolute
  path, and only re-fingerprint if the cached row is missing or stale.
* Degrade silently (5-signal matching) when the native backend is absent.
  One :mod:`warnings` emission per process so logs stay clean.
* Only invoke :func:`compare_pair` for candidates that scored < 3 signals
  on the cheap checks; :mod:`apps.sync.matcher` wires that up.
"""
from __future__ import annotations

import sqlite3
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apps.sync.matcher import WEIGHTS, Signal

DEFAULT_FP_THRESHOLD: float = 0.90

_BACKEND_WARNED: bool = False


@dataclass(slots=True)
class FingerprintResult:
    """Fingerprint read result: duration + fingerprint string."""

    duration: float
    fingerprint: str


class FingerprintCache:
    """SQLite-backed fingerprint cache.

    Schema
    ------
    ``CREATE TABLE fingerprints (
        path TEXT PRIMARY KEY,
        duration REAL,
        fingerprint TEXT,
        computed_at REAL   -- epoch seconds
    )``

    The cache records the absent-fingerprint case (``fingerprint=''``) so
    we don't retry every run when the backend is missing.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(str(self.db_path))
        self._con.execute(
            """CREATE TABLE IF NOT EXISTS fingerprints (
                path TEXT PRIMARY KEY,
                duration REAL,
                fingerprint TEXT,
                computed_at REAL,
                mtime REAL,
                size INTEGER
            )"""
        )
        # Additive migration for pre-P02-F2 caches that lack mtime/size.
        existing_cols = {
            row[1]
            for row in self._con.execute("PRAGMA table_info(fingerprints)").fetchall()
        }
        if "mtime" not in existing_cols:
            self._con.execute("ALTER TABLE fingerprints ADD COLUMN mtime REAL")
        if "size" not in existing_cols:
            self._con.execute("ALTER TABLE fingerprints ADD COLUMN size INTEGER")
        self._con.commit()

    def close(self) -> None:
        try:
            self._con.close()
        except Exception:
            pass

    def __enter__(self) -> FingerprintCache:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- public API --------------------------------------------------------

    @staticmethod
    def _file_stat(path: str) -> tuple[float, int] | None:
        """Return ``(mtime, size)`` for ``path`` or None if unavailable."""
        try:
            st = Path(path).stat()
        except (OSError, ValueError):
            return None
        return float(st.st_mtime), int(st.st_size)

    def _lookup(self, path: str) -> tuple[float, str] | None:
        row = self._con.execute(
            "SELECT duration, fingerprint, mtime, size "
            "FROM fingerprints WHERE path = ?",
            (path,),
        ).fetchone()
        if row is None:
            return None
        duration, fingerprint, cached_mtime, cached_size = row
        current = self._file_stat(path)
        # Invalidate cache entry if the underlying file has changed on disk.
        # Missing file -> keep the cached "absent" row so we don't retry
        # every run. Missing stat metadata on the row (older schema) -> no
        # basis for invalidation, keep entry.
        if current is not None and cached_mtime is not None and cached_size is not None:
            if current[0] != float(cached_mtime) or current[1] != int(cached_size):
                self._con.execute(
                    "DELETE FROM fingerprints WHERE path = ?", (path,)
                )
                self._con.commit()
                return None
        return float(duration or 0.0), fingerprint or ""

    def _store(self, path: str, duration: float, fingerprint: str) -> None:
        import time

        stat = self._file_stat(path)
        mtime = stat[0] if stat is not None else None
        size = stat[1] if stat is not None else None
        self._con.execute(
            "INSERT OR REPLACE INTO fingerprints "
            "(path, duration, fingerprint, computed_at, mtime, size) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                path,
                float(duration or 0.0),
                fingerprint or "",
                time.time(),
                mtime,
                size,
            ),
        )
        self._con.commit()

    def get_or_compute(
        self, audio_path: Path | None
    ) -> tuple[float, str] | None:
        """Return ``(duration, fingerprint)`` for ``audio_path`` or ``None``.

        Cache hit returns the stored tuple (even if the fingerprint is an
        empty string, meaning "backend was absent / file unreadable last
        time"). Cache miss calls out to ``acoustid.fingerprint_file`` and
        records the result. On missing backend / file, stores an empty
        fingerprint and warns once per process.
        """
        if audio_path is None:
            return None
        path_str = str(audio_path)
        cached = self._lookup(path_str)
        if cached is not None:
            return cached

        try:
            import acoustid  # local import so module loads without the lib
        except Exception:
            _warn_backend_missing()
            self._store(path_str, 0.0, "")
            return (0.0, "")

        try:
            duration, fingerprint = acoustid.fingerprint_file(path_str)
            fp_str = fingerprint.decode() if isinstance(fingerprint, bytes) else str(fingerprint)
            self._store(path_str, float(duration or 0.0), fp_str)
            return (float(duration or 0.0), fp_str)
        except FileNotFoundError:
            self._store(path_str, 0.0, "")
            return (0.0, "")
        except Exception as exc:  # acoustid.NoBackendError, OSError, etc.
            if "NoBackendError" in type(exc).__name__:
                _warn_backend_missing()
            self._store(path_str, 0.0, "")
            return (0.0, "")

    def compare(
        self,
        a: Path | None,
        b: Path | None,
        *,
        threshold: float = DEFAULT_FP_THRESHOLD,  # noqa: ARG002 - keyword contract
    ) -> float | None:
        """Return similarity (0..1) for two audio files, or None if absent.

        ``None`` means "no verdict" (either file couldn't be fingerprinted).
        """
        if a is None or b is None:
            return None
        fp_a = self.get_or_compute(a)
        fp_b = self.get_or_compute(b)
        if not fp_a or not fp_b:
            return None
        dur_a, str_a = fp_a
        dur_b, str_b = fp_b
        if not str_a or not str_b:
            return None
        try:
            import acoustid

            score = acoustid.compare_fingerprints(
                (dur_a, str_a), (dur_b, str_b)
            )
            return float(score)
        except Exception:
            return None

    # -- matcher integration ----------------------------------------------

    def compare_pair_tracks(
        self, rb: Any, dj: Any, *, threshold: float = DEFAULT_FP_THRESHOLD
    ) -> Signal:
        """Return a ``chromaprint`` :class:`apps.sync.matcher.Signal`.

        Matches the signature expected by
        :func:`apps.sync.matcher.score_pair`'s ``fingerprint_fn``.
        """
        rb_path = getattr(rb, "file_path", None)
        dj_path = getattr(dj, "file_path", None)
        return compare_pair(rb_path, dj_path, cache=self, threshold=threshold)


def compare_pair(
    rb_path: Path | None,
    dj_path: Path | None,
    *,
    cache: FingerprintCache,
    threshold: float = DEFAULT_FP_THRESHOLD,
) -> Signal:
    """Compute the fingerprint :class:`Signal` for a candidate pair."""
    score = cache.compare(rb_path, dj_path, threshold=threshold)
    if score is None:
        return Signal(
            name="chromaprint",
            weight=WEIGHTS["chromaprint"],
            fired=False,
            detail="",
        )
    return Signal(
        name="chromaprint",
        weight=WEIGHTS["chromaprint"],
        fired=score >= threshold,
        detail=f"{score:.2f}",
    )


def _warn_backend_missing() -> None:
    global _BACKEND_WARNED
    if _BACKEND_WARNED:
        return
    _BACKEND_WARNED = True
    warnings.warn(
        "chromaprint/fpcalc backend missing; matcher degrades to 5 signals. "
        "Install with `brew install chromaprint`.",
        RuntimeWarning,
        stacklevel=2,
    )


__all__ = [
    "DEFAULT_FP_THRESHOLD",
    "FingerprintCache",
    "FingerprintResult",
    "compare_pair",
]
