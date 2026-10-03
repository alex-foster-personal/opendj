"""Audio fingerprints as relink evidence for a moved or renamed file.

The duplicate scan (:mod:`apps.dedup.library_scan`) stores each library
track's fingerprint in the dedup database, under the track's file path and
``stable_id``. When that file later goes missing, the stored fingerprint
is still the best description of what the track sounds like, so locate can
ask two questions of it:

* seeds: which files the scan has seen at OTHER paths carry the same
  audio? That finds a file that moved and was renamed, where no basename
  signal fires.
* confirmation: does a candidate found by name actually contain that
  audio? A match adds ``fingerprint_match``; clearly different audio adds
  ``fingerprint_mismatch``, which vetoes automatic apply.

Everything stays on the machine and read-only towards the music: candidate
files are only decoded, and the only writes go to the dedup database's own
fingerprint cache. A track the scan never fingerprinted has no recorded
fingerprint, and then neither signal fires: no evidence, not a verdict.
"""
from __future__ import annotations

import sqlite3
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from apps.shared import paths
from apps.shared.fingerprints import (
    ChromaprintMissing,
    FingerprintCache,
    compute,
    match,
    words,
)

# The duplicate finder's own threshold (apps/dedup/find_clusters.py
# DEFAULT_THRESHOLD): at or above it two files are the same recording.
MATCH_THRESHOLD = 0.92
# Below this the audio is plainly different. Re-encodes of one recording
# score above 0.95, different music near 0.5 (random bits); between the two
# lines neither signal fires.
MISMATCH_THRESHOLD = 0.7
# Sub-fingerprints a seed must share with the recorded one at one offset
# before it is worth scoring (the dedup index uses the same floor).
MIN_SHARED_WORDS = 3
# A value in more files than this says nothing about any one of them.
_STOPWORD_FILES = 50


def _norm(path: str) -> str:
    return unicodedata.normalize("NFC", path)


@dataclass
class FingerprintEvidence:
    """Recorded fingerprints from one dedup database, plus a word index over
    the files that still exist, built once and queried per broken row."""

    db_path: Path
    by_path: dict[str, str] = field(default_factory=dict)
    by_stable_id: dict[str, str] = field(default_factory=dict)
    sid_by_path: dict[str, str] = field(default_factory=dict)
    # Candidates whose fingerprint could not be measured (no backend, or a
    # file the decoder rejected): reported, never scored as a mismatch.
    unmeasured: int = 0
    # Files that still exist: (path, fingerprint, stable_id it belongs to).
    _live: list[tuple[Path, str, str | None]] = field(default_factory=list)
    # (values, files, positions), sorted by value; built on first use.
    _index: tuple[Any, Any, Any] | None = None
    _cache: FingerprintCache | None = None

    @classmethod
    def open(cls, db_path: Path | None = None) -> FingerprintEvidence | None:
        """Load ``db_path`` (the app's dedup database by default); None when
        no duplicate scan has ever written one."""
        use = db_path if db_path is not None else paths.DEDUP_FALLBACK_DB
        if not use.exists():
            return None
        ev = cls(db_path=use)
        conn = sqlite3.connect(f"file:{use}?mode=ro", uri=True)
        try:
            rows = conn.execute("SELECT path, stable_id, fingerprint FROM fingerprints").fetchall()
        except sqlite3.OperationalError:
            return None
        finally:
            conn.close()
        for path, sid, fp in rows:
            ev.by_path[_norm(path)] = fp
            if sid:
                ev.by_stable_id[str(sid)] = fp
                ev.sid_by_path[_norm(path)] = str(sid)
            p = Path(path)
            if p.exists():
                ev._live.append((p, fp, str(sid) if sid else None))
        return ev

    def _built_index(self) -> tuple[Any, Any, Any]:
        """Flat numpy arrays like the dedup index: about 10 bytes per
        sub-fingerprint, where Python lists would cost over 1 GB for a
        library of 8,000 tracks."""
        if self._index is not None:
            return self._index
        import numpy as np

        values, files, positions = [], [], []
        for i, (_p, fp, _sid) in enumerate(self._live):
            try:
                ws = np.asarray(words(fp), dtype=np.uint32)
            except ValueError:
                continue
            uniq, first = np.unique(ws, return_index=True)
            values.append(uniq)
            files.append(np.full(len(uniq), i, dtype=np.int32))
            positions.append(first.astype(np.int32))
        if not values:
            self._index = (np.empty(0, np.uint32), np.empty(0, np.int32), np.empty(0, np.int32))
            return self._index
        w = np.concatenate(values)
        order = np.argsort(w, kind="stable")
        self._index = (w[order], np.concatenate(files)[order], np.concatenate(positions)[order])
        return self._index

    def owner(self, original_path: str) -> str | None:
        """The stable_id the scan recorded for the track at ``original_path``,
        for a caller whose row names only the path (``broken.csv``)."""
        return self.sid_by_path.get(_norm(original_path)) if original_path else None

    def recorded(self, original_path: str, stable_id: str | None = None) -> str | None:
        """The fingerprint recorded for a track before its file went missing."""
        if stable_id and stable_id in self.by_stable_id:
            return self.by_stable_id[stable_id]
        return self.by_path.get(_norm(original_path)) if original_path else None

    def seeds(self, recorded: str, *, exclude: str = "", stable_id: str | None = None) -> list[Path]:
        """Existing files the scan fingerprinted that carry ``recorded``'s audio.

        A file another library track already owns is left out: that is a
        duplicate of this track (the duplicates page lists it), not where
        this track's own file went, and relinking to it would point two
        tracks at one file."""
        import numpy as np

        try:
            ws = np.asarray(words(recorded), dtype=np.uint32)
        except ValueError:
            return []
        w, f, p = self._built_index()
        uniq, first = np.unique(ws, return_index=True)
        lo = np.searchsorted(w, uniq, side="left")
        hi = np.searchsorted(w, uniq, side="right")
        votes: dict[tuple[int, int], int] = {}
        for a, b, pos in zip(lo.tolist(), hi.tolist(), first.tolist(), strict=True):
            if not 0 < b - a <= _STOPWORD_FILES:
                continue
            for i, ipos in zip(f[a:b].tolist(), p[a:b].tolist(), strict=True):
                key = (i, ipos - pos)
                votes[key] = votes.get(key, 0) + 1
        hits = {i for (i, _off), n in votes.items() if n >= MIN_SHARED_WORDS}
        out: list[Path] = []
        for i in sorted(hits):
            path, fp, owner = self._live[i]
            if _norm(str(path)) == _norm(exclude):
                continue
            if owner is not None and owner != stable_id:
                continue
            if match(recorded, fp)[0] >= MATCH_THRESHOLD:
                out.append(path)
        return out

    def similarity(self, recorded: str, candidate: Path) -> float | None:
        """How alike ``candidate``'s audio is to ``recorded``; None when it
        could not be measured."""
        if self._cache is None:
            self._cache = FingerprintCache(self.db_path)
        try:
            fp = self._cache.get(candidate)
            if fp is None:
                fp = compute(candidate)
                # Keep the track id a re-fingerprinted library file was stored under.
                self._cache.put(fp, stable_id=self.sid_by_path.get(_norm(str(candidate))))
            return match(recorded, fp)[0]
        except (ChromaprintMissing, OSError, RuntimeError, ValueError):
            self.unmeasured += 1
            return None

    def signal(self, recorded: str, candidate: Path) -> str | None:
        """``fingerprint_match``, ``fingerprint_mismatch`` or None (no verdict)."""
        sim = self.similarity(recorded, candidate)
        if sim is None:
            return None
        if sim >= MATCH_THRESHOLD:
            return "fingerprint_match"
        if sim < MISMATCH_THRESHOLD:
            return "fingerprint_mismatch"
        return None
