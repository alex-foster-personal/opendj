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
 *             +-> toneBright (highpass) -> brightSend -+-> fxIn -+-> echoIn -> echo kernel (loop sum -> two
 *                                                               |            crossfaded lines <-> loopHp -> loopLp -> feedback)
 *                                                               |            -> echoReturn ----------> cue / fader
 *                                                               +-> spaceIn -> space (convolver) -> spaceReturn -> cue / fader
 *
 * Placement (COLORFX-10754): the deck's stems sum upstream of trim, so the send
 * hears the stem balance and a muted stem sends nothing new while the loop
 * rings out. The send taps `colorBus`, after EQ and FILTER. Both returns feed
 * the channel's cue and fader only, so the phones hear the effect and the
 * channel fader cuts the wet tail (a pre-fader insert, DJM-A9 behaviour,
 * (private path) section 3). Pre-EQ placement (the A9
 * diagram) is an open question. tests/unit/color-fx-placement.test.mjs and
 * tests/e2e/color-fx-placement-render.spec.ts pin this, so a move must change
 * them on purpose.
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
 * crossfades what goes IN and lets what is already in the loop ring out. A retune
 * crossfades the two echo lines over 30 ms and never ramps a delay time.
 *
 * CRUSH (SNDCOL-10655) is a separate branch, tapped BEFORE the FILTER from the
 * channel's `high` EQ node and returned into the color bus, crossfaded
 * linearly against `filterDry` (`color-fx-crush.ts`):
 *
 *   high -+-> filterDry / filterLp / filterHp -> colorBus
 *         +-> crush worklet -> highpass -> crushWet -> colorBus
 */
import { filterSideCorners } from './filter-slope';
import {
	COLOR_FX_ECHO_BEATS,
	COLOR_FX_MIN_BPM,
	colorFxTargets,
	echoDelaySeconds,
	type ColorFxSettings,
	type ColorFxTargets
} from './color-fx';
import {
	CRUSH_CLOSED,
	applyCrushTargets,
	buildCrushGraph,
	crushTargets,
	type CrushGraph,
	type CrushVoicing
} from './color-fx-crush';
import { buildEchoKernel, type EchoKernel } from './fx/echo-kernel';
import { glideCorner } from './filter-glide';
import { SPACE_IMPULSE_SEED, spaceImpulse } from './space-impulse';
import { FILTER_GLIDE_DOMAIN, FILTER_GLIDE_S } from '$lib/player/constants';

/** Longest echo line: the longest division at the slowest accepted tempo. */
export const COLOR_FX_ECHO_MAX_DELAY_SEC = Math.max(...COLOR_FX_ECHO_BEATS) * (60 / COLOR_FX_MIN_BPM);

/** Echo feedback per repeat (0.55 = about -5.2 dB a repeat) and the loop's tone. */
export const ECHO_FEEDBACK = 0.55;
export const ECHO_LOOP_HP_HZ = 180;
export const ECHO_LOOP_LP_HZ = 3000;
export const ECHO_WET_GAIN = 0.8;

/** SPACE tail: the low band decays 60 dB over this many seconds, the highs in half (space-impulse.ts). */
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

/** Voicing overrides, for the blind A/B page's variants. The shipped voicing is the defaults. */
export interface ColorFxGraphOptions {
	/** Checked by the kernel's setFeedback law: a value outside [0, 0.95] throws. */
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
	/** The shared echo loop; retunes crossfade its two lines. */
	echo: EchoKernel;
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
	| 'createGain' | 'createDelay' | 'createBiquadFilter' | 'createWaveShaper' | 'createChannelMerger' | 'createConvolver'
	| 'createBuffer' | 'sampleRate'
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

/** The SPACE impulse response (space-impulse.ts v2), built once per splice (never per render quantum). */
export function spaceImpulseBuffer(ctx: GraphContext, seconds = SPACE_RT60_SEC): AudioBuffer {
	const [left, right] = spaceImpulse(ctx.sampleRate, seconds, SPACE_IMPULSE_SEED);
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
	const echo = buildEchoKernel(ctx, {
		mode: 'echo',
		maxDelaySec: COLOR_FX_ECHO_MAX_DELAY_SEC,
		feedback: options.echoFeedback ?? ECHO_FEEDBACK,
		loopHpHz: ECHO_LOOP_HP_HZ,
		loopLpHz: ECHO_LOOP_LP_HZ
	});
	const echoLoopHp = echo.loopHp;
	const echoLoopLp = echo.loopLp;
	const echoFeedback = echo.feedback;
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
	echoIn.connect(echo.input);
	echo.output.connect(echoReturn);
	fxIn.connect(spaceIn);
	for (const destination of destinations) {
		echoReturn.connect(destination);
		spaceReturn.connect(destination);
	}
	return {
		toneDark, toneBright, darkSend, brightSend, fxIn, echoIn, echo, echoLoopHp, echoLoopLp, echoFeedback,
		echoReturn, spaceIn, space: null, spaceReturn,
		nodes: [
			toneDark, toneBright, darkSend, brightSend, fxIn, echoIn, echoReturn, spaceIn, spaceReturn,
			...echo.nodes
		]
	};
}

/** Attach the SPACE convolver (and its impulse response) on first use. Silent:
 * its input gain is still closed when it joins the graph. */
export function attachSpace(ctx: GraphContext, graph: ColorFxGraph, options: ColorFxGraphOptions = {}): ConvolverNode {
	if (graph.space !== null) return graph.space;
	const space = ctx.createConvolver();
	space.normalize = false; // before the buffer: the impulse carries its own level (space-impulse.ts)
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
	/** FXFILT-09: second sections at 24 dB/oct; absent or null at 12. */
	filterLp2?: BiquadFilterNode | null;
	filterHp2?: BiquadFilterNode | null;
	filterDry: GainNode;
	filterLpWet: GainNode;
	filterHpWet: GainNode;
}

/** Drive the FILTER nodes to their part of the targets: corners on the FILTER glide, gains on the smoothing ramp. */
export function applyColorFilterTargets(
	nodes: ColorFilterNodes,
	targets: ColorFxTargets,
	now: number,
	sampleRate: number
): void {
	const f = targets.filter;
	for (const p of filterSideCorners(nodes.filterLp, nodes.filterLp2))
		glideCorner(p, f.lpHz, now, sampleRate, FILTER_GLIDE_DOMAIN, FILTER_GLIDE_S);
	for (const p of filterSideCorners(nodes.filterHp, nodes.filterHp2))
		glideCorner(p, f.hpHz, now, sampleRate, FILTER_GLIDE_DOMAIN, FILTER_GLIDE_S);
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
	if (echoSeconds !== null) graph.echo.setTime(echoSeconds, now);
}

/** The channel nodes the color knob drives: the FILTER nodes plus the color
 * bus and the two destinations the SPACE / ECHO returns feed. */
export interface ColorChannelNodes extends ColorFilterNodes {
	/** The pre-color EQ stage, CRUSH's tap. */
	high: AudioNode;
	colorBus: GainNode;
	cue: GainNode;
	fader: GainNode;
	/** STEMPERF-11663: stem FX sends -> stemFxSend -> fxIn (connected at splice). */
	stemFxSend?: GainNode;
}

/** One channel's color FX runtime: its nodes and, once spliced, its graph. */
export interface ColorChannelRuntime {
	nodes: ColorChannelNodes;
	graph: ColorFxGraph | null;
	/** CRUSH nodes, spliced once its worklet has loaded on this context (spliceCrush). */
	crush: CrushGraph | null;
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

/** Splice this channel's CRUSH nodes (once; the worklet module must already be
 * loaded on the channel's context). Silent: built with its wet gain at 0. */
export function spliceCrush(
	rt: ColorChannelRuntime,
	voicing?: CrushVoicing,
	onProcessorError?: (graph: CrushGraph) => void
): CrushGraph {
	if (rt.crush !== null) return rt.crush;
	rt.crush = buildCrushGraph(rt.nodes.colorBus.context, rt.nodes.high, rt.nodes.colorBus, voicing, onProcessorError);
	return rt.crush;
}

/**
 * Drive one channel to `settings` at knob `value`, the echo timed to
 * `tempoBpm`: the FILTER nodes always, the SPACE / ECHO graph once the channel
 * selects SPACE or ECHO (spliced then, on the channel's own context), and the
 * CRUSH nodes whenever they are spliced, at PARAMETER `crushParameter`. The
 * live module and the render test both call this, so the render measures
 * shipped code.
 */
export function driveColorChannel(
	rt: ColorChannelRuntime,
	settings: ColorFxSettings,
	tempoBpm: number | null,
	value: number,
	crushParameter: number,
	options: ColorFxGraphOptions = {}
): void {
	const echoSeconds = echoDelaySeconds(settings.echo_beats, tempoBpm);
	const targets = colorFxTargets(settings.effect, value, echoSeconds !== null);
	const ctx = rt.nodes.colorBus.context;
	const now = ctx.currentTime;
	applyColorFilterTargets(rt.nodes, targets, now, ctx.sampleRate);
	if ((settings.effect === 'space' || settings.effect === 'echo') && rt.graph === null) {
		const graph = buildColorFxGraph(ctx, rt.nodes.colorBus, [rt.nodes.cue, rt.nodes.fader], options);
		rt.graph = graph;
		rt.nodes.stemFxSend?.connect(graph.fxIn);
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
	// After the FILTER drive: on CRUSH this replaces the dry ramp just scheduled
	// with the linear crossfade, so dry and wet stay complementary. A failed
	// processor is driven closed, so the FILTER-bypass dry ramp stands.
	if (rt.crush !== null) {
		const onCrush = settings.effect === 'crush' && rt.crush.failure === null;
		const crush = onCrush ? crushTargets(value, crushParameter) : CRUSH_CLOSED;
		applyCrushTargets(rt.crush, rt.nodes.filterDry.gain, crush, onCrush, now, COLOR_FX_RAMP_SEC);
	}
}
