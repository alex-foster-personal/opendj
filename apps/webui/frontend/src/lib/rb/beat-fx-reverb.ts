/**
 * Beat FX REVERB parameter surface, pure (issue #9821, spec `fx-fanout-20261108/05-reverb.md`
 * sections 3 and 4). Mimicked from the Pioneer DJM-V10-LF manual: amount steps 1, 10, 25,
 * 50 (default), 75, 90, 100 (p. 75), TIME 1..100% and the X-PAD filter cut-off (p. 61),
 * LEVEL/DEPTH fully CCW is the original sound (p. 57). The amount-to-RT60 law is a placeholder
 * until the calibration session (#9824): 0.4 s at 1%, 2.6 s at 50%, 8 s at 100%, log-spaced.
 */
import { SPACE_WET_GAIN } from './color-fx-graph';

export const REVERB_AMOUNT_STEPS = [1, 10, 25, 50, 75, 90, 100] as const;
export type ReverbAmount = (typeof REVERB_AMOUNT_STEPS)[number];
const RANGES = { time: [1, 100], filter_hz: [500, 20000], level: [0, 1] } as const;
const RT60_ANCHORS = [
	[1, 0.4],
	[50, 2.6],
	[100, 8]
] as const;
/** Room size across TIME 1..100 (spec section 4: 0.5x to 1.5x). */
export const REVERB_SIZE_RANGE = [0.5, 1.5] as const;
/** House pre-delay at size 1.0 (spec section 4, not tempo-synced); it scales with the room. */
export const REVERB_PREDELAY_SEC = 0.02;
/** Wet at LEVEL/DEPTH 1: today's SPACE return, a placeholder until calibration. */
export const REVERB_WET_CEILING = SPACE_WET_GAIN;
/** Wet, filter and voice ramps, and the Release cut (issue #9821: 10 ms). */
export const REVERB_RAMP_SEC = 0.01;

/** The unit's REVERB controls as the command and the mirror carry them. */
export interface BeatFxReverbSettings {
	amount: ReverbAmount;
	time: number;
	filter_hz: number;
}

export const DEFAULT_REVERB_SETTINGS: Readonly<BeatFxReverbSettings> = Object.freeze({
	amount: 50,
	time: 50,
	filter_hz: 20000
});

/** Validate time, filter_hz or level against its range; the refusal names the field. */
export function assertReverbRange(field: keyof typeof RANGES, value: unknown): number {
	const [lo, hi] = RANGES[field];
	if (typeof value !== 'number' || !Number.isFinite(value) || value < lo || value > hi) {
		throw new RangeError(
			`beat_fx reverb ${field} must be a number in ${lo}..${hi}; got ${String(value)}`
		);
	}
	return value;
}

/** Validate an amount step; any other value is refused naming the field and the steps. */
export function assertReverbAmount(value: unknown): ReverbAmount {
	if (!(REVERB_AMOUNT_STEPS as readonly unknown[]).includes(value)) {
		throw new RangeError(
			`beat_fx reverb amount must be one of ${REVERB_AMOUNT_STEPS.join('|')}; got ${String(value)}`
		);
	}
	return value as ReverbAmount;
}

/** RT60 in seconds for an amount step: log-spaced between the 1%, 50% and 100% anchors. */
export function reverbAmountToRt60(amount: unknown): number {
	const step = assertReverbAmount(amount);
	const [[a0, r0], [a1, r1]] = step <= 50 ? RT60_ANCHORS.slice(0, 2) : RT60_ANCHORS.slice(1);
	return r0 * (r1 / r0) ** ((step - a0) / (a1 - a0));
}

/** Room size scale for TIME: linear, 1 -> 0.5x and 100 -> 1.5x. */
export function reverbTimeToSize(time: unknown): number {
	const t = assertReverbRange('time', time);
	const [lo, hi] = REVERB_SIZE_RANGE;
	return lo + ((hi - lo) * (t - 1)) / 99;
}

/** Wet return gain for LEVEL/DEPTH 0..1; level 0 gives exactly 0. */
export function reverbWetForLevel(level: unknown): number {
	return assertReverbRange('level', level) * REVERB_WET_CEILING;
}
