/**
 * TRANS gate envelope math on the mod FX LFO phase (FXGATE).
 *
 * Pure: no AudioContext, no deck state. The gate is OPEN from the downbeat
 * (LFO phase 0, from `lfoPhase` in `./mod-fx-lfo`) for `duty` of the cycle
 * and CLOSED for the rest, with a raised-cosine edge at both transitions.
 * The edges live inside the closed part, so the open hold is bit-exact 1 and
 * the downbeat itself is fully open. One cycle is a beat division of the
 * deck's effective tempo through the mod FX LFO's tempo rule
 * (`lfoCycleSeconds`, the only tempo rule) or a free TIME in ms. The kernel,
 * the unit readout and the rating export all call `transGain`, so they
 * compute one envelope.
 *
 * Mimicked (hygiene, no calibration): DJM-A9 Instruction Manual p.55 TRANS,
 * "Cuts the sound cyclically according to the beat", BEAT 1/16 to 16, TIME
 * 10 to 16000 ms, LEVEL/DEPTH "Balance between the original sound and the
 * effect sound, and duty"; the same row in the DJM-900NXS2 Operating
 * Instructions BEAT FX table p.16. 3/4 beat kept from main's color ECHO.
 * Edge shapes follow Traktor Gater SHAPE (hold to decay) as NAMED variants;
 * the pick is taste and lands in #10008. Spec
 * `(private path)` section 2 and
 * `01-beat-fx.md` section 4.3.
 */
import { lfoCycleSeconds, TRIPLET_CYCLE_BEATS, type TripletCycleBeats } from './mod-fx-lfo';

/** Cut cycle lengths in beats (A9 range, plus 3/4 from main's color ECHO). */
export const TRANS_CUT_BEATS = [1 / 16, 1 / 8, 1 / 4, 1 / 2, 3 / 4, 1, 2, 4, 8, 16] as const;
/** Triplet cut cycles, the mod FX LFO's triplet set (1/12 to 12/3 beats, issue #10016). */
export const TRANS_TRIPLET_CUT_BEATS = TRIPLET_CYCLE_BEATS;
export type TransCutBeats = (typeof TRANS_CUT_BEATS)[number] | TripletCycleBeats;

/** Free (grid-less) cut cycle bounds in milliseconds (A9 TIME range). */
export const TRANS_TIME_MS = Object.freeze({ min: 10, max: 16000 });

/** Edge (raised-cosine ramp) length per named shape, in milliseconds. */
export const TRANS_EDGE_SHAPES = Object.freeze({ hard: 1, soft: 4, smooth: 10 });
export type TransEdgeShape = keyof typeof TRANS_EDGE_SHAPES;
export const TRANS_EDGE_DEFAULT: TransEdgeShape = 'soft';

/** Two edges never take more than half the cycle: one edge is at most a quarter. */
export const TRANS_EDGE_MAX_FRAC = 0.25;

/** LEVEL/DEPTH knob laws; the default is the A9's balance reading. */
export const TRANS_DEPTH_LAWS = Object.freeze({
	balance: 'knob is the wet balance 0..1; duty unchanged',
	balance_then_duty:
		'knob 0..0.5 raises the wet balance 0..1 at the given duty; 0.5..1 pulls duty from the given value to half of it'
});
export type TransDepthLaw = keyof typeof TRANS_DEPTH_LAWS;
export const TRANS_DEPTH_LAW_DEFAULT: TransDepthLaw = 'balance';

export interface TransMix {
	/** Wet balance 0..1; 0 is bit-exact dry. */
	wet: number;
	/** Open fraction of the cycle after the law is applied. */
	duty: number;
}

//-----------------------------------------------------------------------------
// validation

function _assertInRange(
	field: string,
	value: number,
	min: number,
	max: number,
	maxOpen = false
): void {
	const inside =
		typeof value === 'number' && value >= min && (maxOpen ? value < max : value <= max);
	if (!inside) {
		const upper = maxOpen ? `< ${max}` : `<= ${max}`;
		throw new Error(
			`trans-gate: ${field} must be a number >= ${min} and ${upper}, got ${String(value)}`
		);
	}
}

/** Throws unless `beats` is one of TRANS_CUT_BEATS or TRANS_TRIPLET_CUT_BEATS. */
export function assertTransCutBeats(beats: number): asserts beats is TransCutBeats {
	const duple = (TRANS_CUT_BEATS as readonly number[]).includes(beats);
	if (!duple && !(TRANS_TRIPLET_CUT_BEATS as readonly number[]).includes(beats)) {
		throw new Error(
			`trans-gate: beats must be one of ${TRANS_CUT_BEATS.join(', ')} or triplet ${TRANS_TRIPLET_CUT_BEATS.join(', ')}, got ${String(beats)}`
		);
	}
}

/** Throws unless `timeMs` is within TRANS_TIME_MS. */
export function assertTransTimeMs(timeMs: number): void {
	_assertInRange('timeMs', timeMs, TRANS_TIME_MS.min, TRANS_TIME_MS.max);
}

function _assertEdgeShape(shape: string): asserts shape is TransEdgeShape {
	if (!Object.hasOwn(TRANS_EDGE_SHAPES, shape)) {
		throw new Error(
			`trans-gate: shape must be one of ${Object.keys(TRANS_EDGE_SHAPES).join(', ')}, got ${String(shape)}`
		);
	}
}

function _assertDepthLaw(law: string): asserts law is TransDepthLaw {
	if (!Object.hasOwn(TRANS_DEPTH_LAWS, law)) {
		throw new Error(
			`trans-gate: law must be one of ${Object.keys(TRANS_DEPTH_LAWS).join(', ')}, got ${String(law)}`
		);
	}
}

//-----------------------------------------------------------------------------
// cycle length

/**
 * Seconds per cut cycle of `beats` at `bpm`, through the mod FX LFO's one
 * tempo rule. Null outside 40..300 BPM; the caller reports that, it never
 * guesses a cycle.
 */
export function transCycleSeconds(beats: number, bpm: number): number | null {
	assertTransCutBeats(beats);
	return lfoCycleSeconds(beats, bpm);
}

/** Seconds per cut cycle for a free TIME of `timeMs` (10..16000 ms). */
export function transTimeCycleSeconds(timeMs: number): number {
	assertTransTimeMs(timeMs);
	return timeMs / 1000;
}

//-----------------------------------------------------------------------------
// envelope

/**
 * Edge length as a fraction of a `cycleSec` cycle for a named shape, clamped
 * so two edges never exceed half the cycle (short cycles get shorter edges
 * rather than a gate that never fully closes).
 */
export function edgeFracFor(shape: TransEdgeShape, cycleSec: number): number {
	_assertEdgeShape(shape);
	if (typeof cycleSec !== 'number' || !Number.isFinite(cycleSec) || cycleSec <= 0) {
		throw new Error(`trans-gate: cycleSec must be a finite number > 0, got ${String(cycleSec)}`);
	}
	return Math.min(TRANS_EDGE_SHAPES[shape] / 1000 / cycleSec, TRANS_EDGE_MAX_FRAC);
}

/**
 * Gate gain at LFO `phase` in [0, 1): 1 across [0, duty) (the open hold
 * starts on the downbeat), a raised-cosine fall over the next `edgeFrac`,
 * 0 across the closed part, and a raised-cosine rise over the last
 * `edgeFrac` before the next downbeat. When the closed part is shorter than
 * two edges, each edge shrinks to half of it so the hold is never eaten.
 * With edgeFrac 0 the gain is a pure step and its integral over a cycle is
 * `duty`; each edge adds half its width.
 */
export function transGain(phase: number, duty: number, edgeFrac: number): number {
	_assertInRange('phase', phase, 0, 1, true);
	_assertInRange('duty', duty, 0, 1);
	_assertInRange('edgeFrac', edgeFrac, 0, TRANS_EDGE_MAX_FRAC);
	if (phase < duty) return 1;
	const edge = Math.min(edgeFrac, (1 - duty) / 2);
	const fallEnd = duty + edge;
	if (phase < fallEnd) return 0.5 * (1 + Math.cos((Math.PI * (phase - duty)) / edge));
	const riseStart = 1 - edge;
	if (phase < riseStart) return 0;
	return 0.5 * (1 - Math.cos((Math.PI * (phase - riseStart)) / edge));
}

//-----------------------------------------------------------------------------
// depth law

/**
 * LEVEL/DEPTH `knob` (0..1) to a wet balance and an effective duty under a
 * named law. Knob 0 is wet 0 (bit-exact dry) under every law.
 */
export function transMix(knob: number, duty: number, law: TransDepthLaw): TransMix {
	_assertInRange('knob', knob, 0, 1);
	_assertInRange('duty', duty, 0, 1);
	_assertDepthLaw(law);
	switch (law) {
		case 'balance':
			return { wet: knob, duty };
		case 'balance_then_duty': {
			if (knob <= 0.5) return { wet: knob * 2, duty };
			return { wet: 1, duty: duty * (1 - (knob - 0.5)) };
		}
		default: {
			const _exhaustive: never = law;
			throw new Error(`trans-gate: unhandled law ${String(_exhaustive)}`);
		}
	}
}
