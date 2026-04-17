"""OPEN-01b stable_id algorithm tests.

Vectors pinned against the current algorithm output; deviating would
silently re-key every library.

Note on the open-dj strawman's example hash: the strawman section 4.2
shows ``track_id = "7d865e959b2466918c9863afca942d0fb89d7c9a"`` next to
``isrc = "GBCEN0900132"``. That value is a placeholder, not a real
``sha1("GBCEN0900132")`` digest. This test uses the real digest so the
actual algorithm is the regression anchor.
"""
from __future__ import annotations

import hashlib

import pytest

from apps.shared.state import ids


pytestmark = pytest.mark.requirement("OPEN-01b")


CANONICAL_ISRC = "GBCEN0900132"
CANONICAL_DIGEST = hashlib.sha1(CANONICAL_ISRC.encode("ascii")).hexdigest()


def test_tier1_isrc_canonical() -> None:
    digest, tier = ids.stable_id(isrc=CANONICAL_ISRC)
    assert tier == "isrc"
    assert digest == CANONICAL_DIGEST
    assert len(digest) == 40
    assert digest == digest.lower()
    assert all(c in "0123456789abcdef" for c in digest)


def test_tier1_normalises_lowercase() -> None:
    digest, tier = ids.stable_id(isrc="gbcen0900132")
    assert tier == "isrc"
    assert digest == CANONICAL_DIGEST


def test_tier1_normalises_hyphenated() -> None:
    digest, tier = ids.stable_id(isrc="GB-CEN-09-00132")
    assert tier == "isrc"
    assert digest == CANONICAL_DIGEST


def test_tier1_normalises_spaces_and_mixed_case() -> None:
    digest, tier = ids.stable_id(isrc="gb CEN 09 00132")
    assert tier == "isrc"
    assert digest == CANONICAL_DIGEST


def test_tier1_rejects_wrong_length_falls_to_path() -> None:
    digest, tier = ids.stable_id(isrc="GBCEN090013", abs_path="/music/x.mp3", mtime=1.0)
    assert tier == "inferred"
    assert digest == hashlib.sha1(b"/music/x.mp3|1.0").hexdigest()


def test_tier1_rejects_non_alphanumeric_only() -> None:
    _, tier = ids.stable_id(isrc="----", abs_path="/music/y.mp3", mtime=2.0)
    assert tier == "inferred"


def test_tier1_rejects_too_long() -> None:
    _, tier = ids.stable_id(isrc="GBCEN09001322", abs_path="/music/z.mp3", mtime=3.0)
    assert tier == "inferred"


def test_tier1_normalise_isrc_helper() -> None:
    assert ids.normalise_isrc("GB-CEN-09-00132") == "GBCEN0900132"
    assert ids.normalise_isrc("gbcen0900132") == "GBCEN0900132"
    assert ids.normalise_isrc("") is None
    assert ids.normalise_isrc(None) is None
    assert ids.normalise_isrc("GBCEN090013") is None
    assert ids.normalise_isrc("GBCEN09001AB") is None


def test_tier2_happy_path() -> None:
    fp = "A" * 64 + "IGNORED"
    digest, tier = ids.stable_id(
        isrc=None, fingerprint=fp, duration_ms=634000, size_bytes=12_345_678,
    )
    assert tier == "fingerprint"
    expected = hashlib.sha1(
        f"{'A' * 64}|634000|12345678".encode("utf-8")
    ).hexdigest()
    assert digest == expected


def test_tier2_uses_fingerprint_prefix_64() -> None:
    fp_a = "B" * 64 + "suffix_one"
    fp_b = "B" * 64 + "suffix_two"
    d_a, _ = ids.stable_id(isrc=None, fingerprint=fp_a, duration_ms=123, size_bytes=456)
    d_b, _ = ids.stable_id(isrc=None, fingerprint=fp_b, duration_ms=123, size_bytes=456)
    assert d_a == d_b


def test_tier2_requires_duration_and_size() -> None:
    _, tier = ids.stable_id(
        isrc=None, fingerprint="X" * 64, duration_ms=None,
        size_bytes=10, abs_path="/a.mp3", mtime=1.0,
    )
    assert tier == "inferred"


def test_isrc_missing_falls_to_fingerprint() -> None:
    _, tier = ids.stable_id(
        isrc=None, fingerprint="C" * 64, duration_ms=100, size_bytes=200,
    )
    assert tier == "fingerprint"


def test_empty_string_isrc_falls_to_fingerprint() -> None:
    _, tier = ids.stable_id(
        isrc="", fingerprint="D" * 64, duration_ms=100, size_bytes=200,
    )
    assert tier == "fingerprint"


def test_tier3_path_mtime_fallback() -> None:
    digest, tier = ids.stable_id(
        isrc=None, abs_path="/music/song.mp3", mtime=1700000000.0
    )
    assert tier == "inferred"
    assert digest == hashlib.sha1(
        b"/music/song.mp3|1700000000.0"
    ).hexdigest()


def test_tier3_marks_inferred_source() -> None:
    _, tier = ids.stable_id(isrc=None, abs_path="/x", mtime=0.0)
    assert tier == "inferred"


def test_tier3_streaming_empty_path_is_accepted() -> None:
    d1, t1 = ids.stable_id(isrc=None, abs_path="", mtime=0.0)
    d2, t2 = ids.stable_id(isrc=None, abs_path="", mtime=0.0)
    assert t1 == t2 == "inferred"
    assert d1 == d2


def test_all_missing_raises() -> None:
    with pytest.raises(ValueError):
        ids.stable_id(
            isrc=None, fingerprint=None, duration_ms=None,
            size_bytes=None, abs_path=None, mtime=None,
        )
