/**
 * Color FX, graph half (COLORFX-01..03): the per-channel SPACE / ECHO nodes and
 * the targets -> AudioParam drive. No Svelte state, so the same module runs in
 * the node tests (fake context) and in the headless Chromium render test (real
 * OfflineAudioContext), which is what the objective checks measure.
 *
 * Spliced once per channel per audio context, on the first non-FILTER use,
 * beside the channel's color bus (the sum of the FILTER branches):
 *
 *   colorBus -+-> (dry, always: colorBus -> cue / fader) ------------------------+
 *             +-> toneDark (lowpass)    -> darkSend   -+                         |
 *             +-> toneBright (highpass) -> brightSend -+-> fxIn -+-> echoIn -> echo <-> loopHp -> loopLp -> feedback
 *                                                               |            echo -> echoReturn ----------> cue / fader
 *                                                               +-> spaceIn -> space (convolver) -> spaceReturn -> cue / fader
 *
 * The convolver is attached only when a SPACE send first opens: it is the
 * expensive node (measured about 2.7% of a core per channel even with its send
 * closed), so a channel that only ever echoes never pays for it. Once the send
 * has been closed for the whole tail plus a hold (`spaceDetachDelaySec`), the
 * convolver is detached again (`detachIdleSpace`); the next real turn attaches
 * a fresh one. Both happen while every send into it is closed, so neither can
 * click, and the detach waits until the tail is below -90 dB.
 *
 * Native nodes only: DelayNode, ConvolverNode and BiquadFilterNode render inside
 * the browser's audio thread with buffers sized at splice, so nothing allocates
 * per render quantum. The returns are fixed gains: the knob only ever moves the
 * sends and the effect inputs, so an effect switch or a return to center
 * crossfades what goes IN and lets what is already in the loop ring out.
 */
import {
	COLOR_FX_ECHO_BEATS,
	COLOR_FX_MIN_BPM,
	colorFxTargets,
	echoDelaySeconds,
	type ColorFxSettings,
	type ColorFxTargets
} from './color-fx';

/**
 * Stereo impulse response: decorrelated noise under an exponential decay that
 * falls 60 dB over `seconds`. A seeded generator (not Math.random) so the same
 * length always yields the same tail and a test can pin it.
 */
function reverbImpulse(
	sampleRate: number,
	seconds: number,
	seed = 0x5eed
): [Float32Array<ArrayBuffer>, Float32Array<ArrayBuffer>] {
	const length = Math.max(1, Math.round(sampleRate * seconds));
	const left = new Float32Array(length);
	const right = new Float32Array(length);
	let state = seed >>> 0 || 1;
	const next = (): number => {
		state ^= state << 13;
		state ^= state >>> 17;
		state ^= state << 5;
		return ((state >>> 0) / 0xffffffff) * 2 - 1;
	};
	const decayPerSample = Math.log(1000) / length;
	for (let i = 0; i < length; i++) {
		const envelope = Math.exp(-decayPerSample * i);
		left[i] = next() * envelope;
		right[i] = next() * envelope;
	}
	return [left, right];
}

/** Longest echo line: the longest division at the slowest accepted tempo. */
export const COLOR_FX_ECHO_MAX_DELAY_SEC = Math.max(...COLOR_FX_ECHO_BEATS) * (60 / COLOR_FX_MIN_BPM);

/** Echo feedback per repeat (0.55 = about -5.2 dB a repeat) and the loop's tone. */
export const ECHO_FEEDBACK = 0.55;
export const ECHO_LOOP_HP_HZ = 180;
export const ECHO_LOOP_LP_HZ = 3000;
export const ECHO_WET_GAIN = 0.8;

/** SPACE tail: decays 60 dB over this many seconds. */
export const SPACE_RT60_SEC = 2.6;
export const SPACE_WET_GAIN = 0.8;

/** How long a SPACE send must stay closed AFTER its tail has gone before the
 * convolver detaches: long enough that a DJ parking the knob at center between
 * phrases does not pay a re-attach on every turn. */
export const SPACE_DETACH_HOLD_SEC = 3;
/** Level the tail must be under before a detach (dB re full send). */
export const SPACE_DETACH_FLOOR_DB = -90;

/** Time constant for knob and effect-switch moves (matches PARAM_SMOOTH_S). */
export const COLOR_FX_RAMP_SEC = 0.01;
/** Echo-time glide, so a tempo change bends rather than clicks. */
export const ECHO_TIME_GLIDE_SEC = 0.05;

/** Voicing overrides, for the blind A/B page's variants. The shipped voicing is the defaults. */
export interface ColorFxGraphOptions {
	echoFeedback?: number;
	spaceSeconds?: number;
	echoWet?: number;
	spaceWet?: number;
	/** Test/A-B override of the idle-detach delay (seconds after the send closed). */
	spaceIdleDetachSec?: number;
}

export interface ColorFxGraph {
	toneDark: BiquadFilterNode;
	toneBright: BiquadFilterNode;
	darkSend: GainNode;
	brightSend: GainNode;
	fxIn: GainNode;
	echoIn: GainNode;
	echo: DelayNode;
	echoLoopHp: BiquadFilterNode;
	echoLoopLp: BiquadFilterNode;
	echoFeedback: GainNode;
	echoReturn: GainNode;
	spaceIn: GainNode;
	/** Null until the channel first selects SPACE (see attachSpace). */
	space: ConvolverNode | null;
	spaceReturn: GainNode;
	/** Every node, for disposal. */
	nodes: AudioNode[];
}

type GraphContext = Pick<
	BaseAudioContext,
	'createGain' | 'createDelay' | 'createBiquadFilter' | 'createConvolver' | 'createBuffer' | 'sampleRate'
>;

function _gain(ctx: GraphContext, value: number): GainNode {
	const node = ctx.createGain();
	node.gain.value = value;
	return node;
}

function _biquad(ctx: GraphContext, type: BiquadFilterType, hz: number): BiquadFilterNode {
	const node = ctx.createBiquadFilter();
	node.type = type;
	node.frequency.value = hz;
	return node;
}

/** The SPACE impulse response, built once per splice (never per render quantum). */
export function spaceImpulseBuffer(ctx: GraphContext, seconds = SPACE_RT60_SEC): AudioBuffer {
	const [left, right] = reverbImpulse(ctx.sampleRate, seconds, 0xc010f);
	const buffer = ctx.createBuffer(2, left.length, ctx.sampleRate);
	buffer.copyToChannel(left, 0);
	buffer.copyToChannel(right, 1);
	return buffer;
}

/**
 * Build the SPACE / ECHO nodes, fed from `source` (the channel's color bus) and
 * returning into each of `destinations` (the channel's cue and fader). Built
 * closed: every send and effect input at 0, so splicing is silent.
 */
export function buildColorFxGraph(
	ctx: GraphContext,
	source: AudioNode,
	destinations: readonly AudioNode[],
	options: ColorFxGraphOptions = {}
): ColorFxGraph {
	const toneDark = _biquad(ctx, 'lowpass', 20000);
	const toneBright = _biquad(ctx, 'highpass', 20);
	const darkSend = _gain(ctx, 0);
	const brightSend = _gain(ctx, 0);
	const fxIn = _gain(ctx, 1);
	const echoIn = _gain(ctx, 0);
	const echo = ctx.createDelay(COLOR_FX_ECHO_MAX_DELAY_SEC);
	const echoLoopHp = _biquad(ctx, 'highpass', ECHO_LOOP_HP_HZ);
	const echoLoopLp = _biquad(ctx, 'lowpass', ECHO_LOOP_LP_HZ);
	const echoFeedback = _gain(ctx, options.echoFeedback ?? ECHO_FEEDBACK);
	const echoReturn = _gain(ctx, options.echoWet ?? ECHO_WET_GAIN);
	const spaceIn = _gain(ctx, 0);
	const spaceReturn = _gain(ctx, options.spaceWet ?? SPACE_WET_GAIN);

	source.connect(toneDark);
	source.connect(toneBright);
	toneDark.connect(darkSend);
	toneBright.connect(brightSend);
	darkSend.connect(fxIn);
	brightSend.connect(fxIn);
	fxIn.connect(echoIn);
	echoIn.connect(echo);
	echo.connect(echoLoopHp);
	echoLoopHp.connect(echoLoopLp);
	echoLoopLp.connect(echoFeedback);
	echoFeedback.connect(echo);
	echo.connect(echoReturn);
	fxIn.connect(spaceIn);
	for (const destination of destinations) {
		echoReturn.connect(destination);
		spaceReturn.connect(destination);
	}
	return {
		toneDark, toneBright, darkSend, brightSend, fxIn, echoIn, echo, echoLoopHp, echoLoopLp, echoFeedback,
		echoReturn, spaceIn, space: null, spaceReturn,
		nodes: [toneDark, toneBright, darkSend, brightSend, fxIn, echoIn, echo, echoLoopHp, echoLoopLp, echoFeedback,
			echoReturn, spaceIn, spaceReturn]
	};
}

/** Attach the SPACE convolver (and its impulse response) on first use. Silent:
 * its input gain is still closed when it joins the graph. */
export function attachSpace(ctx: GraphContext, graph: ColorFxGraph, options: ColorFxGraphOptions = {}): ConvolverNode {
	if (graph.space !== null) return graph.space;
	const space = ctx.createConvolver();
	space.buffer = spaceImpulseBuffer(ctx, options.spaceSeconds ?? SPACE_RT60_SEC);
	graph.spaceIn.connect(space);
	space.connect(graph.spaceReturn);
	graph.space = space;
	graph.nodes.push(space);
	return space;
}

function _ramp(param: AudioParam, value: number, now: number, tau: number): void {
	param.cancelScheduledValues(now);
	param.setTargetAtTime(value, now, tau);
}

/** The FILTER nodes the color knob also drives (the channel graph's own). */
export interface ColorFilterNodes {
	filterLp: BiquadFilterNode;
	filterHp: BiquadFilterNode;
	filterDry: GainNode;
	filterLpWet: GainNode;
	filterHpWet: GainNode;
}

/** Drive the FILTER nodes to their part of the targets, on the smoothing ramp. */
export function applyColorFilterTargets(nodes: ColorFilterNodes, targets: ColorFxTargets, now: number): void {
	const f = targets.filter;
	_ramp(nodes.filterLp.frequency, f.lpHz, now, COLOR_FX_RAMP_SEC);
	_ramp(nodes.filterHp.frequency, f.hpHz, now, COLOR_FX_RAMP_SEC);
	_ramp(nodes.filterDry.gain, f.dryGain, now, COLOR_FX_RAMP_SEC);
	_ramp(nodes.filterLpWet.gain, f.lpWetGain, now, COLOR_FX_RAMP_SEC);
	_ramp(nodes.filterHpWet.gain, f.hpWetGain, now, COLOR_FX_RAMP_SEC);
}

/** Drive the SPACE / ECHO nodes to the targets; `echoSeconds` sets the line when known. */
export function applyColorFxTargets(
	graph: ColorFxGraph,
	targets: ColorFxTargets,
	echoSeconds: number | null,
	now: number
): void {
	_ramp(graph.toneDark.frequency, targets.darkHz, now, COLOR_FX_RAMP_SEC);
	_ramp(graph.toneBright.frequency, targets.brightHz, now, COLOR_FX_RAMP_SEC);
	_ramp(graph.darkSend.gain, targets.darkSend, now, COLOR_FX_RAMP_SEC);
	_ramp(graph.brightSend.gain, targets.brightSend, now, COLOR_FX_RAMP_SEC);
	_ramp(graph.echoIn.gain, targets.echoIn, now, COLOR_FX_RAMP_SEC);
	_ramp(graph.spaceIn.gain, targets.spaceIn, now, COLOR_FX_RAMP_SEC);
	if (echoSeconds !== null) _ramp(graph.echo.delayTime, echoSeconds, now, ECHO_TIME_GLIDE_SEC);
}

/** The channel nodes the color knob drives: the FILTER nodes plus the color
 * bus and the two destinations the SPACE / ECHO returns feed. */
export interface ColorChannelNodes extends ColorFilterNodes {
	colorBus: GainNode;
	cue: GainNode;
	fader: GainNode;
}

/** One channel's color FX runtime: its nodes and, once spliced, its graph. */
export interface ColorChannelRuntime {
	nodes: ColorChannelNodes;
	graph: ColorFxGraph | null;
	/** Context time the SPACE send last closed (input or both sends at 0), or null while open. */
	spaceQuietSince: number | null;
}

/**
 * Seconds from a SPACE send closing to a safe detach: the impulse length (the
 * convolver's output is exactly the tail of what went in), plus the send ramp
 * falling below SPACE_DETACH_FLOOR_DB, plus SPACE_DETACH_HOLD_SEC.
 */
export function spaceDetachDelaySec(options: ColorFxGraphOptions = {}): number {
	if (options.spaceIdleDetachSec !== undefined) return options.spaceIdleDetachSec;
	const rampToFloorSec = COLOR_FX_RAMP_SEC * Math.log(10 ** (-SPACE_DETACH_FLOOR_DB / 20));
	return (options.spaceSeconds ?? SPACE_RT60_SEC) + rampToFloorSec + SPACE_DETACH_HOLD_SEC;
}

/**
 * Detach this channel's SPACE convolver when its send has been closed for
 * `spaceDetachDelaySec`. Returns whether it detached. Called by the live
 * module's timer and by the render harness at its checkpoints.
 */
export function detachIdleSpace(rt: ColorChannelRuntime, now: number, options: ColorFxGraphOptions = {}): boolean {
	const graph = rt.graph;
	if (graph === null || graph.space === null || rt.spaceQuietSince === null) return false;
	if (now - rt.spaceQuietSince < spaceDetachDelaySec(options)) return false;
	const space = graph.space;
	graph.spaceIn.disconnect(space);
	space.disconnect();
	graph.nodes.splice(graph.nodes.indexOf(space), 1);
	graph.space = null;
	return true;
}

/**
 * Drive one channel to `settings` at knob `value`, the echo timed to
 * `tempoBpm`: the FILTER nodes always, the SPACE / ECHO graph once the channel
 * leaves FILTER (spliced then, on the channel's own context). The live module
 * and the render test both call this, so the render measures shipped code.
 */
export function driveColorChannel(
	rt: ColorChannelRuntime,
	settings: ColorFxSettings,
	tempoBpm: number | null,
	value: number,
	options: ColorFxGraphOptions = {}
): void {
	const echoSeconds = echoDelaySeconds(settings.echo_beats, tempoBpm);
	const targets = colorFxTargets(settings.effect, value, echoSeconds !== null);
	const ctx = rt.nodes.colorBus.context;
	const now = ctx.currentTime;
	applyColorFilterTargets(rt.nodes, targets, now);
	if (settings.effect !== 'filter' && rt.graph === null) {
		const graph = buildColorFxGraph(ctx, rt.nodes.colorBus, [rt.nodes.cue, rt.nodes.fader], options);
		// A cold line starts at its time: gliding up from 0 would pitch-sweep the first repeats.
		if (echoSeconds !== null) graph.echo.delayTime.value = echoSeconds;
		rt.graph = graph;
	}
	const spaceOpen = targets.spaceIn > 0 && (targets.darkSend > 0 || targets.brightSend > 0);
	if (spaceOpen) rt.spaceQuietSince = null;
	else if (rt.spaceQuietSince === null) rt.spaceQuietSince = now;
	// Attached on a real turn only (never at the detent), so a detached SPACE
	// stays detached until the DJ actually turns the knob.
	if (spaceOpen && rt.graph !== null) attachSpace(ctx, rt.graph, options);
	// An already-spliced graph is driven even in FILTER, so leaving SPACE/ECHO
	// closes their inputs on the ramp and lets the tails ring out.
	if (rt.graph !== null) applyColorFxTargets(rt.graph, targets, echoSeconds, now);
}
