"""Round-trip + invariant tests for :mod:`apps.sync.tsaf_writer` (SYNC-06)."""
from __future__ import annotations

import pytest

from apps.shared.djay_db import extract_rating_from_tsaf
from apps.sync.tsaf_writer import (
    TAIL_SCAN_BYTES,
    write_rating_to_tsaf,
    verify_rating_round_trip,
)


pytestmark = pytest.mark.requirement("SYNC-06")


# ---------------------------------------------------------------------------
# Fixture-ish blob builders
# ---------------------------------------------------------------------------


def _blob_with_rating(rating: int, suffix_len: int = 4) -> bytes:
    """Blob shape: 40 bytes of body + last 30 bytes containing 0x0f <rating>
    followed by ``suffix_len`` zero padding bytes."""
    body = bytes(range(40))
    tail = bytes([0x0F, rating]) + b"\x00" * suffix_len
    pad = b"\x00" * max(0, TAIL_SCAN_BYTES - len(tail))
    return body + pad + tail


def _blob_no_rating() -> bytes:
    return bytes(range(40)) + b"\x00" * 30


class TestReplaceExisting:
    def test_replace_in_place_keeps_length(self) -> None:
        blob = _blob_with_rating(5)
        out = write_rating_to_tsaf(blob, 3)
        assert len(out) == len(blob)
        assert extract_rating_from_tsaf(out) == 3

    def test_replace_preserves_surrounding_bytes(self) -> None:
        blob = _blob_with_rating(5)
        out = write_rating_to_tsaf(blob, 2)
        # The 40-byte prefix is untouched.
        assert out[:40] == blob[:40]

    def test_remove_rating_by_writing_zero(self) -> None:
        blob = _blob_with_rating(4)
        out = write_rating_to_tsaf(blob, 0)
        assert len(out) == len(blob) - 2
        assert extract_rating_from_tsaf(out) == 0


class TestInsertWhenAbsent:
    def test_insert_new_rating(self) -> None:
        blob = _blob_no_rating()
        out = write_rating_to_tsaf(blob, 4)
        assert len(out) == len(blob) + 2
        assert extract_rating_from_tsaf(out) == 4

    def test_insert_zero_is_noop(self) -> None:
        blob = _blob_no_rating()
        out = write_rating_to_tsaf(blob, 0)
        assert out == blob


class TestRoundTrip:
    @pytest.mark.parametrize("rating", [1, 2, 3, 4, 5])
    def test_round_trip_reads_back(self, rating: int) -> None:
        blob = _blob_with_rating(5)
        assert verify_rating_round_trip(blob, rating)

    def test_delta_constrained_to_minus_plus_or_zero(self) -> None:
        """|len(out) - len(blob)| is always in {-2, 0, +2}."""
        for rating_before in [0, 3, 5]:
            blob = (
                _blob_with_rating(rating_before)
                if rating_before
                else _blob_no_rating()
            )
            for rating_after in [0, 1, 4, 5]:
                out = write_rating_to_tsaf(blob, rating_after)
                delta = len(out) - len(blob)
                assert delta in (-2, 0, 2), (rating_before, rating_after, delta)

    def test_reversibility(self) -> None:
        blob = _blob_with_rating(4)
        intermediate = write_rating_to_tsaf(blob, 2)
        restored = write_rating_to_tsaf(intermediate, 4)
        assert restored == blob


class TestValidation:
    def test_rejects_rating_out_of_range(self) -> None:
        with pytest.raises(ValueError):
            write_rating_to_tsaf(_blob_no_rating(), 6)
        with pytest.raises(ValueError):
            write_rating_to_tsaf(_blob_no_rating(), -1)

    def test_rejects_non_bytes(self) -> None:
        with pytest.raises(TypeError):
            write_rating_to_tsaf("not bytes", 3)  # type: ignore[arg-type]
