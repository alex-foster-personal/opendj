"""PARITY-01 per-lane scoring against rekordbox ground truth.

Round 1 scores BPM, Key, phrase (PSSI), cues_db, cues_anlz, and Vocal.
Beatgrid and downbeat are owned by BEATMAP-01 and are delegated, never
restated. Remaining lanes are reported as `not_scored_this_round` with a
named ungradable bucket, never as agreement and never as a miss. No lane is
described as matching a calibrated threshold: thresholds are the maintainer-gated.

Scorer 1.1.1 is a patch bump for merge 0526bbb3b. Combining the phrase, cues,
and vocal lane scorers into one score_payload() pass for the first time
changed every round's combined numeric_digest even though no individual
lane's scoring math changed. The version string was not bumped for that
combination before merge, so this bump plus a full re-pin closes the gap.
"""

from __future__ import annotations

SCORER_VERSION = "1.1.1"

__all__ = ["SCORER_VERSION"]
