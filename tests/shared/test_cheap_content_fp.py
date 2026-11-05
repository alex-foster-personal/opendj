"""Cheap cache-identity fingerprint: size plus first and last 64 KiB."""

from __future__ import annotations

from pathlib import Path

from apps.shared.cheap_content_fp import (
    DIGEST_SIZE,
    WINDOW,
    content_fp_from_bytes,
    content_fp_from_path,
)


def _assert_hex_digest(digest: str) -> None:
    assert isinstance(digest, str)
    assert len(digest) == 2 * DIGEST_SIZE
    assert digest == digest.lower()
    bytes.fromhex(digest)


def test_path_and_bytes_agree_on_a_short_file(tmp_path: Path) -> None:
    data = b"short-audio-bytes"
    path = tmp_path / "short.bin"
    path.write_bytes(data)
    digest = content_fp_from_path(path)
    assert digest == content_fp_from_bytes(data)
    assert digest == content_fp_from_bytes(data)
    _assert_hex_digest(digest)


def test_path_and_bytes_agree_on_a_file_larger_than_two_windows(tmp_path: Path) -> None:
    data = bytes((i * 31) % 256 for i in range(2 * WINDOW + 100))
    path = tmp_path / "long.bin"
    path.write_bytes(data)
    digest = content_fp_from_path(path, size=len(data))
    assert digest == content_fp_from_bytes(data)
    _assert_hex_digest(digest)


def test_flipping_a_byte_in_the_first_window_changes_the_digest() -> None:
    data = bytearray(b"\x01" * (WINDOW + 1))
    original = content_fp_from_bytes(bytes(data))
    data[0] ^= 0xFF
    assert content_fp_from_bytes(bytes(data)) != original


def test_flipping_a_byte_in_the_last_window_changes_the_digest() -> None:
    data = bytearray(b"\x02" * (2 * WINDOW + 8))
    original = content_fp_from_bytes(bytes(data))
    data[-1] ^= 0xFF
    assert content_fp_from_bytes(bytes(data)) != original


def test_empty_file_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "empty.bin"
    path.write_bytes(b"")
    digest = content_fp_from_path(path)
    assert digest == content_fp_from_bytes(b"")
    _assert_hex_digest(digest)


def test_middle_bytes_outside_the_windows_are_not_sampled() -> None:
    data = bytearray(b"\x03" * (2 * WINDOW + 16))
    original = content_fp_from_bytes(bytes(data))
    data[WINDOW + 1] ^= 0xFF
    assert content_fp_from_bytes(bytes(data)) == original
