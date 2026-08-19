/**
 * Player DSP constants.
 *
 * Extracted verbatim from audio-engine.svelte.ts (T4 S1). Values are the
 * contract: an EQ frequency, a smoothing time-constant or a schedule safety
 * margin changed here changes what the DJ hears, and Web Audio will not tell
 * you. Nothing in this module imports from the player, so it is a leaf.
 */

import type { DeckId } from '$lib/rb/types';

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
export const ANALYSER_FFT_SIZE = 4096;
export const CONTEXT_WAIT_POLL_MS = 25;
export const CONTEXT_WAIT_STALL_TIMEOUT_MS = 500;
export const HEADPHONE_OPERATION_TIMEOUT_MS = 5_000;
