"""Migration 18 -> 19: tag-independent audio identity for CLOUDSYNC-07."""
from __future__ import annotations

_V19: list[str] = [
    "ALTER TABLE tracks ADD COLUMN audio_hash TEXT",
    "CREATE INDEX IF NOT EXISTS idx_tracks_audio_hash ON tracks(audio_hash) "
    "WHERE audio_hash IS NOT NULL",
]

__all__ = ["_V19"]
