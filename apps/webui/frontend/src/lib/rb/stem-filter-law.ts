/**
 * Per-stem FILTER law (first slice of #10326).
 *
 * Maps one stem part's filter knob (0..1, 0.5 neutral) to a single biquad
 * setting. The frequencies come from the channel FILTER law
 * (`filterParamsFromKnob`), mimicked so a stem filter sweeps exactly like the
 * channel filter; inside the channel dead zone the part is bypassed (allpass)
 * so a neutral deck costs nothing extra.
 */
import { filterParamsFromKnob } from './audio-engine-guards';

export const STEM_FILTER_NEUTRAL = 0.5;

export type StemFilterType = 'allpass' | 'lowpass' | 'highpass';

export interface StemFilterBiquad {
	type: StemFilterType;
	frequency: number;
	bypassed: boolean;
}

/** Clamp a knob value to 0..1, treating non-finite input as neutral. */
export function normaliseStemFilterValue(value: number): number {
	if (!Number.isFinite(value)) return STEM_FILTER_NEUTRAL;
	return Math.min(1, Math.max(0, value));
}

/** Give the biquad type and frequency one stem part's filter knob asks for. */
export function stemFilterBiquad(value: number): StemFilterBiquad {
	const params = filterParamsFromKnob(normaliseStemFilterValue(value));
	if (params.lpWetGain > 0) {
		return { type: 'lowpass', frequency: params.lpHz, bypassed: false };
	}
	if (params.hpWetGain > 0) {
		return { type: 'highpass', frequency: params.hpHz, bypassed: false };
	}
	return { type: 'allpass', frequency: params.lpHz, bypassed: true };
}
