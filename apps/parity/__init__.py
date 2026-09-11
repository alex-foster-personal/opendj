"""PARITY-01 per-lane scoring against rekordbox ground truth.

Round 0 scores BPM and Key. Round 1 adds waveform preview, detail, and
tri-band, phrase (PSSI), plus cues_db and cues_anlz. Beatgrid and downbeat
are owned by BEATMAP-01 and are delegated, never restated. Remaining lanes
are reported as `not_scored_this_round` with a named ungradable bucket,
never as agreement and never as a miss. No lane is described as matching a
calibrated threshold: thresholds are the maintainer-gated.
"""

from __future__ import annotations

SCORER_VERSION = "1.1.0"

__all__ = ["SCORER_VERSION"]
