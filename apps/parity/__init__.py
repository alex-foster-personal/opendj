"""PARITY-01 per-lane scoring against rekordbox ground truth.

Round 1 scores waveform preview, detail, and tri-band, phrase (PSSI), cues_db, cues_anlz, and Vocal.
Beatgrid and downbeat are owned by BEATMAP-01 and are delegated, never restated. No lane is described
as matching a calibrated threshold: thresholds are the maintainer-gated.
"""

from __future__ import annotations

SCORER_VERSION = "1.1.0"

__all__ = ["SCORER_VERSION"]
