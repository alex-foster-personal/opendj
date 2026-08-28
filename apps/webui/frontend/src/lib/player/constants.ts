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
/** Future schedule margin after the slower deck processor's reported latency. */
export const SYNC_SCHEDULE_SAFETY_S = 0.1;
export const ANALYSER_FFT_SIZE = 4096;
export const CONTEXT_WAIT_POLL_MS = 25;
export const CONTEXT_WAIT_STALL_TIMEOUT_MS = 500;
export const HEADPHONE_OPERATION_TIMEOUT_MS = 5_000;
