"""Cloud sync layer for music-dj-tools (Phase 11).

Provides:
  config    -- CloudConfig dataclass loaded from environment (Doppler).
  lock      -- cooperative single-writer lock stored as JSON on R2.
  replicate -- Litestream subprocess supervisor that holds the lock.
  s3_audio  -- opt-in audio object upload helpers (NOT enabled by default).

Ships a ``litestream.yml`` template + ``launchd`` plist for login startup.

Phase 11 requirement coverage: CAT-04, CAT-04a (DB sync), CAT-04b (audio).
"""
from __future__ import annotations

__all__ = ["config", "lock", "replicate", "s3_audio"]
