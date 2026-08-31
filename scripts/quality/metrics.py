"""Analysis primitives for the stretch-quality harness: the one import surface.

Binding spec: ``.planning/QUALITY-METHODOLOGY-RECONCILED.md``. This module is a
facade -- the code lives one module per measurement domain, and the amendment
that constrains each domain is named at its implementation site:

===============  ==========================================================
``contract``     structural constants and the refusal hierarchy
``pcm``          sha256-pinned sidecars, mono folding, excerpt asserts
``alignment``    amendments 6 and 7: the envelope-rise carrier, four gates
``spectral``     amendment 4: LSD floored below the reference peak, residual
``onsets``       amendments 5, 6 and 9: hop 64, rectified flux, linear basis
``pitch``        amendment 8: the master-tempo promise, measured in cents
===============  ==========================================================

Requirements (mini-PRD):
  ✔︎ ✅ 🎯 Align renders by measured integer delay on the half-wave-rectified
    envelope-RISE carrier at BOTH stages, never on raw waveform (amendment 7).
    [if] a pure unmodulated sine is aligned [then ⛔️] the call raises rather
      than returning the cycle-skipped lag at high reported confidence
    [if] two independent noise signals are aligned [then ⛔️] the correlation
      floor raises instead of reporting a lag
    [if] either stage's argmax lands on its search bound [then ⛔️] it raises
    [if] the reference's own carrier matches itself above the ceiling
      [then ⛔️] it raises rather than returning a confident wrong lag
  ✔︎ ✅ 🎯 Floor LSD bins 80 dB below the REFERENCE PEAK, never absolute
    (amendment 4).
    [if] a signal is compared against itself [then] LSD is 0 dB, not the
      empty-bin numerical noise an absolute floor produces
  ✔︎ ✅ 🎯 Raise when onset envelope resolution exceeds gate/4 (amendment 5).
    [if] hop 512 is used against a 12 ms gate [then ⛔️] it raises
    [if] hop 64 is used against a 12 ms gate [then] it is accepted
  ✔︎ ✅ 🎯 Detect low-band transients (amendment 9).
    [if] a 60 Hz kick with no energy above 250 Hz yields no onset [then ⛔️]
      the detector is deaf to the DJ common case
  ✔︎ ✅ 🎯 Assert loud non-silence on every excerpt.
    [if] an iCloud-evicted stub decodes to silence [then ⛔️] it raises
  ✔︎ ✅ 🎯 Assert channel DISTINCTNESS is preserved, not channel count.
    [if] a stereo source renders to bit-identical channels [then ⛔️] it raises

No librosa: the onset flux is a linear-frequency STFT, so the
``n_fft=512``/``n_mels=128`` dead-low-band trap cannot arise. Its structural
analogue is asserted instead by ``assert_low_band_resolution``.
"""

from __future__ import annotations

from scripts.quality.alignment import (
    Alignment,
    apply_delay,
    carrier_self_similarity,
    envelope_rise_carrier,
    envelope_rms,
    estimate_delay_samples,
    normalised_cross_correlation,
)
from scripts.quality.contract import (
    ALIGN_AMBIGUITY_GUARD_MS,
    ALIGN_AMBIGUITY_MAX_RATIO,
    ALIGN_COARSE_MAX_LAG_MS,
    ALIGN_CORRELATION_FLOOR,
    ALIGN_FINE_HOP_SAMPLES,
    ALIGN_SELF_SIMILARITY_CEILING,
    CARRIER_ACTIVITY_FLOOR,
    ENVELOPE_WINDOW_SAMPLES,
    LOW_BAND_MAX_BIN_HZ,
    LOW_BAND_MIN_BINS_BELOW_250HZ,
    LSD_FLOOR_DB_BELOW_REF_PEAK,
    LSD_FRAME_MS,
    NON_SILENCE_MIN_PEAK_DBFS,
    NON_SILENCE_MIN_RMS_DBFS,
    ONSET_FLUX_FFT_SAMPLES,
    ONSET_HOP_SAMPLES,
    ONSET_MATCH_WINDOW_MS,
    ONSET_MIN_SEPARATION_MS,
    ONSET_PEAK_DELTA,
    ONSET_PEAK_FACTOR,
    ONSET_PEAK_MEDIAN_WINDOW_MS,
    ONSET_RECOVERY_GATE_MS,
    PITCH_CONFIDENCE_FLOOR,
    PITCH_FRAME_SAMPLES,
    PITCH_HOP_SAMPLES,
    PITCH_MAX_HZ,
    PITCH_MIN_HZ,
    PITCH_MIN_VOICED_FRACTION,
    PITCH_YIN_THRESHOLD,
    SAMPLE_RATE_HZ,
    AlignmentAmbiguousError,
    AlignmentBoundError,
    AlignmentCorrelationFloorError,
    AlignmentError,
    ChannelCollapseError,
    DegenerateCarrierError,
    HarnessIntegrityError,
    HopResolutionError,
    LowBandResolutionError,
    ReferenceSelfSimilarityError,
    SilentExcerptError,
    UnpitchedMaterialError,
)
from scripts.quality.onsets import (
    OnsetComparison,
    assert_onset_hop_resolution,
    compare_onsets,
    onset_strength,
    pick_onsets,
)
from scripts.quality.pcm import (
    PcmMeta,
    assert_channel_distinctness_preserved,
    assert_loud_non_silence,
    channels_are_bit_identical,
    read_pcm,
    sha256_bytes,
    to_mono,
    write_pcm,
)
from scripts.quality.pitch import PitchError, pitch_error_cents, track_f0
from scripts.quality.spectral import (
    Residual,
    assert_low_band_resolution,
    log_spectral_distance,
    residual_dbr,
    stft_magnitude,
)

__all__ = [
    "ALIGN_AMBIGUITY_GUARD_MS",
    "ALIGN_AMBIGUITY_MAX_RATIO",
    "ALIGN_COARSE_MAX_LAG_MS",
    "ALIGN_CORRELATION_FLOOR",
    "ALIGN_FINE_HOP_SAMPLES",
    "ALIGN_SELF_SIMILARITY_CEILING",
    "CARRIER_ACTIVITY_FLOOR",
    "ENVELOPE_WINDOW_SAMPLES",
    "LOW_BAND_MAX_BIN_HZ",
    "LOW_BAND_MIN_BINS_BELOW_250HZ",
    "LSD_FLOOR_DB_BELOW_REF_PEAK",
    "LSD_FRAME_MS",
    "NON_SILENCE_MIN_PEAK_DBFS",
    "NON_SILENCE_MIN_RMS_DBFS",
    "ONSET_FLUX_FFT_SAMPLES",
    "ONSET_HOP_SAMPLES",
    "ONSET_MATCH_WINDOW_MS",
    "ONSET_MIN_SEPARATION_MS",
    "ONSET_PEAK_DELTA",
    "ONSET_PEAK_FACTOR",
    "ONSET_PEAK_MEDIAN_WINDOW_MS",
    "ONSET_RECOVERY_GATE_MS",
    "PITCH_CONFIDENCE_FLOOR",
    "PITCH_FRAME_SAMPLES",
    "PITCH_HOP_SAMPLES",
    "PITCH_MAX_HZ",
    "PITCH_MIN_HZ",
    "PITCH_MIN_VOICED_FRACTION",
    "PITCH_YIN_THRESHOLD",
    "SAMPLE_RATE_HZ",
    "Alignment",
    "AlignmentAmbiguousError",
    "AlignmentBoundError",
    "AlignmentCorrelationFloorError",
    "AlignmentError",
    "ChannelCollapseError",
    "DegenerateCarrierError",
    "HarnessIntegrityError",
    "HopResolutionError",
    "LowBandResolutionError",
    "OnsetComparison",
    "PcmMeta",
    "PitchError",
    "ReferenceSelfSimilarityError",
    "Residual",
    "SilentExcerptError",
    "UnpitchedMaterialError",
    "apply_delay",
    "assert_channel_distinctness_preserved",
    "assert_loud_non_silence",
    "assert_low_band_resolution",
    "assert_onset_hop_resolution",
    "carrier_self_similarity",
    "channels_are_bit_identical",
    "compare_onsets",
    "envelope_rise_carrier",
    "envelope_rms",
    "estimate_delay_samples",
    "log_spectral_distance",
    "normalised_cross_correlation",
    "onset_strength",
    "pick_onsets",
    "pitch_error_cents",
    "read_pcm",
    "residual_dbr",
    "sha256_bytes",
    "stft_magnitude",
    "to_mono",
    "track_f0",
    "write_pcm",
]
