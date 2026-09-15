"""Stable wire codes for hub-presigned ASR lyric transcript reads (ADR-0049)."""

from __future__ import annotations

LYRICS_ASR_NOT_FOUND = "LYRICS_ASR_NOT_FOUND"
LYRICS_ASR_PRESIGN_FAILED = "LYRICS_ASR_PRESIGN_FAILED"
LYRICS_ASR_HUB_UNREACHABLE = "LYRICS_ASR_HUB_UNREACHABLE"

LYRICS_ASR_PREFIX = "lyrics-asr/v1"


def lyrics_asr_object_key(stable_id: str) -> str:
    """Return the fixed R2 object key for one track's ASR transcript JSON."""
    return f"{LYRICS_ASR_PREFIX}/{stable_id}.json"


__all__ = [
    "LYRICS_ASR_HUB_UNREACHABLE",
    "LYRICS_ASR_NOT_FOUND",
    "LYRICS_ASR_PRESIGN_FAILED",
    "LYRICS_ASR_PREFIX",
    "lyrics_asr_object_key",
]
