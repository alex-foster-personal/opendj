"""Tests for :mod:`apps.shared.stable_id` (Phase 7 dedup helper).

The three tiers are deterministic -- same inputs always produce the same
digest. Cross-bitrate twins (same fingerprint prefix, same duration_ms,
different size_bytes) should still collide when callers pass only the
fingerprint tier inputs. That is the dedup invariant for META-03.
"""
from __future__ import annotations

import pytest

from apps.shared.stable_id import (
    ISRC_PATTERN,
    normalise_isrc,
    stable_id,
    stable_id_for,
    stable_id_str,
)

# --------------------------------------------------------------- ISRC tier


@pytest.mark.requirement("META-03")
def test_isrc_branch_deterministic() -> None:
    digest, tier = stable_id_for(isrc="USRC17607839", fingerprint=None)
    assert tier == "isrc"
    # Phase 5 canonical implementation returns a full 40-char sha1 hex.
    assert len(digest) == 40
    # Re-run must match.
    digest2, _ = stable_id_for(isrc="USRC17607839", fingerprint=None)
    assert digest == digest2


@pytest.mark.requirement("META-03")
def test_different_isrcs_different_ids() -> None:
    a, _ = stable_id_for(isrc="USRC17607839", fingerprint=None)
    b, _ = stable_id_for(isrc="GBUM71507078", fingerprint=None)
    assert a != b


@pytest.mark.requirement("META-03")
def test_normalise_isrc_strips_punctuation() -> None:
    assert normalise_isrc("US-RC1-76078-39") == "USRC17607839"
    assert normalise_isrc("usrc17607839") == "USRC17607839"
    assert normalise_isrc("bad") is None
    assert normalise_isrc(None) is None
    assert normalise_isrc("") is None


@pytest.mark.requirement("META-03")
def test_isrc_pattern_regex() -> None:
    assert ISRC_PATTERN.match("USRC17607839")
    assert not ISRC_PATTERN.match("USRC1760783")  # too short


# -------------------------------------------------------- fingerprint tier


@pytest.mark.requirement("META-03")
def test_fingerprint_branch_deterministic() -> None:
    digest, tier = stable_id_for(
        isrc=None, fingerprint="AQAAAAABC", duration_ms=180000, size_bytes=3_000_000
    )
    assert tier == "fingerprint"
    assert len(digest) == 40
    digest2, _ = stable_id_for(
        isrc=None, fingerprint="AQAAAAABC", duration_ms=180000, size_bytes=3_000_000
    )
    assert digest == digest2


@pytest.mark.requirement("META-03")
def test_fingerprint_ignores_suffix_beyond_64_chars() -> None:
    """The dedup invariant: fp prefix + duration + size drives identity."""
    base_fp = "A" * 64
    a, _ = stable_id_for(
        isrc=None, fingerprint=base_fp + "extra", duration_ms=1000, size_bytes=100
    )
    b, _ = stable_id_for(
        isrc=None, fingerprint=base_fp + "DIFFERENT_SUFFIX", duration_ms=1000, size_bytes=100
    )
    assert a == b, "suffix beyond 64 chars must not affect identity"


@pytest.mark.requirement("META-03")
def test_fingerprint_different_durations_give_different_ids() -> None:
    a, _ = stable_id_for(isrc=None, fingerprint="ABC", duration_ms=180000, size_bytes=10)
    b, _ = stable_id_for(isrc=None, fingerprint="ABC", duration_ms=181000, size_bytes=10)
    assert a != b


# ----------------------------------------------------------- inferred tier


@pytest.mark.requirement("META-03")
def test_inferred_branch_uses_path_mtime() -> None:
    digest, tier = stable_id_for(
        isrc=None, fingerprint=None, abs_path="/a/b/c.mp3", mtime=1700000000.0
    )
    assert tier == "inferred"
    assert len(digest) == 40


@pytest.mark.requirement("META-03")
def test_inferred_different_mtime_different_id() -> None:
    a, _ = stable_id_for(isrc=None, abs_path="/x.mp3", mtime=100.0)
    b, _ = stable_id_for(isrc=None, abs_path="/x.mp3", mtime=200.0)
    assert a != b


# -------------------------------------------------------------- tier order


@pytest.mark.requirement("META-03")
def test_isrc_wins_when_present_even_with_fingerprint() -> None:
    _digest, tier = stable_id_for(
        isrc="USRC17607839",
        fingerprint="AQAA",
        duration_ms=1000,
        size_bytes=10,
    )
    assert tier == "isrc"


@pytest.mark.requirement("META-03")
def test_stable_id_requires_some_input() -> None:
    with pytest.raises(ValueError):
        stable_id_for(isrc=None, fingerprint=None)


# -------------------------------------------------- convenience wrappers


@pytest.mark.requirement("META-03")
def test_stable_id_returns_tuple() -> None:
    out = stable_id(isrc="USRC17607839")
    assert isinstance(out, tuple) and len(out) == 2


@pytest.mark.requirement("META-03")
def test_stable_id_str_returns_digest_only() -> None:
    digest = stable_id_str(isrc="USRC17607839")
    assert isinstance(digest, str) and len(digest) == 40
