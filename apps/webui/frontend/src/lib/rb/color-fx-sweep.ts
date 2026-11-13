/**
 * SWEEP colour kernel, core slice (#9803).
 *
 * DJM-A9 SWEEP (manual p. 40, mimicked: directions and PARAMETER meaning):
 * counterclockwise a notch whose bandwidth grows, clockwise a unity-peak
 * bandpass whose bandwidth narrows, PARAMETER the centre frequency.
 * Native nodes only: two `BiquadFilterNode`s (`notch`, `bandpass`, Q linear,
 * Q = 1/k), each behind its own wet gain, crossfaded against the channel's
 * `filterDry`. Law constants mined from parked PR #492; the k range and the CW
 * makeup are calibrated later (#9808).
 *
 * Pure on purpose: callers pass the signed travel from `colorFxTravel`, so this
 * module has no imports and loads in node tests as is.
 */

export const SWEEP_FC_MIN_HZ = 20;
export const SWEEP_FC_MAX_HZ = 20000;
export const SWEEP_K_MIN = 0.1;
export const SWEEP_K_MAX = 2.0;
/** The wet path crossfades in over this fraction of knob travel. */
export const SWEEP_WET_CROSSFADE_TRAVEL = 0.3;
/** Linear ramp for every SWEEP parameter move (matches COLOR_FX_RAMP_SEC). */
export const SWEEP_RAMP_SEC = 0.01;

export interface SweepTargets {
	centreHz: number;
	notchQ: number;
	bandpassQ: number;
	notchWet: number;
	bandpassWet: number;
	/** What the channel's `filterDry` is driven to while SWEEP owns it. */
	dryGain: number;
}

/** PARAMETER 0..1 to the centre frequency, log 20 Hz .. 20 kHz (0.5 is about 632 Hz). */
export function sweepCentreHz(parameter: number): number {
	const p = Math.min(1, Math.max(0, parameter));
	return SWEEP_FC_MIN_HZ * Math.pow(SWEEP_FC_MAX_HZ / SWEEP_FC_MIN_HZ, p);
}

/** Notch k at CCW travel 0..1: rises from SWEEP_K_MIN to SWEEP_K_MAX (wider notch). */
export function sweepNotchK(travel: number): number {
	return SWEEP_K_MIN + (SWEEP_K_MAX - SWEEP_K_MIN) * Math.min(1, Math.max(0, travel));
}

/** Bandpass k at CW travel 0..1: falls from SWEEP_K_MAX to SWEEP_K_MIN (narrower band). */
export function sweepBandpassK(travel: number): number {
	return SWEEP_K_MAX - (SWEEP_K_MAX - SWEEP_K_MIN) * Math.min(1, Math.max(0, travel));
}

/** Wet level at travel 0..1: linear 0 -> 1 over the first SWEEP_WET_CROSSFADE_TRAVEL, then 1. */
export function sweepWet(travel: number): number {
	return Math.min(1, Math.max(0, travel) / SWEEP_WET_CROSSFADE_TRAVEL);
}

/** Targets at signed travel (-1 full CCW .. 0 detent .. 1 full CW, from colorFxTravel) and PARAMETER. */
export function sweepTargets(signedTravel: number, parameter: number): SweepTargets {
	if (!Number.isFinite(signedTravel) || !Number.isFinite(parameter))
		throw new Error(
			`sweepTargets needs finite inputs, got travel ${signedTravel}, parameter ${parameter}`
		);
	const travel = Math.min(1, Math.abs(signedTravel));
	const wet = sweepWet(travel);
	const ccw = signedTravel < 0;
	return {
		centreHz: sweepCentreHz(parameter),
		notchQ: 1 / sweepNotchK(ccw ? travel : 0),
		bandpassQ: 1 / sweepBandpassK(ccw ? 0 : travel),
		notchWet: ccw ? wet : 0,
		bandpassWet: ccw ? 0 : wet,
		dryGain: 1 - wet
	};
}

//--------------------------------------------------------------------- node build

/** The minimal context the SWEEP build needs (real or offline). */
export type SweepContext = Pick<BaseAudioContext, 'createGain' | 'createBiquadFilter'>;

export interface SweepGraph {
	notch: BiquadFilterNode;
	bandpass: BiquadFilterNode;
	notchWet: GainNode;
	bandpassWet: GainNode;
	/** Every node, for disposal. */
	nodes: AudioNode[];
}

function _sweepFilter(
	ctx: SweepContext,
	type: BiquadFilterType,
	q: number,
	hz: number
): BiquadFilterNode {
	const node = ctx.createBiquadFilter();
	node.type = type;
	node.frequency.value = hz;
	node.Q.value = q;
	return node;
}

/** Build the SWEEP nodes from `source` (pre-color `high`) into `destination` (`colorBus`), closed. */
export function buildSweepGraph(
	ctx: SweepContext,
	source: AudioNode,
	destination: AudioNode
): SweepGraph {
	const rest = sweepTargets(0, 0.5);
	const notch = _sweepFilter(ctx, 'notch', rest.notchQ, rest.centreHz);
	const bandpass = _sweepFilter(ctx, 'bandpass', rest.bandpassQ, rest.centreHz);
	const notchWet = ctx.createGain();
	notchWet.gain.value = 0;
	const bandpassWet = ctx.createGain();
	bandpassWet.gain.value = 0;
	source.connect(notch);
	source.connect(bandpass);
	notch.connect(notchWet);
	bandpass.connect(bandpassWet);
	notchWet.connect(destination);
	bandpassWet.connect(destination);
	return {
		notch,
		bandpass,
		notchWet,
		bandpassWet,
		nodes: [notch, bandpass, notchWet, bandpassWet]
	};
}

function _linear(param: AudioParam, value: number, now: number): void {
	param.cancelScheduledValues(now);
	param.setValueAtTime(param.value, now);
	param.linearRampToValueAtTime(value, now + SWEEP_RAMP_SEC);
}

/** Drive the SWEEP nodes; `dryGain` (the channel's `filterDry`) follows only while SWEEP owns it. */
export function applySweepTargets(
	graph: SweepGraph,
	dryGain: AudioParam,
	targets: SweepTargets,
	sweepOwnsDry: boolean,
	now: number
): void {
	for (const filter of [graph.notch, graph.bandpass])
		_linear(filter.frequency, targets.centreHz, now);
	_linear(graph.notch.Q, targets.notchQ, now);
	_linear(graph.bandpass.Q, targets.bandpassQ, now);
	_linear(graph.notchWet.gain, targets.notchWet, now);
	_linear(graph.bandpassWet.gain, targets.bandpassWet, now);
	if (sweepOwnsDry) _linear(dryGain, targets.dryGain, now);
}
