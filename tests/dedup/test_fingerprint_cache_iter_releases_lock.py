"""FingerprintCache.iter_all must not hold its lock while yielding (PR #6738)."""

from __future__ import annotations

import threading
from pathlib import Path

from apps.shared.fingerprints import Fingerprint, FingerprintCache


def _fp(path: Path) -> Fingerprint:
    return Fingerprint(
        path=path,
        duration=1.0,
        fp_str="AQAA",
        size=1,
        mtime=1.0,
        bitrate=None,
        computed_at="2026-11-02T00:00:00Z",
    )


def test_put_while_iterating_does_not_deadlock(tmp_path: Path) -> None:
    """[if] a caller writes to the cache for each fingerprint it iterates [then]
    the loop finishes instead of blocking on the cache lock, [else stop]."""
    cache = FingerprintCache(tmp_path / "fp.sqlite")
    cache.put(_fp(tmp_path / "a.mp3"))
    cache.put(_fp(tmp_path / "b.mp3"))
    done = threading.Event()

    def consume() -> None:
        for fp in cache.iter_all():
            cache.put(_fp(fp.path.with_suffix(".flac")))
        done.set()

    worker = threading.Thread(target=consume, daemon=True)
    worker.start()
    worker.join(timeout=10)
    assert done.is_set(), "iter_all held the cache lock across a yield"
    assert len(list(cache.iter_all())) == 4


def test_a_half_consumed_iterator_does_not_block_other_callers(tmp_path: Path) -> None:
    """[if] a caller keeps a partly consumed iterator [then] another thread can
    still write, [else stop]."""
    cache = FingerprintCache(tmp_path / "fp.sqlite")
    cache.put(_fp(tmp_path / "a.mp3"))
    cache.put(_fp(tmp_path / "b.mp3"))
    held = iter(cache.iter_all())
    next(held)
    wrote = threading.Event()

    def write() -> None:
        cache.put(_fp(tmp_path / "c.mp3"))
        wrote.set()

    writer = threading.Thread(target=write, daemon=True)
    writer.start()
    writer.join(timeout=10)
    assert wrote.is_set(), "a parked iter_all generator blocked put()"
