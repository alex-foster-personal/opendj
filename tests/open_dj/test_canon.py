"""Tests for :mod:`apps.open_dj.canon` -- RFC 8785 JCS canonicalisation."""
from __future__ import annotations

import hashlib

import pytest

from apps.open_dj.canon import sha256_hex, to_canonical_bytes


@pytest.mark.requirement("OPEN-03")
class TestRfc8785:
    """RFC 8785 canonical JSON vectors."""

    def test_keys_sorted_lexicographically(self) -> None:
        """Section 3.2.3: keys are UTF-16 code-unit order, which for pure
        ASCII keys is the same as lexicographic order."""
        assert to_canonical_bytes({"b": 1, "a": 2}) == b'{"a":2,"b":1}'

    def test_nested_keys_sorted(self) -> None:
        out = to_canonical_bytes({"z": {"b": 1, "a": 2}, "a": 1})
        assert out == b'{"a":1,"z":{"a":2,"b":1}}'

    def test_integer_short_form(self) -> None:
        """RFC 8785 uses the shortest integer representation."""
        assert to_canonical_bytes({"n": 100}) == b'{"n":100}'
        assert to_canonical_bytes({"n": -0}) == b'{"n":0}'

    def test_float_short_form(self) -> None:
        assert to_canonical_bytes({"n": 1.5}) == b'{"n":1.5}'

    def test_unicode_strings_preserved(self) -> None:
        """Non-ASCII strings are emitted as literal UTF-8 bytes, not \\u-escapes."""
        out = to_canonical_bytes({"t": "カリブ"})
        assert "カリブ".encode("utf-8") in out

    def test_boolean_and_null(self) -> None:
        assert to_canonical_bytes({"a": True, "b": False, "c": None}) == \
            b'{"a":true,"b":false,"c":null}'

    def test_empty_structures(self) -> None:
        assert to_canonical_bytes({}) == b"{}"
        assert to_canonical_bytes([]) == b"[]"


@pytest.mark.requirement("OPEN-03")
class TestIdempotent:
    """Canonicalisation of an already-canonical doc is a no-op."""

    def test_canon_idempotent_minimal(self, minimal_bytes: bytes, minimal_doc: dict) -> None:
        # The raw file bytes are canonical; re-dumping the parsed doc yields
        # the same bytes.
        assert to_canonical_bytes(minimal_doc) == minimal_bytes

    def test_canon_idempotent_full(self, full_bytes: bytes, full_doc: dict) -> None:
        assert to_canonical_bytes(full_doc) == full_bytes

    def test_sha256_stable_across_calls(self, full_doc: dict) -> None:
        """sha256(canon(doc)) is stable across calls."""
        hashes = {sha256_hex(full_doc) for _ in range(5)}
        assert len(hashes) == 1


# REQ: OPEN-03
@pytest.mark.requirement("OPEN-03")
def test_sha256_hex_matches_raw_hash(full_bytes: bytes, full_doc: dict) -> None:
    """sha256_hex(doc) == sha256(canon_bytes) (independent spelling)."""
    expected = hashlib.sha256(full_bytes).hexdigest()
    assert sha256_hex(full_doc) == expected


@pytest.mark.requirement("OPEN-01")
def test_x_extension_survives_canon_roundtrip(ext_doc: dict) -> None:
    """x_* keys must survive a canon -> parse -> canon cycle unchanged."""
    import json as _json
    canon_a = to_canonical_bytes(ext_doc)
    reparsed = _json.loads(canon_a)
    canon_b = to_canonical_bytes(reparsed)
    assert canon_a == canon_b
    # And the marker itself is intact.
    assert reparsed["x_muxlab_test_marker"] is True
    assert reparsed["tracks"][0]["x_muxlab_preview_waveform"] == "mock-b64"
