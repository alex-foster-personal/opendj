"""Tests for apps.shared.hashing."""
from __future__ import annotations

import hashlib
import sqlite3

import pytest

from apps.shared.hashing import HASH_PREFIX, HashCache, content_hash_bytes, sha256_file


@pytest.mark.requirement("CAT-02")
def test_sha256_file_matches_hashlib(tmp_path) -> None:
    f = tmp_path / "x.bin"
    f.write_bytes(b"hello, usb")
    expected = HASH_PREFIX + hashlib.sha256(b"hello, usb").hexdigest()
    assert sha256_file(f) == expected


@pytest.mark.requirement("CAT-02")
def test_content_hash_bytes() -> None:
    assert content_hash_bytes(b"") == HASH_PREFIX + hashlib.sha256(b"").hexdigest()


@pytest.mark.requirement("CAT-02")
def test_cache_hit_and_invalidation(tmp_path) -> None:
    f = tmp_path / "a.bin"
    f.write_bytes(b"first")
    cache_db = tmp_path / "cache.sqlite"
    with HashCache(cache_db) as c:
        first = c.hash_with_cache(f)
        assert first.startswith(HASH_PREFIX)
        # Hit: same file, same stat -> still cached.
        cached = c.get(f)
        assert cached == first
        # Invalidate by changing content (size changes).
        f.write_bytes(b"different and longer")
        # stat changes -> cache miss.
        assert c.get(f) is None
        second = c.hash_with_cache(f)
        assert second != first


@pytest.mark.requirement("CAT-02")
def test_cache_missing_file_returns_none(tmp_path) -> None:
    cache_db = tmp_path / "cache.sqlite"
    with HashCache(cache_db) as c:
        assert c.get(tmp_path / "nope.bin") is None


class _SpyConnection:
    """Wrapper around a real sqlite3.Connection that tracks close() calls."""

    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real
        self.close_called = False

    def close(self) -> None:
        self.close_called = True
        self._real.close()

    def __getattr__(self, name: str) -> object:
        return getattr(self._real, name)


class TestHashCacheConnLeak:
    """If _ensure_schema raises during __init__, conn must be closed."""

    def test_conn_closed_when_ensure_schema_raises(self, tmp_path) -> None:
        """if HashCache.__init__ _ensure_schema raises then conn is leaked - broken"""
        import apps.shared.hashing as hashing_mod
        from unittest.mock import patch

        cache_db = tmp_path / "cache.sqlite"
        spy: _SpyConnection | None = None

        original_connect = sqlite3.connect

        def patched_connect(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal spy
            real = original_connect(*args, **kwargs)
            spy = _SpyConnection(real)
            return spy

        with (
            patch.object(hashing_mod.sqlite3, "connect", side_effect=patched_connect),
            patch.object(
                HashCache,
                "_ensure_schema",
                side_effect=RuntimeError("schema fail"),
            ),
        ):
            with pytest.raises(RuntimeError, match="schema fail"):
                HashCache(cache_db)
            assert spy is not None
            assert spy.close_called, (
                "conn.close() was never called after _ensure_schema failure"
            )

    def test_no_journal_left_after_init_failure(self, tmp_path) -> None:
        """if HashCache.__init__ fails then no WAL journal file remains"""
        from unittest.mock import patch

        cache_db = tmp_path / "cache.sqlite"

        with patch.object(
            HashCache,
            "_ensure_schema",
            side_effect=RuntimeError("schema fail"),
        ):
            with pytest.raises(RuntimeError, match="schema fail"):
                HashCache(cache_db)

        wal_file = cache_db.parent / (cache_db.name + "-wal")
        shm_file = cache_db.parent / (cache_db.name + "-shm")
        # After close, WAL/SHM files should not persist (or should be empty)
        if wal_file.exists():
            assert wal_file.stat().st_size == 0, "WAL file not cleaned up"
        if shm_file.exists():
            assert shm_file.stat().st_size == 0, "SHM file not cleaned up"
