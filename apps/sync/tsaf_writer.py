"""Byte-level TSAF writers for Phase 2 cautious rating sync (SYNC-06).

Only the rating field (``0x0f <uint8 1-5>`` in the last 30 bytes) is
mutated. Everything else in the blob -- playCount, colorIndex, tags,
nested arrays -- stays byte-identical because we never re-serialise TSAF;
we only surgically replace or insert the rating two-byte pair.

Round-trip invariant:

    write_rating_to_tsaf(write_rating_to_tsaf(blob, new), original) == blob

This is the single write primitive used by :mod:`apps.sync.apply_ratings`.
All writes are funnelled through here so the reverse-script can restore
the original blob verbatim.
"""
from __future__ import annotations

from apps.shared.djay_db import extract_rating_from_tsaf


TAIL_SCAN_BYTES: int = 30


def _locate_rating_tag_in_tail(blob: bytes) -> int | None:
    """Return the absolute offset of the first valid ``0x0f <1-5>`` in the
    last :data:`TAIL_SCAN_BYTES` bytes, or ``None``.
    """
    if not blob:
        return None
    start = max(0, len(blob) - TAIL_SCAN_BYTES)
    tail = blob[start:]
    for i in range(len(tail) - 1):
        if tail[i] == 0x0F:
            val = tail[i + 1]
            if 1 <= val <= 5:
                return start + i
    return None


def write_rating_to_tsaf(blob: bytes, new_rating: int) -> bytes:
    """Return a new TSAF blob with the rating set to ``new_rating``.

    Strategy
    --------

    1. Scan the last :data:`TAIL_SCAN_BYTES` bytes for an existing
       ``0x0f <uint8 1-5>`` pair.
    2. If found and ``new_rating > 0``, replace the value byte in place
       (length unchanged).
    3. If found and ``new_rating == 0``, remove the two-byte pair.
    4. If absent and ``new_rating > 0``, append ``0x0f <new_rating>``
       immediately before the final two trailing bytes (which are
       typically ``0x00 0x00`` in the observed live fixtures).
    5. If absent and ``new_rating == 0``, return the blob unchanged.

    Parameters
    ----------
    blob:
        Source TSAF payload from ``mediaItemUserData.data``.
    new_rating:
        Integer 0..5. ``0`` removes any existing rating marker.

    Returns
    -------
    bytes
        Mutated blob. ``len(out) - len(blob)`` is always in ``{-2, 0, +2}``.

    Raises
    ------
    ValueError
        If ``new_rating`` is outside ``[0, 5]``.
    """
    if not 0 <= new_rating <= 5:
        raise ValueError(f"new_rating must be in 0..5, got {new_rating!r}")
    if not isinstance(blob, (bytes, bytearray)):
        raise TypeError(f"blob must be bytes, got {type(blob).__name__}")

    blob_b = bytes(blob)
    tag_offset = _locate_rating_tag_in_tail(blob_b)

    if tag_offset is not None:
        # Existing rating marker. Replace or remove.
        if new_rating > 0:
            out = bytearray(blob_b)
            out[tag_offset + 1] = new_rating
            return bytes(out)
        # Remove the two-byte pair.
        return blob_b[:tag_offset] + blob_b[tag_offset + 2 :]

    # No existing marker.
    if new_rating == 0:
        return blob_b

    # Insert before the final two bytes (safe default; live fixtures end
    # with 0x00 0x00). If the blob is shorter than 2 bytes, append.
    insert_at = max(0, len(blob_b) - 2)
    return blob_b[:insert_at] + bytes([0x0F, new_rating]) + blob_b[insert_at:]


def verify_rating_round_trip(blob: bytes, new_rating: int) -> bool:
    """Apply :func:`write_rating_to_tsaf` and assert the reader observes it."""
    mutated = write_rating_to_tsaf(blob, new_rating)
    actual = extract_rating_from_tsaf(mutated)
    return actual == new_rating


__all__ = [
    "TAIL_SCAN_BYTES",
    "write_rating_to_tsaf",
    "verify_rating_round_trip",
]
