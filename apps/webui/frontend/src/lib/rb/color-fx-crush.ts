/**
 * CRUSH color FX (SNDCOL-10655): bit depth and sample rate reduction on the
 * per-channel color knob, as on the DJM-A9's CRUSH (manual p. 41, mimicked).
 *
 *  - Counterclockwise: the further the turn, the crunchier: bits 16 -> 4
 *    (linear) and decimation 1 -> 64 (exponential taper).
 *  - Clockwise: a fixed crush (`CRUSH_CW_FIXED_AMOUNT`) through a highpass
 *    rising 20 Hz -> 8 kHz (log).
 *  - PARAMETER scales the depth curve; 0.5 is exactly the curve above.
 *
 * The kernel runs in `color-fx-crush-processor.js` (an AudioWorklet: decimation
 * needs state across samples). This module holds its constants (the processor
 * takes every number from `crushProcessorOptions`), the knob -> targets laws,
 * a pure sample-for-sample mirror of the processor (`renderCrush`) for tests,
 * and the node build. The processor URL is imported by the live module only,
 * so this file bundles cleanly under esbuild.
 *
 * Graph, per channel, spliced once the worklet module has loaded:
 *
 *   high (pre-color) -> crush worklet -> highpass -> crushWet -> colorBus
 *
 * crossfaded linearly against `filterDry` (`applyCrushTargets`).
 *
 * Constants are mined from parked PR #492 (`color-fx/constants.ts`); taste
 * values (`CRUSH_CW_FIXED_AMOUNT`, the taper) await SNDCOL-05 calibration.
 */
import { colorFxTravel } from './color-fx';

//--------------------------------------------------------------------- constants

export const CRUSH_PROCESSOR_NAME = 'mdt-color-fx-crush';
/** Bit depth at the clean and the crushed end (MIMICKED, #492). */
export const CRUSH_MAX_BITS = 16;
export const CRUSH_MIN_BITS = 4;
/** Sample-and-hold length in samples at the clean and the crushed end (MIMICKED, #492). */
export const CRUSH_MIN_DECIMATION = 1;
export const CRUSH_MAX_DECIMATION = 64;
/** Crush depth held clockwise, under the rising highpass (CALIBRATION-PENDING, #492). */
export const CRUSH_CW_FIXED_AMOUNT = 0.6;
/** Clockwise highpass sweep (MIMICKED, #492). */
export const CRUSH_HPF_MIN_HZ = 20;
export const CRUSH_HPF_MAX_HZ = 8000;
export const CRUSH_HPF_Q = Math.SQRT1_2;
/** Per-sample control smoothing inside the processor (MIMICKED, #492 CONTROL_SMOOTH_MS). */
export const CRUSH_CONTROL_SMOOTH_SEC = 0.012;
/** Linear dry/wet crossfade (MIMICKED, #492 WET_CROSSFADE_MS). Linear, so the wet
 * path is exactly 0 after it, which the -90 dB ring-down check needs. */
export const CRUSH_WET_CROSSFADE_SEC = 0.008;
/** PARAMETER at which the depth curve is the calibrated one. */
export const CRUSH_PARAMETER_NOMINAL = 0.5;
/** Fixed gain after the highpass. The highpass rings on the held steps and
 * overshoots: the worst rendered peak untrimmed was 1.4425 from a -3 dBFS
 * input (COLOR 0.7, PARAMETER 1, tests/e2e/color-fx-crush-real-audio.spec.ts,
 * measured Mon 9 Nov 2026), so this keeps every grid position under 0 dBFS
 * with about 2% headroom. A level trim, never a clamp. */
export const CRUSH_OUTPUT_TRIM = 0.68;
/** Held values below this are flushed to 0 once per block (no subnormals). */
export const CRUSH_DENORMAL_FLUSH = 1e-20;
/** The worklet's render quantum; the mirror runs in the same blocks. */
export const CRUSH_BLOCK_FRAMES = 128;

export type CrushTaper = 'exponential' | 'steep' | 'late';
export interface CrushVoicing {
	taper: CrushTaper;
	cwFixedAmount: number;
}
export const DEFAULT_CRUSH_VOICING: Readonly<CrushVoicing> = Object.freeze({
	taper: 'exponential',
	cwFixedAmount: CRUSH_CW_FIXED_AMOUNT
});

//--------------------------------------------------------------------- laws

/** Crush depth 0..1 at signed travel -1..1 and PARAMETER 0..1. */
export function crushAmountFor(
	travel: number,
	parameter: number,
	cwFixedAmount = CRUSH_CW_FIXED_AMOUNT
): number {
	const base = travel < 0 ? -travel : travel > 0 ? cwFixedAmount : 0;
	const amount = base * (parameter / CRUSH_PARAMETER_NOMINAL);
	return amount > 1 ? 1 : amount;
}

function _warp(amount: number, taper: CrushTaper): number {
	if (taper === 'exponential') return amount;
	else if (taper === 'steep') return Math.pow(amount, 0.5);
	else if (taper === 'late') return amount * amount;
	const _exhaustive: never = taper;
	throw new Error(`Unhandled crush taper: ${String(_exhaustive)}`);
}

/** Sample-and-hold length (samples) at `amount`: exponential 1 -> 64. */
export function crushDecimationFor(amount: number, taper: CrushTaper = 'exponential'): number {
	return (
		CRUSH_MIN_DECIMATION *
		Math.pow(CRUSH_MAX_DECIMATION / CRUSH_MIN_DECIMATION, _warp(amount, taper))
	);
}

/** Bit depth at `amount`: linear 16 -> 4. */
export function crushBitsFor(amount: number): number {
	return CRUSH_MAX_BITS + amount * (CRUSH_MIN_BITS - CRUSH_MAX_BITS);
}

export function crushStepForBits(bits: number): number {
	return 2 / (Math.pow(2, bits) - 1);
}

export function crushQuantise(sample: number, step: number): number {
	return Math.round(sample / step) * step;
}

/** Highpass corner at signed travel: log 20 Hz -> 8 kHz clockwise, parked at 20 Hz otherwise. */
export function crushHighpassHzFor(travel: number): number {
	if (travel <= 0) return CRUSH_HPF_MIN_HZ;
	return CRUSH_HPF_MIN_HZ * Math.pow(CRUSH_HPF_MAX_HZ / CRUSH_HPF_MIN_HZ, travel);
}

export interface CrushProcessorOptions {
	maxBits: number;
	minBits: number;
	minDecimation: number;
	maxDecimation: number;
	cwFixedAmount: number;
	parameterNominal: number;
	controlSmoothSec: number;
	denormalFlush: number;
	taper: CrushTaper;
}

/** The plain object the processor validates; this module is the one source of its numbers. */
export function crushProcessorOptions(
	voicing: CrushVoicing = DEFAULT_CRUSH_VOICING
): CrushProcessorOptions {
	return {
		maxBits: CRUSH_MAX_BITS,
		minBits: CRUSH_MIN_BITS,
		minDecimation: CRUSH_MIN_DECIMATION,
		maxDecimation: CRUSH_MAX_DECIMATION,
		cwFixedAmount: voicing.cwFixedAmount,
		parameterNominal: CRUSH_PARAMETER_NOMINAL,
		controlSmoothSec: CRUSH_CONTROL_SMOOTH_SEC,
		denormalFlush: CRUSH_DENORMAL_FLUSH,
		taper: voicing.taper
	};
}

export interface CrushTargets {
	/** 1 outside the detent, 0 inside it (exact FILTER bypass). */
	wet: 0 | 1;
	travel: number;
	hpHz: number;
	parameter: number;
}

/** Targets at knob `value` (0..1, 0.5 = center) and PARAMETER. */
export function crushTargets(value: number, parameter: number): CrushTargets {
	const travel = colorFxTravel(value);
	return { wet: travel === 0 ? 0 : 1, travel, hpHz: crushHighpassHzFor(travel), parameter };
}

/** CRUSH closed: what a channel with a crush graph is driven to on any other effect. */
export const CRUSH_CLOSED: Readonly<CrushTargets> = Object.freeze({
	wet: 0,
	travel: 0,
	hpHz: CRUSH_HPF_MIN_HZ,
	parameter: CRUSH_PARAMETER_NOMINAL
});

//--------------------------------------------------------------------- pure mirror

/**
 * The processor's arithmetic, sample for sample, in the same operation order
 * and the same 128-frame blocks. `travel` is per sample (a-rate) or length 1.
 */
export function renderCrush(
	input: readonly [Float32Array, Float32Array | null],
	travel: Float32Array,
	parameter: number,
	sampleRate: number,
	voicing: CrushVoicing = DEFAULT_CRUSH_VOICING
): [Float32Array, Float32Array] {
	const o = crushProcessorOptions(voicing);
	const frames = input[0].length;
	const outL = new Float32Array(frames);
	const outR = new Float32Array(frames);
	const rate = 1 - Math.exp(-1 / (o.controlSmoothSec * sampleRate));
	const ratio = o.maxDecimation / o.minDecimation;
	const scale = parameter / o.parameterNominal;
	const held = new Float64Array(2);
	let phase = 0;
	let decimation = o.minDecimation;
	let bits = o.maxBits;
	for (let i = 0; i < frames; i++) {
		const t = travel.length === 1 ? travel[0] : travel[i];
		const base = t < 0 ? -t : t > 0 ? o.cwFixedAmount : 0;
		let amount = base * scale;
		if (amount > 1) amount = 1;
		const decimationTarget = o.minDecimation * Math.pow(ratio, _warp(amount, o.taper));
		const bitsTarget = o.maxBits + amount * (o.minBits - o.maxBits);
		decimation += (decimationTarget - decimation) * rate;
		bits += (bitsTarget - bits) * rate;
		const step = 2 / (Math.pow(2, bits) - 1);
		phase += 1;
		if (phase >= decimation) {
			phase -= decimation;
			held[0] = input[0][i];
			held[1] = input[1] === null ? 0 : input[1][i];
		}
		outL[i] = Math.round(held[0] / step) * step;
		outR[i] = Math.round(held[1] / step) * step;
		if ((i + 1) % CRUSH_BLOCK_FRAMES === 0 || i === frames - 1) {
			if (Math.abs(held[0]) < o.denormalFlush) held[0] = 0;
			if (Math.abs(held[1]) < o.denormalFlush) held[1] = 0;
		}
	}
	return [outL, outR];
}

//--------------------------------------------------------------------- worklet load

export interface CrushLoad {
	state: 'loading' | 'ready' | 'failed';
	/** Settles (never rejects) once `state` is final. */
	promise: Promise<void>;
	error: string | null;
}

const _loads = new WeakMap<BaseAudioContext, CrushLoad>();

/** Load the CRUSH processor module into `ctx`, once per context. A failure is
 * kept in the returned record (and logged once), never retried in a loop. */
export function loadCrushWorklet(ctx: BaseAudioContext, moduleUrl: string): CrushLoad {
	const existing = _loads.get(ctx);
	if (existing !== undefined) return existing;
	const load: CrushLoad = { state: 'loading', promise: Promise.resolve(), error: null };
	const start = (): Promise<void> => {
		if (ctx.audioWorklet === undefined)
			throw new Error('AudioWorklet is unavailable; CRUSH cannot load');
		return ctx.audioWorklet.addModule(moduleUrl);
	};
	load.promise = Promise.resolve()
		.then(start)
		.then(
			() => {
				load.state = 'ready';
			},
			(err: unknown) => {
				load.state = 'failed';
				load.error = err instanceof Error ? err.message : String(err);
				console.error(`[color-fx] CRUSH worklet failed to load: ${load.error}`);
			}
		);
	_loads.set(ctx, load);
	return load;
}

export function crushLoadState(ctx: BaseAudioContext): CrushLoad | null {
	return _loads.get(ctx) ?? null;
}

//--------------------------------------------------------------------- graph

export interface CrushGraph {
	node: AudioWorkletNode;
	highpass: BiquadFilterNode;
	trim: GainNode;
	wet: GainNode;
	nodes: AudioNode[];
	/** Set once the processor raised a terminal processorerror; the node then outputs silence for good. */
	failure: string | null;
}

/** Build the CRUSH nodes from `source` into `destination`, closed (wet 0). */
export function buildCrushGraph(
	ctx: BaseAudioContext,
	source: AudioNode,
	destination: AudioNode,
	voicing: CrushVoicing = DEFAULT_CRUSH_VOICING,
	onProcessorError?: (graph: CrushGraph) => void
): CrushGraph {
	const node = new AudioWorkletNode(ctx, CRUSH_PROCESSOR_NAME, {
		numberOfInputs: 1,
		numberOfOutputs: 1,
		outputChannelCount: [2],
		channelCount: 2,
		channelCountMode: 'explicit',
		channelInterpretation: 'discrete',
		processorOptions: crushProcessorOptions(voicing)
	});
	const highpass = ctx.createBiquadFilter();
	highpass.type = 'highpass';
	highpass.frequency.value = CRUSH_HPF_MIN_HZ;
	highpass.Q.value = CRUSH_HPF_Q;
	const trim = ctx.createGain();
	trim.gain.value = CRUSH_OUTPUT_TRIM;
	const wet = ctx.createGain();
	wet.gain.value = 0;
	source.connect(node);
	node.connect(highpass);
	highpass.connect(trim);
	trim.connect(wet);
	wet.connect(destination);
	const graph: CrushGraph = {
		node,
		highpass,
		trim,
		wet,
		nodes: [node, highpass, trim, wet],
		failure: null
	};
	// A terminal processor error is sticky: the channel then plays dry with CRUSH_UNAVAILABLE.
	node.onprocessorerror = (event: Event): void => {
		if (graph.failure !== null) return;
		graph.failure = _processorErrorText(event);
		onProcessorError?.(graph);
	};
	return graph;
}

function _processorErrorText(event: Event): string {
	const message = (event as Partial<ErrorEvent>).message;
	return typeof message === 'string' && message !== ''
		? message
		: 'processorerror (the processor threw in its constructor or process())';
}

function _linear(param: AudioParam, value: number, now: number): void {
	param.cancelScheduledValues(now);
	param.setValueAtTime(param.value, now);
	param.linearRampToValueAtTime(value, now + CRUSH_WET_CROSSFADE_SEC);
}

/**
 * Drive the CRUSH nodes. `dryGain` (the channel's `filterDry`) is crossfaded
 * against the wet path only when `crushOwnsDry` (CRUSH is the selected effect);
 * otherwise the effect's own law keeps the dry gain and only the wet closes.
 * `hpRampSec` is the color FX smoothing time constant for the highpass corner.
 */
export function applyCrushTargets(
	graph: CrushGraph,
	dryGain: AudioParam,
	targets: CrushTargets,
	crushOwnsDry: boolean,
	now: number,
	hpRampSec: number
): void {
	const travel = graph.node.parameters.get('travel');
	const parameter = graph.node.parameters.get('parameter');
	if (travel === undefined || parameter === undefined)
		throw new Error('crush worklet node is missing its parameters');
	travel.setValueAtTime(targets.travel, now);
	parameter.setValueAtTime(targets.parameter, now);
	graph.highpass.frequency.cancelScheduledValues(now);
	graph.highpass.frequency.setTargetAtTime(targets.hpHz, now, hpRampSec);
	_linear(graph.wet.gain, targets.wet, now);
	if (crushOwnsDry) _linear(dryGain, 1 - targets.wet, now);
}
