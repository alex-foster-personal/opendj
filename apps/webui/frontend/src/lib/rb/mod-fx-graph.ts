/**
 * Mod FX graph (FXMOD): constants, processor options and the node build for
 * `mod-fx-processor.js` (FLANGER, PHASER and TRANS, one options builder each
 * in `KERNEL_OPTIONS`).
 *
 * The node is a plain insert: `source -> node -> destination`, dry inside, so a
 * host (a test-only host until the beat FX unit registers it, #9837) splices it
 * anywhere. The returned graph satisfies `ModFxNodePort` from `mod-fx.svelte.ts`,
 * which feeds it grid anchors.
 *
 * Spec: `(private path)` s.4.
 * Mimicked: Pioneer FLANGER 1-cycle per beat fraction and LEVEL/DEPTH fully
 * counterclockwise = original only (DJM-900NXS2 p.16); 10 ms ramps (house
 * COLOR_FX_RAMP_SEC); sweep 0.3..6 ms (PASP Flanging, textbook). Feedback and
 * spread are taste, calibrated later (#9839): named voicing constants.
 * Mimicked for PHASER: same Pioneer cycle and LEVEL/DEPTH law (DJM-900NXS2
 * p.16); 200 Hz..4 kHz exponential sweep (PASP Phasing with first-order
 * allpass filters, textbook). Stages, feedback and spread are taste (#9839).
 * TRANS: the gate law, edge shapes, depth laws and time range are trans-gate.ts
 * (#10000, mimicked DJM-A9 p.55 TRANS); the processor mirrors `transGain` and
 * `transMix` sample for sample. Duty is taste (#10008): a named constant.
 */
import type { LfoAnchor, ModFxCycleBeats } from './mod-fx-lfo';
import {
	TRANS_DEPTH_LAWS,
	TRANS_DEPTH_LAW_DEFAULT,
	TRANS_EDGE_DEFAULT,
	TRANS_EDGE_SHAPES,
	assertTransTimeMs,
	transCycleSeconds,
	type TransDepthLaw,
	type TransEdgeShape
} from './trans-gate';
import type { ModFxNodePort } from './mod-fx.svelte';
import modFxProcessorUrl from './mod-fx-processor.js?url';

//----------------------------------------------------------------------------- constants

export const MOD_FX_PROCESSOR_NAME = 'mdt-mod-fx';
/** Level ramp on engage, release and depth moves (MIMICKED, COLOR_FX_RAMP_SEC). */
export const MOD_FX_RAMP_SEC = 0.01;
/** LFO glide from the old phase to the new on a seek re-anchor (MIMICKED, spec s.4). */
export const MOD_FX_CROSSFADE_SEC = 0.01;
/** Loop state below this is flushed to 0 (no subnormals). */
export const MOD_FX_DENORMAL_FLUSH = 1e-30;
/** Swept delay at the LFO trough and peak (MIMICKED, PASP Flanging). */
export const FLANGER_MIN_DELAY_SEC = 0.0003;
export const FLANGER_MAX_DELAY_SEC = 0.006;
/** Frames past the max delay that the linear fractional read may touch. */
export const FLANGER_HEADROOM_FRAMES = 2;
/** Feedback voicing (CALIBRATION-PENDING #9839, candidates 0.3, 0.55, 0.75). */
export const FLANGER_FEEDBACK = 0.55;
/** Feedback magnitude cap applied in code, whatever voicing asks. */
export const FLANGER_FEEDBACK_MAX = 0.9;
/** Right-channel LFO offset in degrees (CALIBRATION-PENDING #9839, candidates 0, 45, 90). */
export const FLANGER_SPREAD_DEG = 0;

/** Break frequency at the LFO trough and peak, swept exponentially (MIMICKED, PASP Phasing). */
export const PHASER_MIN_HZ = 200;
export const PHASER_MAX_HZ = 4000;
/** Allpass sections (CALIBRATION-PENDING #9839, Traktor 8-pole switch: 4 or 8). */
export const PHASER_STAGES = 4;
export const PHASER_STAGES_ALLOWED = [4, 8] as const;
/** Feedback voicing (CALIBRATION-PENDING #9839, candidates 0, 0.4, 0.65). */
export const PHASER_FEEDBACK = 0.4;
/** Feedback magnitude cap applied in code, whatever voicing asks. */
export const PHASER_FEEDBACK_MAX = 0.75;
/** Right-channel LFO offset in degrees (CALIBRATION-PENDING #9839, as FLANGER). */
export const PHASER_SPREAD_DEG = 0;

/** Open fraction of each cut cycle (CALIBRATION-PENDING #10008, the maintainer to pick). */
export const TRANS_DUTY = 0.5;
/** Why a beat-timed TRANS is inert; same pattern as ECHO_NO_TEMPO_REASON. */
export const TRANS_NO_TEMPO_REASON =
	'trans is timed in beats and this deck has no tempo: load a track with a BPM or beatgrid, or set time_ms';

export interface FlangerVoicing {
	feedback: number;
	spreadDeg: number;
}

export interface PhaserVoicing extends FlangerVoicing {
	stages: (typeof PHASER_STAGES_ALLOWED)[number];
}

export interface TransVoicing {
	duty: number;
	edge: TransEdgeShape;
	depthLaw: TransDepthLaw;
}

//----------------------------------------------------------------------------- options

/** Flanger processorOptions; voicing overridable (pair export), feedback clamped to +-0.9. */
export function flangerProcessorOptions(voicing: Partial<FlangerVoicing> = {}) {
	const feedback = voicing.feedback ?? FLANGER_FEEDBACK;
	const spreadDeg = voicing.spreadDeg ?? FLANGER_SPREAD_DEG;
	if (!Number.isFinite(feedback))
		throw new RangeError(`mod-fx-graph: feedback must be finite, got ${feedback}`);
	return {
		kernel: 'flanger' as const,
		rampSec: MOD_FX_RAMP_SEC,
		crossfadeSec: MOD_FX_CROSSFADE_SEC,
		denormalFlush: MOD_FX_DENORMAL_FLUSH,
		minDelaySec: FLANGER_MIN_DELAY_SEC,
		maxDelaySec: FLANGER_MAX_DELAY_SEC,
		headroomFrames: FLANGER_HEADROOM_FRAMES,
		feedback: Math.max(-FLANGER_FEEDBACK_MAX, Math.min(FLANGER_FEEDBACK_MAX, feedback)),
		spreadDeg
	};
}

/** Phaser processorOptions; voicing overridable (pair export), stages 4 or 8, feedback clamped to +-0.75. */
export function phaserProcessorOptions(voicing: Partial<PhaserVoicing> = {}) {
	const stages = voicing.stages ?? PHASER_STAGES;
	const feedback = voicing.feedback ?? PHASER_FEEDBACK;
	if (!PHASER_STAGES_ALLOWED.includes(stages))
		throw new RangeError(`mod-fx-graph: phaser stages must be 4 or 8, got ${stages}`);
	if (!Number.isFinite(feedback))
		throw new RangeError(`mod-fx-graph: feedback must be finite, got ${feedback}`);
	return {
		kernel: 'phaser' as const,
		rampSec: MOD_FX_RAMP_SEC,
		crossfadeSec: MOD_FX_CROSSFADE_SEC,
		denormalFlush: MOD_FX_DENORMAL_FLUSH,
		stages,
		minHz: PHASER_MIN_HZ,
		maxHz: PHASER_MAX_HZ,
		feedback: Math.max(-PHASER_FEEDBACK_MAX, Math.min(PHASER_FEEDBACK_MAX, feedback)),
		spreadDeg: voicing.spreadDeg ?? PHASER_SPREAD_DEG
	};
}

/** Trans processorOptions; duty 0..1, edge a trans-gate shape (its ms), depthLaw a trans-gate law. */
export function transProcessorOptions(voicing: Partial<TransVoicing> = {}) {
	const duty = voicing.duty ?? TRANS_DUTY;
	const edge = voicing.edge ?? TRANS_EDGE_DEFAULT;
	const depthLaw = voicing.depthLaw ?? TRANS_DEPTH_LAW_DEFAULT;
	if (!(duty >= 0 && duty <= 1))
		throw new RangeError(`mod-fx-graph: trans duty must be 0..1, got ${duty}`);
	if (!Object.hasOwn(TRANS_EDGE_SHAPES, edge))
		throw new RangeError(`mod-fx-graph: trans edge is unknown, got ${String(edge)}`);
	if (!Object.hasOwn(TRANS_DEPTH_LAWS, depthLaw))
		throw new RangeError(`mod-fx-graph: trans depthLaw is unknown, got ${String(depthLaw)}`);
	return {
		kernel: 'trans' as const,
		rampSec: MOD_FX_RAMP_SEC,
		crossfadeSec: MOD_FX_CROSSFADE_SEC,
		denormalFlush: MOD_FX_DENORMAL_FLUSH,
		duty,
		edgeSec: TRANS_EDGE_SHAPES[edge] / 1000,
		depthLaw,
		spreadDeg: 0
	};
}

/** Why a TRANS at `bpm` with free cycle `timeMs` is inert, or null when it can gate. */
export function transInertReason(bpm: number | null, timeMs: number | null): string | null {
	if (timeMs !== null) {
		assertTransTimeMs(timeMs);
		return null;
	}
	return bpm === null || transCycleSeconds(1, bpm) === null ? TRANS_NO_TEMPO_REASON : null;
}

const KERNEL_OPTIONS = {
	flanger: flangerProcessorOptions,
	phaser: phaserProcessorOptions,
	trans: transProcessorOptions
} as const;
export type ModFxKernel = keyof typeof KERNEL_OPTIONS;

//----------------------------------------------------------------------------- graph

export interface ModFxGraphOptions {
	kernel: ModFxKernel;
	cycleBeats: ModFxCycleBeats;
	timeMs: number | null;
	/** `stages` applies to the phaser only; `duty`, `edge` and `depthLaw` to trans only. */
	voicing?: Partial<PhaserVoicing & TransVoicing>;
}

export interface ModFxGraph extends ModFxNodePort {
	readonly node: AudioWorkletNode;
	/** LEVEL/DEPTH 0..1; 0 is the dry input bit for bit. */
	setDepth(depth: number): void;
	/** Engage (ramps in from the grid phase now) or release (ramps out). */
	setActive(on: boolean): void;
}

/** Load the mod FX worklet module on `ctx` (the browser dedupes a repeated URL). */
export function loadModFxModule(ctx: BaseAudioContext): Promise<void> {
	if (ctx.audioWorklet === undefined) throw new Error('mod-fx-graph: AudioWorklet is unavailable');
	return ctx.audioWorklet.addModule(modFxProcessorUrl);
}

/** Build one stereo mod FX insert on a context whose module is loaded (`loadModFxModule`). */
export function createModFxGraph(ctx: BaseAudioContext, options: ModFxGraphOptions): ModFxGraph {
	const node = new AudioWorkletNode(ctx, MOD_FX_PROCESSOR_NAME, {
		numberOfInputs: 1,
		numberOfOutputs: 1,
		outputChannelCount: [2],
		processorOptions: KERNEL_OPTIONS[options.kernel](options.voicing)
	});
	const depth = node.parameters.get('depth');
	if (depth === undefined) throw new Error('mod-fx-graph: processor has no depth parameter');
	return {
		node,
		effect: options.kernel,
		cycleBeats: options.cycleBeats,
		timeMs: options.timeMs,
		context: ctx,
		postAnchor: (anchor: LfoAnchor) => node.port.postMessage({ type: 'anchor', anchor }),
		crossfadeWet: () => node.port.postMessage({ type: 'crossfade' }),
		setDepth: (value: number) => {
			if (!(value >= 0 && value <= 1))
				throw new RangeError(`mod-fx-graph: depth must be 0..1, got ${value}`);
			depth.setValueAtTime(value, ctx.currentTime);
		},
		setActive: (on: boolean) => node.port.postMessage({ type: 'active', on })
	};
}
