/**
 * FILTER resonance (issue #9826, core slice): the PARAMETER knob (0..1) to the
 * Q of the channel FILTER's active biquad, with the two travel guards and the
 * level makeup from `(private path)` section 7, mimicked.
 *
 * - Resonance is its own control, never linked to COLOR (DJM-A9 manual p.41,
 *   PARAMETER = Resonance, independent of COLOR), except through the guards.
 * - Q is in dB, as Web Audio's lowpass/highpass Q is (and `dsp.rs` takes
 *   `q_db`): linear Q = 10^(qDb / 20). The knob runs linear in dB from the
 *   shipped flat Q (`FILTER_Q_DB`, 0.707 dB) to the ceiling.
 * - Onset guard (7.2 guard 1): Q fades in over the first 10% of COLOR travel,
 *   so leaving the detent never applies a full resonant peak (Mixxx #15116).
 * - Edge guard (7.2 guard 2): Q returns to flat over the last 15% of travel,
 *   so the LP floor and HP ceiling never boom or whistle.
 * - Makeup (7.3): a closed-form gain that holds pink-noise power (equal power
 *   per octave, the long-term spectrum of music) through the active side
 *   constant as Q rises. The resonant excess over a brickwall, on a log
 *   frequency axis, of a 2-pole LP or HP (they mirror under f -> 1/f) is
 *
 *     E(Q) = b / (4c) * (pi/2 + atan(b / (2c))),  b = 2 - 1/Q^2,  c = sqrt(1 - b^2/4)
 *
 *   in nepers of bandwidth; the side's passband inside 20 Hz..20 kHz is P
 *   nepers, so the makeup is sqrt((P + E(Qflat)) / (P + E(Q))). It does not
 *   cancel the corner peak (that would cancel the resonance); it keeps the
 *   integrated level where it was.
 *
 * At `FILTER_RES_DEFAULT` every output is exactly today's: Q === FILTER_Q_DB and
 * makeup === 1 for any COLOR position. The ceiling and default are taste,
 * calibrated in round filter-6 (MIXUX-05); the rest is hygiene.
 */
import { FILTER_DEADZONE_FRAC, FILTER_Q_DB, assertUnitRange } from '$lib/player/constants';
import { filterParamsFromKnob } from '$lib/rb/audio-engine-guards';

/** Shipped flat Q of both FILTER biquads, in dB (Web Audio units). */
export const FILTER_RES_Q_DB_FLAT = FILTER_Q_DB;
/** Resonance ceiling as linear Q; candidates 2, 4, 8 (Mixxx tops out at 4). Taste: filter-6. */
export const FILTER_RES_Q_MAX_LINEAR = 4;
/** Resonance ceiling in dB. */
export const FILTER_RES_Q_DB_MAX = 20 * Math.log10(FILTER_RES_Q_MAX_LINEAR);
/** Default knob position; at it the FILTER is bit-identical to before. Taste: filter-6. */
export const FILTER_RES_DEFAULT = 0;
/** Share of COLOR travel over which Q fades in from flat (taper doc 7.2, mimicked). */
export const FILTER_Q_ONSET_FRAC = 0.1;
/** Share of COLOR travel over which Q fades back to flat at the extreme (taper doc 7.2, mimicked). */
export const FILTER_Q_EDGE_FRAC = 0.15;
/** Audio band the makeup holds pink-noise power over. */
export const FILTER_RES_MAKEUP_BAND_LO_HZ = 20;
export const FILTER_RES_MAKEUP_BAND_HI_HZ = 20000;

/** Q (dB) and makeup gain for the active FILTER side at one knob pair. */
export interface FilterResonanceParams {
	qDb: number;
	makeupGain: number;
}

export type FilterSide = 'lp' | 'hp';

//----- mapping

/** Q in dB at a resonance depth 0..1, linear in dB from flat to the ceiling. */
function _qDbAtDepth(depth: number): number {
	return FILTER_RES_Q_DB_FLAT + depth * (FILTER_RES_Q_DB_MAX - FILTER_RES_Q_DB_FLAT);
}

/** Resonance knob 0..1 to its set Q in dB, before the travel guards; refuses non-finite or out of range. */
export function resonanceQDbFromKnob(value: number): number {
	assertUnitRange('filter_resonance', value);
	return _qDbAtDepth(value);
}

/** FILTER knob 0..1 to travel 0..1 away from the detent on the active side, 0 inside the dead zone. */
export function filterTravelFromKnob(filterKnob: number): number {
	assertUnitRange('filter', filterKnob);
	const colour = Math.abs((filterKnob - 0.5) * 2);
	if (colour < FILTER_DEADZONE_FRAC) return 0;
	return (colour - FILTER_DEADZONE_FRAC) / (1 - FILTER_DEADZONE_FRAC);
}

//----- guards

/** Share 0..1 of the set resonance allowed at a travel: ramps up over the onset, down over the edge. */
export function resonanceGuardWeight(travel: number): number {
	assertUnitRange('filter travel', travel);
	if (travel < FILTER_Q_ONSET_FRAC) return travel / FILTER_Q_ONSET_FRAC;
	if (travel > 1 - FILTER_Q_EDGE_FRAC) return (1 - travel) / FILTER_Q_EDGE_FRAC;
	return 1;
}

//----- makeup

/** Pink-noise power a 2-pole LP/HP of linear Q adds over a brickwall, in nepers of bandwidth. */
function _resonantExcessNepers(qLinear: number): number {
	const b = 2 - 1 / (qLinear * qLinear);
	const c = Math.sqrt(1 - (b * b) / 4);
	return (b / (4 * c)) * (Math.PI / 2 + Math.atan(b / (2 * c)));
}

/** Gain that keeps pink-noise power through one FILTER side at its flat-Q level when Q is raised to qDb. */
export function resonanceMakeupGain(qDb: number, cornerHz: number, side: FilterSide): number {
	if (!(qDb >= FILTER_RES_Q_DB_FLAT && qDb <= _qDbAtDepth(1))) {
		throw new RangeError(
			`filter resonance qDb must be within ${FILTER_RES_Q_DB_FLAT}..${FILTER_RES_Q_DB_MAX}, got ${qDb}`
		);
	}
	if (!(cornerHz > FILTER_RES_MAKEUP_BAND_LO_HZ && cornerHz < FILTER_RES_MAKEUP_BAND_HI_HZ)) {
		throw new RangeError(`filter corner must be inside the makeup band, got ${cornerHz} Hz`);
	}
	const passband =
		side === 'lp'
			? Math.log(cornerHz / FILTER_RES_MAKEUP_BAND_LO_HZ)
			: Math.log(FILTER_RES_MAKEUP_BAND_HI_HZ / cornerHz);
	const flat = passband + _resonantExcessNepers(10 ** (FILTER_RES_Q_DB_FLAT / 20));
	return Math.sqrt(flat / (passband + _resonantExcessNepers(10 ** (qDb / 20))));
}

//----- combined

/** FILTER knob plus resonance knob to the guarded Q (dB) and makeup gain of the active side. */
export function filterResonanceParams(
	filterKnob: number,
	resonance: number
): FilterResonanceParams {
	assertUnitRange('filter_resonance', resonance);
	const travel = filterTravelFromKnob(filterKnob);
	const qDb = _qDbAtDepth(resonanceGuardWeight(travel) * resonance);
	if (travel === 0) return { qDb, makeupGain: 1 };
	const { lpHz, hpHz } = filterParamsFromKnob(filterKnob);
	const side: FilterSide = filterKnob < 0.5 ? 'lp' : 'hp';
	return { qDb, makeupGain: resonanceMakeupGain(qDb, side === 'lp' ? lpHz : hpHz, side) };
}
