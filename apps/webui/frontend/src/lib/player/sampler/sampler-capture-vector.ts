/**
 * Edge sampler v1: pure mapping from a deck's mix state to a SamplerVector (spec section 3.2).
 * The deck knobs use the channel strip's own laws (player/constants.ts), so a captured
 * vector replays through the same curves.
 *
 * Requirements (mini-PRD):
 *   ✔︎ ✅ 🎯 the vector is the deck's sound, exactly
 *     [if] EQ low knob is 0.25 [then] eq_db.low is eqDbFromKnob(0.25) = -13 [else ⛔️]
 *     [if] filter knob is 0.25 [then] filter is -0.5 (low-pass side) [else ⛔️]
 *     [if] stems are not 'ready' [then] stems is null [else ⛔️]
 */

import { TRIM_MAX_GAIN, eqDbFromKnob, stemLinearFromKnob } from '$lib/player/constants';
import type { SamplerVector } from './sampler-types';

/** A silent gain (knob at 0) is stored as this floor; JSON cannot carry -Infinity. */
export const VECTOR_FLOOR_DB = -96;

/** One stem's knob state as the deck holds it. */
export type StemKnob = { muted: boolean; solo: boolean; gain: number };
/** The deck mix state a capture reads (trim, EQ, filter, stems, key shift, Master Tempo). */
export interface DeckMixInput {
	trimKnob: number;
	eqKnobs: { low: number; mid: number; high: number };
	filterKnob: number;
	/** null unless the deck's stems are ready */
	stems: {
		controls: Record<'vocal' | 'instrumental' | 'drums' | 'bass' | 'other', StemKnob>;
		hasInstrumental: boolean;
	} | null;
	keyShiftSt: number;
	masterTempo: boolean;
}

/** Linear gain to dB, a silent gain clamped to VECTOR_FLOOR_DB. */
export function linearToDb(linear: number): number {
	if (!(linear >= 0)) throw new Error(`gain must be >= 0, got ${linear}`);
	return linear === 0 ? VECTOR_FLOOR_DB : Math.max(VECTOR_FLOOR_DB, 20 * Math.log10(linear));
}

/** Trim knob (0..1) to dB through the channel strip's trim law. */
export function trimDbFromKnob(knob: number): number {
	return linearToDb(knob * TRIM_MAX_GAIN);
}

/** Filter knob (0..1, centre off) to the vector's bipolar -1..1 filter. */
export function filterBipolarFromKnob(knob: number): number {
	return (knob - 0.5) * 2;
}

/** Inverse of filterBipolarFromKnob, for replaying a vector through filterParamsFromKnob. */
export function filterKnobFromBipolar(filter: number): number {
	if (!(filter >= -1 && filter <= 1)) throw new Error(`filter must be -1..1, got ${filter}`);
	return filter / 2 + 0.5;
}

/** Round to 1e-6 dB and drop -0, so a vector compares and serialises stably. */
const _db = (x: number): number => Math.round(x * 1e6) / 1e6 + 0;
const _stem = (k: StemKnob) => ({
	gain_db: _db(linearToDb(stemLinearFromKnob(k.gain))),
	mute: k.muted,
	solo: k.solo
});

/** The SamplerVector a capture stores for a deck's mix state at that moment. */
export function vectorFromDeckMix(m: DeckMixInput): SamplerVector {
	return {
		trim_db: _db(trimDbFromKnob(m.trimKnob)),
		eq_db: {
			low: _db(eqDbFromKnob(m.eqKnobs.low)),
			mid: _db(eqDbFromKnob(m.eqKnobs.mid)),
			high: _db(eqDbFromKnob(m.eqKnobs.high))
		},
		filter: filterBipolarFromKnob(m.filterKnob),
		stems:
			m.stems === null
				? null
				: {
						vocals: _stem(m.stems.controls.vocal),
						drums: _stem(m.stems.controls.drums),
						bass: _stem(m.stems.controls.bass),
						inst: _stem(
							m.stems.hasInstrumental ? m.stems.controls.instrumental : m.stems.controls.other
						)
					},
		key_shift_st: m.keyShiftSt,
		master_tempo: m.masterTempo
	};
}

/** Whole beats in a loop: the deck's quantised length, else measured from the native BPM. */
export function loopBeatCount(
	inMs: number,
	outMs: number,
	beatLength: number | null,
	nativeBpm: number | null
): number {
	if (!(outMs > inMs)) throw new Error(`loop out ${outMs} must be after in ${inMs}`);
	if (beatLength !== null) return beatLength;
	if (nativeBpm === null || !(nativeBpm > 0))
		throw new Error('loop has no beat length and the deck has no BPM');
	return Math.max(1, Math.round(((outMs - inMs) / 60000) * nativeBpm));
}
