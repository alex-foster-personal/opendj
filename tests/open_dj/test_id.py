"""Tests for :mod:`apps.open_dj.id` -- spec section 5 identity chain."""
from __future__ import annotations

import hashlib

import pytest

from apps.open_dj.id import compute_track_id, compute_track_id_with_tier


@pytest.mark.requirement("OPEN-01")
class TestTier1Isrc:
    """Tier 1: normalised ISRC."""

    def test_happy_path_sha1(self) -> None:
        tid, tier = compute_track_id_with_tier({"isrc": "GBCEN0900132"})
        assert tier == "isrc"
        assert tid == hashlib.sha1(b"GBCEN0900132").hexdigest()
        assert len(tid) == 40
        assert tid == "9bbb11637465090ce8135bcc3e66c0f00cf777fa"

    def test_isrc_normalised_lowercase(self) -> None:
        a = compute_track_id({"isrc": "gbcen0900132"})
        b = compute_track_id({"isrc": "GBCEN0900132"})
        assert a == b

    def test_isrc_with_punctuation_normalised(self) -> None:
        a = compute_track_id({"isrc": "gb-cen-09-00132"})
        b = compute_track_id({"isrc": "GBCEN0900132"})
        assert a == b

    def test_isrc_beats_fingerprint_fallback(self) -> None:
        """Tier 1 wins even when fingerprint triple is present."""
        _tid, tier = compute_track_id_with_tier({
            "isrc": "GBCEN0900132",
            "fingerprint": "Z" * 80,
            "duration_ms": 1000,
            "size_bytes": 1,
        })
        assert tier == "isrc"


@pytest.mark.requirement("OPEN-01")
class TestTier2Fingerprint:
    """Tier 2: chromaprint + duration + size."""

    def test_happy_path(self) -> None:
        tid, tier = compute_track_id_with_tier({
            "fingerprint": "A" * 80,
            "duration_ms": 634000,
            "size_bytes": 12345,
        })
        assert tier == "fingerprint"
        assert len(tid) == 40
        # Deterministic: first 64 chars of fp, then duration, size joined by |.
        expected = hashlib.sha1(f"{'A' * 64}|634000|12345".encode()).hexdigest()
        assert tid == expected

    def test_fingerprint_prefix_only_64_chars(self) -> None:
        """Fingerprints >64 chars must be truncated to 64."""
        short = compute_track_id({
            "fingerprint": "A" * 64,
            "duration_ms": 1000,
            "size_bytes": 1,
        })
        long = compute_track_id({
            "fingerprint": "A" * 64 + "Z" * 100,
            "duration_ms": 1000,
            "size_bytes": 1,
        })
        assert short == long

    def test_fingerprint_needs_all_three_keys(self) -> None:
        """Missing duration_ms falls through to tier 3."""
        _, tier = compute_track_id_with_tier({
            "fingerprint": "A" * 80,
            "absolute_path": "/x",
            "mtime": 1.0,
        })
        assert tier == "inferred"


@pytest.mark.requirement("OPEN-01")
class TestTier3Inferred:
    """Tier 3: path + mtime last resort."""

    def test_happy_path(self) -> None:
        tid, tier = compute_track_id_with_tier({
            "absolute_path": "/music/strobe.flac",
            "mtime": 1700000000.0,
        })
        assert tier == "inferred"
        assert len(tid) == 40

    def test_file_path_alias_works(self) -> None:
        """compute_track_id accepts file_path as alias for absolute_path."""
        a = compute_track_id({"file_path": "/music/x", "mtime": 123.0})
        b = compute_track_id({"absolute_path": "/music/x", "mtime": 123.0})
        assert a == b

    def test_no_signals_raises(self) -> None:
        with pytest.raises(ValueError):
            compute_track_id({})


@pytest.mark.requirement("OPEN-01")
def test_chromaprint_fingerprint_alias() -> None:
    """`chromaprint_fingerprint` key is an alias for `fingerprint`."""
    a = compute_track_id({
        "chromaprint_fingerprint": "A" * 80,
        "duration_ms": 1000,
        "size_bytes": 10,
    })
    b = compute_track_id({
        "fingerprint": "A" * 80,
        "duration_ms": 1000,
        "size_bytes": 10,
    })
    assert a == b


@pytest.mark.requirement("OPEN-01")
def test_determinism_repeated_call() -> None:
    """Same input MUST yield same output every call."""
    meta = {"isrc": "GBCEN0900132"}
    values = {compute_track_id(meta) for _ in range(10)}
    assert len(values) == 1


@pytest.mark.requirement("OPEN-01")
class TestCoercion:
    """Type coercion helpers cover str/int/float with odd inputs."""

    def test_numeric_isrc_coerced_to_str(self) -> None:
        """Non-string ISRC value is coerced but still validated; invalid -> None."""
        # Numeric ISRC makes no sense; should fall through to next tier.
        _, tier = compute_track_id_with_tier({
            "isrc": 12345,
            "absolute_path": "/x",
            "mtime": 1.0,
        })
        assert tier == "inferred"

    def test_bool_duration_rejected_falls_through(self) -> None:
        _, tier = compute_track_id_with_tier({
            "fingerprint": "A" * 80,
            "duration_ms": True,
            "size_bytes": 10,
            "absolute_path": "/x",
            "mtime": 1.0,
        })
        # True is bool (subclass of int) but our helper rejects bools.
        assert tier == "inferred"

    def test_string_duration_coerced(self) -> None:
        _, tier = compute_track_id_with_tier({
            "fingerprint": "A" * 80,
            "duration_ms": "1000",
            "size_bytes": "10",
        })
        assert tier == "fingerprint"

    def test_unparseable_mtime_defaults_to_zero(self) -> None:
        """Unparseable mtime -> None -> state.ids defaults to 0.0 (tier 3 OK)."""
        tid, tier = compute_track_id_with_tier({
            "absolute_path": "/x",
            "mtime": "not-a-number",
        })
        assert tier == "inferred"
        # Same result as passing mtime=0.0 directly.
        tid2, _ = compute_track_id_with_tier({"absolute_path": "/x", "mtime": 0.0})
        assert tid == tid2

    def test_float_path_coerced_to_str(self) -> None:
        """Edge: a Path-like value is str()-ified."""
        # Pass an int as path; works because _as_str falls back to str().
        a = compute_track_id({"absolute_path": 42, "mtime": 1.0})
        b = compute_track_id({"absolute_path": "42", "mtime": 1.0})
        assert a == b
