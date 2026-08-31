/**
 * Player DSP constants.
 *
 * Extracted verbatim from audio-engine.svelte.ts (T4 S1). Values are the
 * contract: an EQ frequency, a smoothing time-constant or a schedule safety
 * margin changed here changes what the DJ hears, and Web Audio will not tell
 * you. Nothing in this module imports from the player, so it is a leaf.
 */

import type { DeckId } from '$lib/rb/deck-slots';

export const DECK_IDS: readonly DeckId[] = [1, 2, 3, 4] as const;

/** Pitch fader range in percent; 100 renders as WIDE in the jog readout. */
export type PitchRange = 8 | 16 | 100;
export const PITCH_RANGES: readonly PitchRange[] = [8, 16, 100] as const;

export const EQ_FREQ_LOW_HZ = 250;
export const EQ_FREQ_MID_HZ = 1200;
export const EQ_FREQ_HIGH_HZ = 5000;
export const EQ_MID_Q = 1.0;
/** Knob 0 -> full cut (DJ-mixer style deep cut), knob 1 -> gentle boost. */
export const EQ_MIN_DB = -26;
export const EQ_MAX_DB = 6;
/** TRIM knob 0..1 maps linearly to 0..2x amplitude (0.5 = unity). */
export const TRIM_MAX_GAIN = 2;
/** Smoothing time-constant for AudioParam changes (anti-zipper). */
export const PARAM_SMOOTH_S = 0.01;
/**
 * BEAT-SYNC ALIGNMENT margin, on top of the slower deck processor's reported
 * latency. It buys the coordinator room to plan several decks against ONE
 * shared context time and have every processor acknowledge before that time
 * arrives: a follower that misses the common instant is audibly out of phase,
 * not merely late. 100ms is sized for that multi-deck handshake.
 *
 * It is NOT the generic "do not schedule in the past" guard. Charging it to a
 * single-deck play/pause put ~100ms between the maintainer's click and the sound
 * (LATENCY-01 budget is 30ms p99), which is what
 * `TRANSPORT_IMMEDIATE_SAFETY_S` below exists to fix. Sync pays for sync;
 * plain transport does not.
 */
export const SYNC_SCHEDULE_SAFETY_S = 0.1;
/**
 * IMMEDIATE (Class A) transport not-in-the-past guard: the smallest margin that
 * keeps a scheduled change strictly ahead of the render quantum, once the deck's
 * own reported output latency has already been added by the caller.
 *
 * 8ms is about three 128-frame render quanta at 48kHz (~2.7ms each), and still
 * three at 44.1kHz (~2.9ms each), so the processor reliably receives the change
 * before it renders the block it lands in, while the margin itself stays inside
 * the LATENCY-01 30ms p99 budget. Nothing here aligns two decks - anything that
 * must agree on a shared instant passes `SYNC_SCHEDULE_SAFETY_S` explicitly.
 *
 * Name and value are shared verbatim with the sister rebuild lane's fix: one
 * concept, one name, so the two lanes cannot diverge on it.
 */
export const TRANSPORT_IMMEDIATE_SAFETY_S = 0.008;
/**
 * ONSET RAMP of a Signalsmith stretch processor, as a fraction of its STFT
 * block length. This is what an immediate transport schedule must lead by, and
 * round 2 replaced the processor's self-report with it.
 *
 * It is deliberately NOT that self-report. `latency()` reports the LIVE-INPUT
 * latency, and our decks are never in live-input mode: they play from loaded
 * buffers, on a worklet path that re-seeks the algorithm every render quantum
 * and so compensates the whole term itself. Measured: given enough lead, audio
 * arrives EXACTLY when scheduled with no trace of the reported 120ms, so
 * leading by 120ms was paying for a delay that does not exist.
 *
 * What a short lead actually costs is a SOFT START - the output ramps up rather
 * than arriving square. Measured offline at 44100Hz, lateness to 90 percent of
 * steady-state RMS is a straight line of slope -1 down to a floor:
 *
 *     lateness(lead) = max(0, RAMP - lead),   RAMP ~= 0.37 x blockMs
 *
 * fitted across six block sizes: 120 -> 43.4ms (0.36x), 60 -> 21.9ms (0.37x),
 * 40 -> 14.9ms (0.37x), 30 -> 11.4ms (0.38x), 20 -> 7.9ms (0.40x), 10 -> 3.8ms
 * (0.38x). The 90 percent point is used rather than the 50 percent point
 * (0.21x) because a start that has only reached half level is still audibly
 * soft.
 *
 * The value is 0.39 and NOT the 0.37 mean of that fit, which is the correction
 * the implementation-time verification bought. The lead has to COVER the ramp,
 * so the right constant is the largest knee observed, not the average one -
 * fitting the mean leaves the biggest block sitting just below its own knee.
 * Measured directly on the knee, same apparatus, reproducible bit-for-bit
 * across runs (lateness to 90 percent, against each block's own long-lead
 * baseline):
 *
 *   block 120: lead 44.4ms (0.37x) -> 4.53ms late;  46.8ms (0.39x) -> -0.04ms
 *   block  60: lead 22.2ms (0.37x) -> 0.00ms late;  23.4ms (0.39x) -> -0.04ms
 *   block  30: lead 11.1ms (0.37x) -> 1.39ms late;  11.7ms (0.39x) ->  0.48ms
 *   block  20: lead  7.4ms (0.37x) -> 0.94ms late;   7.8ms (0.39x) ->  1.36ms
 *
 * (long-lead baselines, i.e. the measurement noise floor: 0.96ms at block 120,
 * 1.41ms at 60, 1.54ms at 30, 1.25ms at 20.) At 0.39 every block is at or
 * inside its own baseline; at 0.37 the shipped 120ms block is 4.5ms soft, which
 * is the one place it mattered. The extra 2.4ms of lead is cheap against
 * removing 4.5ms of audible softness: what a DJ hears is the onset.
 *
 * `latency()` equals the block length exactly for this processor, so the ramp
 * is derivable from the number every deck already caches. Above the knee the
 * lead buys nothing measurable - lead 46.8ms and lead 128ms are indistinguish-
 * able at the shipped block - which is why charging the full self-report to a
 * plain play/pause was ~73ms of dead weight.
 *
 * Apparatus, the per-block sweep, the knee sweep and the falsification checks:
 * `.planning/latency-round2-design.md`.
 */
export const PROCESSOR_ONSET_RAMP_FACTOR = 0.39;
/**
 * `AudioContext` construction options, in ONE named place.
 *
 * Deliberately EMPTY, and that is a measured decision rather than an omission.
 * The spec default for `latencyHint` is already `'interactive'`, so passing the
 * category buys nothing (measured identical to unset). A NUMERIC hint does move
 * the device floor, but neither monotonically nor safely: Chrome honours the
 * hint UPWARD too, so 0.02 made `outputLatency` WORSE (48.0ms against 32.0ms
 * unset), and the only real win - 0.002, giving 2.9/24.0ms against 5.8/32.0ms -
 * shrinks the output buffer and so raises underrun probability under four decks
 * of worklets. A mid-set dropout costs more than the ~11ms it buys. Full probe
 * table: `.planning/latency-round2-design.md`, appendix.
 *
 * It is a named constant rather than a bare `new AudioContext()` because every
 * shipping DJ application (rekordbox, Serato, Traktor) exposes a user-facing
 * buffer/latency setting, and this is the single seam such a control would
 * write to. Building that control is NOT in scope here; not foreclosing it is.
 */
export const AUDIO_CONTEXT_OPTIONS: Readonly<AudioContextOptions> = Object.freeze({});
export const ANALYSER_FFT_SIZE = 4096;
export const CONTEXT_WAIT_POLL_MS = 25;
export const CONTEXT_WAIT_STALL_TIMEOUT_MS = 500;
export const HEADPHONE_OPERATION_TIMEOUT_MS = 5_000;
