"""Structural constants and refusal hierarchy for the stretch-quality harness.

Binding spec: ``.planning/QUALITY-METHODOLOGY-RECONCILED.md``. Every amendment
that constrains a value is named at the value.

These are HARNESS INTEGRITY values, NOT quality thresholds. Quality thresholds
are set jointly across lanes after both harnesses run (spec preamble), and none
are hard-coded anywhere in this package.
"""

from __future__ import annotations

# These are HARNESS INTEGRITY values, not quality thresholds. Quality
# thresholds are set jointly across lanes after both harnesses run (spec
# preamble), and none are hard-coded anywhere in this package.

SAMPLE_RATE_HZ = 44_100

#: Amendment 5. Onset envelope hop. 64 samples = 1.451 ms at 44.1 kHz, which
#: is gate/8 against a 12 ms gate; librosa's default 512 would be 11.61 ms.
ONSET_HOP_SAMPLES = 64

#: PROPOSED, per lane A's harness. Not a pass/fail threshold: it is the
#: matching window that defines what "recovered" counts as, and it is reported
#: beside every recovery figure so a later joint decision can restate it.
ONSET_RECOVERY_GATE_MS = 12.0

#: Amendment 4. Relative to the REFERENCE PEAK, never absolute dBFS.
LSD_FLOOR_DB_BELOW_REF_PEAK = 80.0
LSD_FRAME_MS = 50.0

#: Amendment 7 guards. The refinement of Wed 19 Aug 2026 pins FOUR of them,
#: because the carrier fix alone proved insufficient: a rectified envelope keeps
#: ~8.5 percent ripple at 2f on a pure tone and correlation is scale-invariant,
#: so a carrier-only stack still cycle-skipped the sine at corr 0.999.
#:   (a) envelope-rise carrier at both stages  -> envelope_rise_carrier
#:   (b) coarse AND fine bound raise           -> AlignmentBoundError
#:   (c) correlation floor ON THE CARRIER      -> ALIGN_CORRELATION_FLOOR
#:   (d) reference self-similarity ceiling     -> ALIGN_SELF_SIMILARITY_CEILING
ALIGN_COARSE_MAX_LAG_MS = 250.0
ALIGN_FINE_HOP_SAMPLES = 4
#: Gate (c), and the value is CALIBRATED, not chosen: independent noise reads
#: 0.005 on the carrier and the worst real arm reads 0.148, so 0.05 separates
#: them with margin on both sides. The floor is meaningful only ON THE CARRIER:
#: a raw-waveform floor is unusable because the real baseline arm reads 0.012
#: raw, indistinguishable from noise, for the same phase-scrambling reason that
#: demoted residual out of the ranking metrics.
#:
#: This module previously floored at 0.20, which would have REFUSED the worst
#: real arm at 0.148 -- a floor set too high does not make the instrument
#: stricter, it silently deletes the rows nearest the decision.
ALIGN_CORRELATION_FLOOR = 0.05
#: Gate (d). A reference whose own carrier matches itself this well at some
#: other lag inside the search window cannot distinguish that lag from the true
#: one, so it RAISES instead of returning a confident wrong answer. This is what
#: catches the pure tone that survives gates (a) to (c).
ALIGN_SELF_SIMILARITY_CEILING = 0.95
ALIGN_AMBIGUITY_GUARD_MS = 15.0
#: Raises only when the best and runner-up peaks are INDISTINGUISHABLE, i.e.
#: when the correlation genuinely does not identify a delay. It is deliberately
#: NOT set tight enough to refuse ordinary repetitive dance music: measured on
#: 16th-note material at 122 BPM the one-period alias reads 0.83 against a true
#: peak of 1.00, so a tight ratio would refuse four of the six fixtures. The
#: spec's instruction for that regime is that align_correlation (and here also
#: peak_ratio and the runner-up lag) is the DISCRIMINATING OBSERVABLE, reported
#: per row for joint threshold-setting, not a unilateral raise.
ALIGN_AMBIGUITY_MAX_RATIO = 0.98

#: Envelope RMS window for the alignment carrier (2.9 ms at 44.1 kHz).
ENVELOPE_WINDOW_SAMPLES = 128

#: A carrier whose rise is this flat relative to its own level carries no
#: alignment information (an unmodulated tone). Refusing is the point.
CARRIER_ACTIVITY_FLOOR = 1e-3

#: Loud-non-silence assert. An iCloud-evicted stub serves empty rather than
#: erroring, so it decodes to digital silence and would otherwise be measured.
NON_SILENCE_MIN_RMS_DBFS = -60.0
NON_SILENCE_MIN_PEAK_DBFS = -40.0

#: Structural analogue of the librosa mel dead-band defect.
LOW_BAND_MAX_BIN_HZ = 30.0
LOW_BAND_MIN_BINS_BELOW_250HZ = 8

ONSET_FLUX_FFT_SAMPLES = 2048
ONSET_MIN_SEPARATION_MS = 30.0
ONSET_MATCH_WINDOW_MS = 50.0
ONSET_PEAK_FACTOR = 1.5
ONSET_PEAK_DELTA = 0.02
ONSET_PEAK_MEDIAN_WINDOW_MS = 100.0

# ----- amendment 8: master tempo / pitch ------------------------------------
# The tracker is a numpy YIN (cumulative mean normalised difference with
# parabolic interpolation). Proposed rather than assumed, with the calibration
# argument recorded in ops/quality/stretch/README.md: the metric is a
# DIFFERENTIAL of two contours tracked by the SAME tracker, so a systematic
# tracker bias cancels to first order, and the absolute accuracy that matters
# is verified against synthesised tones at exact known cent offsets.
#
# This hop is NOT the onset hop and amendment 5's gate/4 rule does not apply to
# it: gate/4 constrains ONSET TIMING resolution, and nothing here measures a
# time displacement.
PITCH_HOP_SAMPLES = 512
PITCH_FRAME_SAMPLES = 2048
PITCH_MIN_HZ = 65.0
PITCH_MAX_HZ = 1000.0
PITCH_YIN_THRESHOLD = 0.15
PITCH_CONFIDENCE_FLOOR = 0.75
#: Below this share of jointly voiced frames the fixture is not pitched enough
#: to carry the metric, which is why it is scoped to F2 and F5.
PITCH_MIN_VOICED_FRACTION = 0.10


# ----- structural failures --------------------------------------------------
# Every one of these is a REFUSAL TO PRODUCE A NUMBER. None of them is a
# quality verdict; a raise means the instrument could not measure this row, so
# the row is reported as a failure rather than as a plausible-looking value.


class HarnessIntegrityError(RuntimeError):
    """Base for every structural refusal in this module."""


class HopResolutionError(HarnessIntegrityError):
    """Amendment 5: onset envelope resolution coarser than gate/4."""


class SilentExcerptError(HarnessIntegrityError):
    """The excerpt is silent or near-silent (an evicted-stub decode)."""


class ChannelCollapseError(HarnessIntegrityError):
    """A distinct-channel source rendered to duplicated channels."""


class LowBandResolutionError(HarnessIntegrityError):
    """The flux STFT cannot resolve the bass region."""


class UnpitchedMaterialError(HarnessIntegrityError):
    """Too few jointly voiced frames to measure a pitch error (amendment 8)."""


class AlignmentError(HarnessIntegrityError):
    """Base for alignment refusals (amendment 7)."""


class DegenerateCarrierError(AlignmentError):
    """The envelope-rise carrier holds no alignment information."""


class AlignmentCorrelationFloorError(AlignmentError):
    """Best alignment correlation is below the floor."""


class AlignmentBoundError(AlignmentError):
    """A stage's argmax landed on its own search bound.

    Gate (b). At the coarse stage it means the true delay is outside the
    searched window, so the reported lag is a clipped guess; at the fine stage
    it means the two stages disagree.
    """


class ReferenceSelfSimilarityError(AlignmentError):
    """Gate (d): the reference is too periodic to align trustworthily.

    Its own carrier matches itself at some non-trivial lag inside the search
    window as well as it matches itself at zero, so no correlation-based
    estimator can tell those lags apart. Refusing beats returning a confident
    wrong lag, which is exactly what a pure tone did at corr 0.999.
    """


class AlignmentAmbiguousError(AlignmentError):
    """A runner-up correlation peak rivals the best one.

    This is the guard for quasi-periodic material: the spec records that a
    120 ms-early render can alias onto an interior peak on 16th-note-periodic
    music at 103-158 BPM without tripping a +-25 ms bound, so the bound alone
    is not proof of correct alignment. Peak dominance is.
    """
