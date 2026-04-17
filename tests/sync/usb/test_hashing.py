"""Tests for apps.shared.hashing."""
from __future__ import annotations

import hashlib

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
