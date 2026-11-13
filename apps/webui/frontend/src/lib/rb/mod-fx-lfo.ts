/**
 * Beat-locked LFO phase math for the flanger and phaser (FXMOD-01).
 *
 * Pure: no AudioContext, no deck state. A deck's beat grid, its playhead and
 * its effective tempo become an ANCHOR (one beat position at one context
 * time); an anchor plus any later context time becomes an LFO PHASE in
 * [0, 1). Phase 0 is the sweep's trough. For a cycle of one beat or more the
 * trough lands on the grid's downbeats (counting cycles from the track's
 * first downbeat); for a cycle under a beat it lands on every beat. A tempo
 * change re-anchors at the change moment, so the phase never jumps.
 *
 * Spec: `(private path)` sections 2 and 4.
 * Mimicked references: Pioneer DJM-900NXS2 BEAT 1/16 to 64 beats and TIME 10
 * to 32000 ms (Operating Instructions p.16); Traktor LFO RESET to the lowest
 * point (Traktor Pro manual, Effect Reference).
 */
import type { AnlzBeat } from './anlz-types';
import { gridBeatPosition } from './beat-sync-math';
import { COLOR_FX_MAX_BPM, COLOR_FX_MIN_BPM } from './color-fx';

/** LFO cycle lengths in beats, 1/16 to 64 (DJM BEAT range). */
export const MOD_FX_CYCLE_BEATS = [1 / 16, 1 / 8, 1 / 4, 1 / 2, 1, 2, 4, 8, 16, 32, 64] as const;
/**
 * Triplet cycle lengths in beats, 1/12 to 12/3 (DJM-A9 TRIPLET FILTER p.55,
 * mimicked endpoints; interior steps are the duple steps times 2/3, hygiene).
 * Three cycles span a duple step, so counted from the first downbeat the
 * trough lands back on a grid beat every third cycle.
 */
export const TRIPLET_CYCLE_BEATS = [1 / 12, 1 / 6, 1 / 3, 2 / 3, 4 / 3, 8 / 3, 4] as const;
export type TripletCycleBeats = (typeof TRIPLET_CYCLE_BEATS)[number];
export type ModFxCycleBeats = (typeof MOD_FX_CYCLE_BEATS)[number] | TripletCycleBeats;

/** Free (grid-less) LFO cycle bounds in milliseconds (DJM TIME range). */
export const MOD_FX_TIME_MS = Object.freeze({ min: 10, max: 32000 });

/** Tempo range the beat-locked LFO accepts; the same range as color ECHO. */
export const MOD_FX_MIN_BPM = COLOR_FX_MIN_BPM;
export const MOD_FX_MAX_BPM = COLOR_FX_MAX_BPM;

/** A beat-locked anchor: the deck sat at fractional grid beat `beatIndex` at `contextTime`. */
export interface LfoBeatAnchor {
	kind: 'beat';
	contextTime: number;
	beatIndex: number;
	beatsPerSec: number;
	/** Grid index of the track's first downbeat; cycles count from here. */
	cycleOriginBeat: number;
	cycleBeats: ModFxCycleBeats;
}

/** A free anchor: phase 0 at `contextTime`, one cycle every `periodSec`. */
export interface LfoTimeAnchor {
	kind: 'time';
	contextTime: number;
	periodSec: number;
}

export type LfoAnchor = LfoBeatAnchor | LfoTimeAnchor;

/** A fractional phase this close below 1 is the next cycle's 0 (float noise, not a sweep). */
const WRAP_EPSILON = 1e-12;

//-----------------------------------------------------------------------------
// validation

function _assertFiniteNonNegative(field: string, value: number): void {
	if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) {
		throw new Error(`mod-fx-lfo: ${field} must be a finite number >= 0, got ${String(value)}`);
	}
}

function _assertCycleBeats(cycleBeats: number): asserts cycleBeats is ModFxCycleBeats {
	const duple = (MOD_FX_CYCLE_BEATS as readonly number[]).includes(cycleBeats);
	if (!duple && !(TRIPLET_CYCLE_BEATS as readonly number[]).includes(cycleBeats)) {
		throw new Error(
			`mod-fx-lfo: cycleBeats must be one of ${MOD_FX_CYCLE_BEATS.join(', ')} or triplet ${TRIPLET_CYCLE_BEATS.join(', ')}, got ${String(cycleBeats)}`
		);
	}
}

/** Fractional part in [0, 1); a value within float noise of 1 wraps to 0. */
export function wrapUnitPhase(x: number): number {
	const frac = x - Math.floor(x);
	return frac > 1 - WRAP_EPSILON ? 0 : frac;
}

//-----------------------------------------------------------------------------
// public

/**
 * Seconds per cycle of `cycleBeats` at `bpm`: the ONE tempo rule for every
 * beat-timed modulation and gate (the TRANS gate reads it too). Null outside
 * 40..300 BPM; the caller reports that, it never guesses a cycle.
 */
export function lfoCycleSeconds(cycleBeats: number, bpm: number): number | null {
	if (typeof bpm !== 'number' || Number.isNaN(bpm))
		throw new Error(`mod-fx-lfo: bpm must be a number, got ${String(bpm)}`);
	if (typeof cycleBeats !== 'number' || !Number.isFinite(cycleBeats) || cycleBeats <= 0)
		throw new Error(
			`mod-fx-lfo: cycleBeats must be a finite number > 0, got ${String(cycleBeats)}`
		);
	if (!(bpm >= MOD_FX_MIN_BPM && bpm <= MOD_FX_MAX_BPM)) return null;
	return (cycleBeats * 60) / bpm;
}

/**
 * Anchor a beat-locked LFO: the deck is at track `positionSec` at
 * `contextTime`, playing at effective tempo `bpm`. Returns null when the LFO
 * cannot lock (no grid, the playhead off the grid, no downbeat, or a tempo
 * outside 40..300 BPM); the caller reports that, it never guesses a phase.
 */
export function lfoAnchor(
	beats: readonly AnlzBeat[],
	positionSec: number,
	bpm: number,
	contextTime: number,
	cycleBeats: number
): LfoBeatAnchor | null {
	_assertFiniteNonNegative('positionSec', positionSec);
	_assertFiniteNonNegative('contextTime', contextTime);
	_assertCycleBeats(cycleBeats);
	if (typeof bpm !== 'number' || Number.isNaN(bpm))
		throw new Error(`mod-fx-lfo: bpm must be a number, got ${String(bpm)}`);
	if (!Array.isArray(beats)) throw new Error('mod-fx-lfo: beats must be an array');
	if (lfoCycleSeconds(cycleBeats, bpm) === null) return null;
	const cycleOriginBeat = beats.findIndex((beat) => beat.n === 1);
	if (cycleOriginBeat < 0) return null;
	const beatIndex = gridBeatPosition(beats, positionSec);
	if (beatIndex === null) return null;
	return {
		kind: 'beat',
		contextTime,
		beatIndex,
		beatsPerSec: bpm / 60,
		cycleOriginBeat,
		cycleBeats
	};
}

/** Anchor a free LFO of period `timeMs` (10..32000 ms) with its trough at `contextTime`. */
export function lfoTimeAnchor(timeMs: number, contextTime: number): LfoTimeAnchor {
	_assertFiniteNonNegative('contextTime', contextTime);
	if (!(
		typeof timeMs === 'number' &&
		timeMs >= MOD_FX_TIME_MS.min &&
		timeMs <= MOD_FX_TIME_MS.max
	)) {
		throw new Error(
			`mod-fx-lfo: timeMs must be within ${MOD_FX_TIME_MS.min}..${MOD_FX_TIME_MS.max}, got ${String(timeMs)}`
		);
	}
	return { kind: 'time', contextTime, periodSec: timeMs / 1000 };
}

/** LFO phase in [0, 1) at `contextTime`; 0 is the trough. */
export function lfoPhase(anchor: LfoAnchor, contextTime: number): number {
	_assertFiniteNonNegative('contextTime', contextTime);
	switch (anchor.kind) {
		case 'beat': {
			const beatNow = anchor.beatIndex + (contextTime - anchor.contextTime) * anchor.beatsPerSec;
			return wrapUnitPhase((beatNow - anchor.cycleOriginBeat) / anchor.cycleBeats);
		}
		case 'time':
			return wrapUnitPhase((contextTime - anchor.contextTime) / anchor.periodSec);
		default: {
			const _exhaustive: never = anchor;
			throw new Error(`mod-fx-lfo: unknown anchor kind ${String((_exhaustive as LfoAnchor).kind)}`);
		}
	}
}
