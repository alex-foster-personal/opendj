/**
 * Beat FX FILTER kernel (FXFILT-08): a lowpass whose cutoff sweeps cyclically in time with
 * the beat. Spec `(private path)` section 4 (last row)
 * and section 6 feature D. Mimicked: DJM-A9 manual p.54 (BEAT FX FILTER: the cutoff changes
 * cyclically according to the beat; BEAT 1/16 to 64 beats; TIME 10 to 32000 ms; LEVEL/DEPTH
 * is the effect level). TRIPLET FILTER (p.55) is out of scope.
 *
 * The LFO is a sine on the octave axis of the channel FILTER law's LP side: centred on the
 * log2 midpoint of FILTER_LP_FLOOR_HZ..FILTER_LP_CEILING_HZ, trough at the floor, crest at
 * the ceiling. Phase 0 (the trough) lands on the grid's downbeats for a cycle of one beat
 * or more, and on every beat under that (the mod-fx-lfo rule). With no usable tempo the
 * kernel runs on TIME and says so in `state()`.
 *
 * ```
 * input -+-> dry (1 - level) ------------------------------+-> output
 *        +-> lp (lowpass, f = CENTER_HZ) -> wet (level) ---+
 * lfo (oscillator, -cos at phase) -> lfoDepth (SPAN_OCT * 1200 cents) -> lp.detune
 * ```
 *
 * Native nodes only, nothing allocated per render quantum: a clock change (division, tempo,
 * TIME) makes one oscillator and one PeriodicWave; a level change makes nothing.
 */
import { FILTER_LP_CEILING_HZ, FILTER_LP_FLOOR_HZ, FILTER_Q } from '$lib/player/constants';

import type { AnlzBeat } from '../anlz-types';
import { lfoAnchor, lfoCycleSeconds, MOD_FX_TIME_MS, wrapUnitPhase } from '../mod-fx-lfo';

/** Beat divisions, DJM-A9 BEAT 1/16 to 64 beats, plus 3/4 from the beat FX unit's list. */
export const BEAT_FX_FILTER_BEATS = [
	1 / 16,
	1 / 8,
	1 / 4,
	1 / 2,
	3 / 4,
	1,
	2,
	4,
	8,
	16,
	32,
	64
] as const;
export type BeatFxFilterBeats = (typeof BEAT_FX_FILTER_BEATS)[number];

/** Free cycle bounds in ms (DJM TIME range). */
export const BEAT_FX_FILTER_TIME_MS = MOD_FX_TIME_MS;

/** LFO centre: the log2 midpoint of the LP sweep (about 1612 Hz). */
export const BEAT_FX_FILTER_CENTER_HZ = Math.sqrt(FILTER_LP_FLOOR_HZ * FILTER_LP_CEILING_HZ);

/** LFO half-span in octaves: half the LP sweep, so trough = floor and crest = ceiling. */
export const BEAT_FX_FILTER_SPAN_OCT = Math.log2(FILTER_LP_CEILING_HZ / FILTER_LP_FLOOR_HZ) / 2;

/** Engage/release and level-move ramp (raised cosine). */
export const BEAT_FX_FILTER_LEVEL_RAMP_SEC = 0.01;

/** Points in the preallocated raised-cosine level curve. */
export const BEAT_FX_FILTER_CURVE_POINTS = 64;

export type BeatFxFilterContext = Pick<
	BaseAudioContext,
	'createGain' | 'createBiquadFilter' | 'createOscillator' | 'createPeriodicWave'
>;

export interface BeatFxFilterClockInput {
	/** Division; validated always, even in TIME mode. */
	beats: number;
	/** TIME knob; validated always (it always has a value). */
	timeMs: number;
	grid: { beats: readonly AnlzBeat[]; positionSec: number; bpm: number | null } | null;
	contextTime: number;
}

export type BeatFxFilterClock =
	| {
			mode: 'beat';
			beats: BeatFxFilterBeats;
			timeMs: number;
			bpm: number;
			periodSec: number;
			contextTime: number;
			beatIndex: number;
			beatsPerSec: number;
			cycleOriginBeat: number;
	  }
	| {
			mode: 'time';
			beats: BeatFxFilterBeats;
			timeMs: number;
			periodSec: number;
			contextTime: number;
			reason: 'no-tempo';
	  };

export interface BeatFxFilterState {
	mode: 'beat' | 'time' | null;
	reason: 'no-tempo' | null;
	beats: BeatFxFilterBeats | null;
	bpm: number | null;
	timeMs: number | null;
	periodSec: number | null;
	level: number;
}

export interface BeatFxFilterKernel {
	input: GainNode;
	output: GainNode;
	dry: GainNode;
	wet: GainNode;
	lp: BiquadFilterNode;
	/** LFO (-1..1) to cents on lp.detune. */
	lfoDepth: GainNode;
	setClock(clock: BeatFxFilterClock, now: number): void;
	setLevel(level: number, now: number): void;
	state(): BeatFxFilterState;
	dispose(): void;
}

//-----------------------------------------------------------------------------
// pure

export function assertBeatFxFilterBeats(beats: number): asserts beats is BeatFxFilterBeats {
	if (!(BEAT_FX_FILTER_BEATS as readonly number[]).includes(beats)) {
		throw new Error(
			`beat-fx-filter: beats must be one of ${BEAT_FX_FILTER_BEATS.join(', ')}, got ${String(beats)}`
		);
	}
}

export function assertBeatFxFilterTimeMs(timeMs: number): void {
	if (!(
		typeof timeMs === 'number' &&
		timeMs >= BEAT_FX_FILTER_TIME_MS.min &&
		timeMs <= BEAT_FX_FILTER_TIME_MS.max
	)) {
		throw new Error(
			`beat-fx-filter: timeMs must be within ${BEAT_FX_FILTER_TIME_MS.min}..${BEAT_FX_FILTER_TIME_MS.max}, got ${String(timeMs)}`
		);
	}
}

/** Cutoff at LFO `phase`: 0 = trough (floor), 0.5 = crest (ceiling), a sine on the octave axis. */
export function beatFxFilterCutoffHz(phase: number): number {
	if (!Number.isFinite(phase))
		throw new Error(`beat-fx-filter: phase must be finite, got ${String(phase)}`);
	return BEAT_FX_FILTER_CENTER_HZ * 2 ** (-BEAT_FX_FILTER_SPAN_OCT * Math.cos(2 * Math.PI * phase));
}

/** Beat clock when the grid and tempo lock; TIME clock (reason no-tempo) otherwise. Never guesses a tempo. */
export function resolveBeatFxFilterClock(input: BeatFxFilterClockInput): BeatFxFilterClock {
	const { beats, timeMs, grid, contextTime } = input;
	assertBeatFxFilterBeats(beats);
	assertBeatFxFilterTimeMs(timeMs);
	if (grid !== null && grid.bpm !== null) {
		// cycleBeats 1 anchors on the grid; the kernel carries its own division (lfoAnchor refuses 3/4).
		const anchor = lfoAnchor(grid.beats, grid.positionSec, grid.bpm, contextTime, 1);
		const periodSec = lfoCycleSeconds(beats, grid.bpm);
		if (anchor !== null && periodSec !== null) {
			return {
				mode: 'beat',
				beats,
				timeMs,
				bpm: grid.bpm,
				periodSec,
				contextTime,
				beatIndex: anchor.beatIndex,
				beatsPerSec: anchor.beatsPerSec,
				cycleOriginBeat: anchor.cycleOriginBeat
			};
		}
	}
	return { mode: 'time', beats, timeMs, periodSec: timeMs / 1000, contextTime, reason: 'no-tempo' };
}

/** LFO phase in [0, 1) at `contextTime`; 0 is the trough. */
export function beatFxFilterPhase(clock: BeatFxFilterClock, contextTime: number): number {
	switch (clock.mode) {
		case 'beat': {
			const beatNow = clock.beatIndex + (contextTime - clock.contextTime) * clock.beatsPerSec;
			return wrapUnitPhase((beatNow - clock.cycleOriginBeat) / clock.beats);
		}
		case 'time':
			return wrapUnitPhase((contextTime - clock.contextTime) / clock.periodSec);
		default: {
			const _exhaustive: never = clock;
			throw new Error(
				`beat-fx-filter: unknown clock mode ${String((_exhaustive as BeatFxFilterClock).mode)}`
			);
		}
	}
}

function _raisedCosine(from: number, to: number, frac: number): number {
	return from + ((to - from) * (1 - Math.cos(Math.PI * frac))) / 2;
}

//-----------------------------------------------------------------------------
// graph

export function buildBeatFxFilterKernel(ctx: BeatFxFilterContext): BeatFxFilterKernel {
	const input = ctx.createGain();
	const output = ctx.createGain();
	const dry = ctx.createGain();
	const wet = ctx.createGain();
	const lp = ctx.createBiquadFilter();
	const lfoDepth = ctx.createGain();
	dry.gain.value = 1;
	wet.gain.value = 0;
	lp.type = 'lowpass';
	lp.frequency.value = BEAT_FX_FILTER_CENTER_HZ;
	lp.Q.value = FILTER_Q;
	lfoDepth.gain.value = BEAT_FX_FILTER_SPAN_OCT * 1200;
	input.connect(dry).connect(output);
	input.connect(lp).connect(wet).connect(output);
	lfoDepth.connect(lp.detune);

	const waveReal = new Float32Array(2);
	const waveImag = new Float32Array(2);
	const wetCurve = new Float32Array(BEAT_FX_FILTER_CURVE_POINTS);
	const dryCurve = new Float32Array(BEAT_FX_FILTER_CURVE_POINTS);
	let lfo: OscillatorNode | null = null;
	let clock: BeatFxFilterClock | null = null;
	let ramp = { start: 0, from: 0, to: 0 };

	function levelAt(t: number): number {
		const frac = (t - ramp.start) / BEAT_FX_FILTER_LEVEL_RAMP_SEC;
		if (frac >= 1) return ramp.to;
		if (frac <= 0) return ramp.from;
		return _raisedCosine(ramp.from, ramp.to, frac);
	}

	function setClock(next: BeatFxFilterClock, now: number): void {
		const phase = beatFxFilterPhase(next, now);
		waveReal[1] = -Math.cos(2 * Math.PI * phase);
		waveImag[1] = Math.sin(2 * Math.PI * phase);
		const wave = ctx.createPeriodicWave(waveReal, waveImag, { disableNormalization: true });
		const osc = ctx.createOscillator();
		osc.setPeriodicWave(wave);
		osc.frequency.value = 1 / next.periodSec;
		osc.connect(lfoDepth);
		osc.start(now);
		if (lfo !== null) {
			const old = lfo;
			old.onended = () => old.disconnect();
			old.stop(now);
		}
		lfo = osc;
		clock = next;
	}

	function setLevel(level: number, now: number): void {
		if (!(typeof level === 'number' && level >= 0 && level <= 1)) {
			throw new Error(`beat-fx-filter: level must be within 0..1, got ${String(level)}`);
		}
		if (level > 0 && clock === null) throw new Error('beat-fx-filter: no clock');
		if (level === ramp.to) return;
		const from = levelAt(now);
		const last = BEAT_FX_FILTER_CURVE_POINTS - 1;
		for (let i = 0; i < last; i++) {
			const v = _raisedCosine(from, level, i / last);
			wetCurve[i] = v;
			dryCurve[i] = 1 - v;
		}
		wetCurve[last] = level;
		dryCurve[last] = 1 - level;
		for (const [param, curve] of [
			[wet.gain, wetCurve],
			[dry.gain, dryCurve]
		] as const) {
			param.cancelAndHoldAtTime(now);
			param.setValueCurveAtTime(curve, now, BEAT_FX_FILTER_LEVEL_RAMP_SEC);
		}
		ramp = { start: now, from, to: level };
	}

	function state(): BeatFxFilterState {
		return {
			mode: clock?.mode ?? null,
			reason: clock?.mode === 'time' ? clock.reason : null,
			beats: clock?.beats ?? null,
			bpm: clock?.mode === 'beat' ? clock.bpm : null,
			timeMs: clock?.timeMs ?? null,
			periodSec: clock?.periodSec ?? null,
			level: ramp.to
		};
	}

	function dispose(): void {
		if (lfo !== null) {
			lfo.stop();
			lfo.disconnect();
			lfo = null;
		}
		for (const node of [input, output, dry, wet, lp, lfoDepth]) node.disconnect();
	}

	return { input, output, dry, wet, lp, lfoDepth, setClock, setLevel, state, dispose };
}
