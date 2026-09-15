/**
 * The headphone / cue monitor: its own sink, its own graph, its own state.
 *
 * Extracted from `audio-engine.svelte.ts` under convention D5 -- the feature
 * file keeps the call sites, a subsystem with a real boundary keeps its own
 * arithmetic AND its own state. This is the one part of the player that owns a
 * SECOND audio sink: a MediaStreamAudioDestinationNode played through a
 * detached HTMLAudioElement whose `setSinkId` points at the operator's
 * headphones. Everything that follows from that -- the
 * `navigator.mediaDevices` capability checks, the equal-power cue/master
 * blend, the generation counter that stops a stale async selection publishing
 * over a disposed monitor -- lives here and nowhere else.
 *
 * The interface back to the engine is four edges and nothing more:
 *   - `ensureHeadphoneGraph(context, masterGain)`, because the master bus and
 *     the four channel cue gains are the engine's nodes, not this module's;
 *   - `applyHeadphoneMix()`, from the two mixer setters;
 *   - `disposeHeadphoneMonitor()`, from route teardown;
 *   - the three device operations, which take a `MonitorSource` thunk so the
 *     engine's graph is still built at exactly the point inside the operation
 *     it always was, where a failure is wrapped as a headphone error.
 *
 * Nothing here sits on a Class A control path. `setHeadphoneMix` /
 * `setHeadphoneLevel` reach `applyHeadphoneMix()` synchronously; only the
 * device-selection flows await, and those are user-gesture driven rather than
 * transport.
 */

import { HEAD_DELAY_MAX_MS, HEADPHONE_OPERATION_TIMEOUT_MS, PARAM_SMOOTH_S, assertHeadDelayMs, headDelaySeconds } from '$lib/player/constants';
import {
	CUE_LATENCY_CLICK_COUNT,
	CUE_LATENCY_CLICK_MS,
	CUE_LATENCY_PERIOD_MS,
	CUE_LATENCY_PREROLL_MS,
	cueLatencyCaptureMs,
	measureCueLatencyMs
} from '$lib/player/cue-latency';
import { persistMixerConfig } from '$lib/player/mixer-config';
import { deckStates, mixerState } from '$lib/player/state.svelte';
import {
	installHeadphoneOutputLiveness,
	type LivenessVerdict,
	type HeadphoneOutputSnapshot
} from '$lib/rb/audio-output-liveness';
import { recordPerfEvent } from '$lib/rb/perf-event-log';
import { pushToast } from '$lib/stores.svelte';

/**
 * One enumerated headphone output -- structurally the `HeadphoneOutputDevice`
 * of `$lib/rb/mixer-types`, derived from the mixer read model rather than
 * imported from it. A module whose only use for the shape is to filter and
 * merge a list has no business widening that module's importer count.
 */
export type HeadphoneOutput = (typeof mixerState)['headphones']['outputs'][number];

/**
 * The engine's tap for the monitor: the live AudioContext plus the master bus
 * the monitor listens to. A thunk rather than two arguments because it must be
 * resolved lazily, inside the selecting operation's try block.
 */
export type MonitorSource = () => { context: AudioContext; masterGain: GainNode };

/** Legal `HeadphoneState.output_mode` values. */
export const HEADPHONE_OUTPUT_MODES = ['practice', 'two_outputs', 'split_cable'] as const;
export type HeadphoneOutputMode = (typeof HEADPHONE_OUTPUT_MODES)[number];

export function assertHeadphoneOutputMode(mode: unknown): asserts mode is HeadphoneOutputMode {
	if (!HEADPHONE_OUTPUT_MODES.includes(mode as HeadphoneOutputMode)) {
		throw new TypeError(
			`headphone output_mode must be practice, two_outputs, or split_cable; got ${String(mode)}`
		);
	}
}

/** Main-output cue/master gains. Practice with no monitor selected keeps master
 * at unity (the room must never be silenced by MIX) and blends the cue bus on
 * top using the `headphoneMixGains` cue leg; any selected monitor (or
 * `two_outputs`) is master-only. */
export function practiceMainGains(
	mode: unknown,
	selectedOutputDeviceId: string | null,
	mix: number
): { cue: number; master: number } {
	assertHeadphoneOutputMode(mode);
	if (selectedOutputDeviceId !== null && typeof selectedOutputDeviceId !== 'string') {
		throw new TypeError('selected headphone output device id must be a string or null');
	}
	if (mode === 'split_cable') {
		return { cue: 0, master: 0 };
	}
	if (mode !== 'practice' || selectedOutputDeviceId !== null) {
		return { cue: 0, master: 1 };
	}
	return { cue: headphoneMixGains(mix).cue, master: 1 };
}

/** Split-cable L/R gains. Left is master-only mono (room); right is the cue
 * ear, scaled by headphone GAIN. */
export function splitCableGains(
	mode: unknown,
	mix: number,
	level: number
): { left: number; rightCue: number; rightMaster: number } {
	assertHeadphoneOutputMode(mode);
	_assertUnit('headphone level', level);
	if (mode !== 'split_cable') {
		return { left: 0, rightCue: 0, rightMaster: 0 };
	}
	const { cue, master } = headphoneMixGains(mix);
	return { left: 1, rightCue: cue * level, rightMaster: master * level };
}

/** Practice-path cue gain is MIX cue * GAIN. Master on that path is unscaled
 * so GAIN cannot silence the room when MIX is full master. */
export function practiceCueWithGain(practiceCue: number, level: number): number {
	_assertUnit('practice cue', practiceCue);
	_assertUnit('headphone level', level);
	return practiceCue * level;
}

/** The monitor graph: channel cue sum and a master tap, blended equal-power,
 * through one level gain into a MediaStream the sink element plays. Practice
 * mode also owns a second equal-power pair that sits in the main output path. */
export interface HeadphoneNodes {
	cueSum: GainNode;
	masterMonitor: GainNode;
	cueMix: GainNode;
	masterMix: GainNode;
	level: GainNode;
	delay: DelayNode;
	destination: MediaStreamAudioDestinationNode;
	element: HTMLAudioElement;
	practiceCueMix: GainNode;
	practiceMasterMix: GainNode;
	masterSplitter: ChannelSplitterNode;
	cueSplitter: ChannelSplitterNode;
	masterLeftHalf: GainNode;
	masterRightHalf: GainNode;
	cueLeftHalf: GainNode;
	cueRightHalf: GainNode;
	masterMono: GainNode;
	cueMono: GainNode;
	splitLeftGain: GainNode;
	splitRightCueGain: GainNode;
	splitRightMasterGain: GainNode;
	splitMerger: ChannelMergerNode;
}

let _headphoneNodes: HeadphoneNodes | null = null;
/** The engine AudioContext the master mix is pinned onto via setSinkId. */
let _outputContext: AudioContext | null = null;
/** Bumped by every teardown. An operation that started under an older
 * generation refuses to publish rather than resurrect a disposed monitor. */
let _headphoneGeneration = 0;

/** Unit-interval guard for the mix knob. Deliberately a private leaf here
 * rather than an import: the engine's copy guards trim/EQ/fader/master, and
 * reaching back into the feature file for five lines would put an import cycle
 * where a boundary is supposed to be. */
function _assertUnit(name: string, value: number): void {
	if (!Number.isFinite(value) || value < 0 || value > 1) {
		throw new RangeError(`${name} must be within 0..1, got ${value}`);
	}
}

/** Smoothed param write on the monitor's own clock. Reads `currentTime` per
 * call, exactly as the engine's `_setParam` did. */
function _setMonitorParam(nodes: HeadphoneNodes, param: AudioParam, value: number): void {
	param.setTargetAtTime(value, nodes.level.context.currentTime, PARAM_SMOOTH_S);
}

/** Equal-power CUE/MASTER gains, where 0 is full cue and 1 is full master. */
export function headphoneMixGains(mix: number): { cue: number; master: number } {
	_assertUnit('headphone mix', mix);
	if (mix === 0) return { cue: 1, master: 0 };
	if (mix === 1) return { cue: 0, master: 1 };
	return {
		cue: Math.cos((mix * Math.PI) / 2),
		master: Math.sin((mix * Math.PI) / 2)
	};
}

/** MIX knob fill: cue orange at 0 (max left), master accent at 1. Never EQ red. */
export function headphoneMixAccent(mix: number): string {
	_assertUnit('headphone mix', mix);
	const cuePercent = Math.round((1 - mix) * 100);
	return `color-mix(in srgb, var(--rb-orange) ${cuePercent}%, var(--rb-accent))`;
}

export function assertHeadphoneOutputSelection(
	deviceId: string,
	outputs: readonly HeadphoneOutput[]
): void {
	if (typeof deviceId !== 'string' || deviceId.trim() === '') {
		throw new TypeError('headphone output device id must be a non-empty string');
	}
	if (!outputs.some((output) => output.id === deviceId)) {
		throw new RangeError(`headphone output ${deviceId} is not an enumerated headphone output`);
	}
}

/** Merge a browser-authorized output into the serializable read model. */
export function mergeHeadphoneOutput(
	outputs: readonly HeadphoneOutput[],
	device: Pick<MediaDeviceInfo, 'deviceId' | 'label'>
): HeadphoneOutput[] {
	if (typeof device.deviceId !== 'string' || device.deviceId.trim() === '') {
		throw new TypeError('acquired headphone output device id must be a non-empty string');
	}
	if (typeof device.label !== 'string') {
		throw new TypeError('acquired headphone output label must be a string');
	}
	const next = { id: device.deviceId, label: device.label };
	return outputs.some((output) => output.id === next.id)
		? outputs.map((output) => (output.id === next.id ? next : { ...output }))
		: [...outputs.map((output) => ({ ...output })), next];
}

/** Browsers may rotate opaque output IDs when device permission changes. An
 * unenumerated sink is no longer trustworthy, so stop reporting it as live. */
export function reconcileHeadphoneOutputRefresh(
	active: boolean,
	selectedOutputDeviceId: string | null,
	outputs: readonly HeadphoneOutput[]
): { active: boolean; selected_output_device_id: string | null } {
	if (typeof active !== 'boolean') throw new TypeError('headphone active state must be boolean');
	if (selectedOutputDeviceId !== null && typeof selectedOutputDeviceId !== 'string') {
		throw new TypeError('selected headphone output device id must be a string or null');
	}
	if (selectedOutputDeviceId === null || outputs.some((output) => output.id === selectedOutputDeviceId)) {
		return { active, selected_output_device_id: selectedOutputDeviceId };
	}
	return { active: false, selected_output_device_id: null };
}

export function headphoneSelectionStages(): readonly string[] {
	return ['setSinkId', 'attachStream', 'play', 'publish'];
}

export function headphoneReselectionStages(): readonly string[] {
	return [
		'createCandidate',
		'setSinkId',
		'attachStream',
		'play',
		'replaceAndPublish',
		'detachPrevious'
	];
}

export function headphoneReselectionResult(candidateAccepted: boolean): {
	replaceCurrentElement: boolean;
	publishSelection: boolean;
	detachPrevious: boolean;
} {
	if (typeof candidateAccepted !== 'boolean') {
		throw new TypeError('headphone candidate acceptance must be boolean');
	}
	return candidateAccepted
		? { replaceCurrentElement: true, publishSelection: true, detachPrevious: true }
		: { replaceCurrentElement: false, publishSelection: false, detachPrevious: false };
}

export function headphoneOwnershipIsCurrent(
	operationGeneration: number,
	currentGeneration: number,
	nodesOwned: boolean
): boolean {
	return (
		Number.isInteger(operationGeneration) &&
		Number.isInteger(currentGeneration) &&
		operationGeneration === currentGeneration &&
		nodesOwned
	);
}

export function assertHeadphoneOwnership(
	operationGeneration: number,
	currentGeneration: number,
	nodesOwned: boolean
): void {
	if (!headphoneOwnershipIsCurrent(operationGeneration, currentGeneration, nodesOwned)) {
		throw new Error('stale headphone operation cannot publish state after disposal');
	}
}

export async function withHeadphoneOperationTimeout<T>(
	operation: string,
	promise: Promise<T>,
	timeoutMs = HEADPHONE_OPERATION_TIMEOUT_MS
): Promise<T> {
	if (!Number.isInteger(timeoutMs) || timeoutMs <= 0) {
		throw new RangeError(`headphone ${operation} timeout must be a positive integer, got ${timeoutMs}`);
	}
	let timeoutId: ReturnType<typeof setTimeout> | null = null;
	const timeout = new Promise<never>((_, reject) => {
		timeoutId = setTimeout(() => reject(new Error(`headphone ${operation} timed out after ${timeoutMs}ms`)), timeoutMs);
	});
	try {
		return await Promise.race([promise, timeout]);
	} finally {
		if (timeoutId !== null) clearTimeout(timeoutId);
	}
}

/**
 * Write the current mix/level onto the monitor graph.
 *
 * The smoothing clock is the monitor graph's OWN context (`nodes.level.context`
 * is the AudioContext those nodes were created from, i.e. the engine's `_ctx`),
 * so this no longer has to reach back into the engine for it. The engine's
 * `_setParam` guarded `_ctx === null`; that guard was unreachable from here,
 * because a non-null `_headphoneNodes` already implies a live context.
 */
export function applyHeadphoneMix(): void {
	const nodes = _headphoneNodes;
	if (nodes === null) return;
	const mix = mixerState.headphones.mix;
	const level = mixerState.headphones.level;
	const gains = headphoneMixGains(mix);
	const monitorLive =
		mixerState.headphones.output_mode === 'two_outputs' && mixerState.headphones.active;
	_setMonitorParam(nodes, nodes.cueMix.gain, monitorLive ? gains.cue : 0);
	_setMonitorParam(nodes, nodes.masterMix.gain, monitorLive ? gains.master : 0);
	_setMonitorParam(nodes, nodes.level.gain, monitorLive ? level : 0);
	const practice = practiceMainGains(
		mixerState.headphones.output_mode,
		mixerState.headphones.selected_output_device_id,
		mix
	);
	_setMonitorParam(nodes, nodes.practiceCueMix.gain, practiceCueWithGain(practice.cue, level));
	_setMonitorParam(nodes, nodes.practiceMasterMix.gain, practice.master);
	const split = splitCableGains(mixerState.headphones.output_mode, mix, level);
	_setMonitorParam(nodes, nodes.splitLeftGain.gain, split.left);
	_setMonitorParam(nodes, nodes.splitRightCueGain.gain, split.rightCue);
	_setMonitorParam(nodes, nodes.splitRightMasterGain.gain, split.rightMaster);
	nodes.delay.delayTime.setValueAtTime(
		headDelaySeconds(mixerState.headphones.head_delay_ms),
		nodes.level.context.currentTime
	);
}

export function setHeadDelayMs(value: unknown): void {
	assertHeadDelayMs(value);
	mixerState.headphones.head_delay_ms = value;
	persistMixerConfig({ head_delay_ms: value });
	applyHeadphoneMix();
}

type HeadphonesStateWithLiveness = (typeof mixerState)['headphones'] & {
	liveness_verdict: LivenessVerdict;
	liveness_snapshot: HeadphoneOutputSnapshot | null;
};

function _headphonesWithLiveness(): HeadphonesStateWithLiveness {
	return mixerState.headphones as HeadphonesStateWithLiveness;
}

function _publishHeadphoneLiveness(snapshot: HeadphoneOutputSnapshot): void {
	const hp = _headphonesWithLiveness();
	hp.liveness_verdict = snapshot.verdict;
	hp.liveness_snapshot = snapshot;
}

/** UI copy for a live headphone liveness verdict (distinct from the static BT caveat). */
export function headphoneLivenessAlertText(verdict: LivenessVerdict | undefined): string | null {
	if (verdict === 'stalled' || verdict === 'dead') {
		return 'Headphone output not producing sound';
	}
	if (verdict === 'dead-escalated') {
		return 'Headphone output not producing sound - re-select the device or reload';
	}
	return null;
}

/** Case-insensitive label match for Bluetooth-class monitor devices. */
export function monitorLabelIsBluetooth(label: string): boolean {
	if (label === '') return false;
	const lower = label.toLowerCase();
	if (lower.includes('bluetooth')) return true;
	if (lower.includes('airpods')) return true;
	if (lower.includes('a2dp')) return true;
	if (/\bhfp\b/i.test(label)) return true;
	return false;
}

/** Standing two_outputs warning copy, or null outside that mode. */
export function twoOutputsWarning(args: {
	outputMode: unknown;
	selectedLabel: string | null;
}): string | null {
	assertHeadphoneOutputMode(args.outputMode);
	if (args.outputMode !== 'two_outputs') return null;
	const parts = [
		'Two independently clocked devices drift (about 6 ms/min at 100 ppm).',
		'Bluetooth as the monitor leg is for auditioning, not beatmatching.'
	];
	if (args.selectedLabel !== null && monitorLabelIsBluetooth(args.selectedLabel)) {
		parts.push('Selected monitor looks like Bluetooth.');
	}
	return parts.join(' ');
}

/** Sample-accurate monitor lag for a static DelayNode delayTime (loopback acceptance helper). */
export function clickTrainLagMs(opts: {
	delayMs: number;
	sampleRate: number;
	bufferSize: number;
	clickPeriodMs: number;
	clickCount: number;
}): number {
	const { delayMs, sampleRate, clickPeriodMs, clickCount } = opts;
	const delaySamples = Math.round(headDelaySeconds(delayMs) * sampleRate);
	const periodSamples = Math.round((clickPeriodMs / 1000) * sampleRate);
	const totalSamples = delaySamples + periodSamples * (clickCount - 1) + 1;
	const undelayed = new Float32Array(totalSamples);
	const delayed = new Float32Array(totalSamples + delaySamples);
	for (let i = 0; i < clickCount; i += 1) {
		undelayed[i * periodSamples] = 1;
		delayed[i * periodSamples + delaySamples] = 1;
	}
	let undelayedPeak = -1;
	let delayedPeak = -1;
	for (let i = 0; i < undelayed.length; i += 1) {
		if (undelayed[i] > 0 && undelayedPeak === -1) undelayedPeak = i;
	}
	for (let i = 0; i < delayed.length; i += 1) {
		if (delayed[i] > 0 && delayedPeak === -1) delayedPeak = i;
	}
	if (undelayedPeak === -1 || delayedPeak === -1) {
		throw new Error('click train did not produce detectable peaks');
	}
	return ((delayedPeak - undelayedPeak) / sampleRate) * 1000;
}

/** First-select CUE latency: Bluetooth, unnamed BT (WH-1000XM5), not speakers or the Mac jack. */
export function shouldCalibrateCueLatency(args: {
	previousCueId: string | null;
	nextCueId: string;
	label: string;
	alreadyCalibrated: boolean;
}): boolean {
	if (typeof args.nextCueId !== 'string' || args.nextCueId.trim() === '') {
		throw new TypeError('cue id for latency calibration must be a non-empty string');
	}
	if (typeof args.label !== 'string') throw new TypeError('cue label must be a string');
	if (args.previousCueId !== null && typeof args.previousCueId !== 'string') {
		throw new TypeError('previous cue id must be a string or null');
	}
	if (args.alreadyCalibrated) return false;
	const label = args.label.trim();
	if (label === '') return false;
	if (outputLooksLikeSpeakers(label)) return false;
	if (/external headphones/i.test(label)) return false;
	return true;
}

/** Skip the setSinkId dance when the live sink is selected again (OUT-change lag). */
export function sinkSelectIsNoop(
	currentId: string | null,
	nextId: string,
	live: boolean
): boolean {
	if (typeof nextId !== 'string' || nextId.trim() === '') {
		throw new TypeError('next sink id must be a non-empty string');
	}
	if (typeof live !== 'boolean') throw new TypeError('sink live flag must be boolean');
	if (currentId !== null && typeof currentId !== 'string') {
		throw new TypeError('current sink id must be a string or null');
	}
	return live && currentId === nextId;
}

export function cueOutputChangePlan(args: {
	currentCueId: string | null;
	nextCueId: string;
	currentActive: boolean;
}): { skip: boolean; applyMixBeforePlay: boolean; pinMasterInParallel: boolean } {
	if (sinkSelectIsNoop(args.currentCueId, args.nextCueId, args.currentActive)) {
		return { skip: true, applyMixBeforePlay: false, pinMasterInParallel: false };
	}
	return { skip: false, applyMixBeforePlay: true, pinMasterInParallel: true };
}

/** P0: the MAIN speaker line cannot be interrupted. Unplug, dead battery,
 * power-back-on, OS-default change, or devicechange of CUE never re-calls
 * AudioContext.setSinkId on a still-present pinned MASTER. */
export type PinnedSinkReapplyPlan = {
	applyMaster: boolean;
	applyCue: boolean;
	clearCue: boolean;
	restoreCue: boolean;
	keepTwoOutputs: boolean;
};

export function pinnedSinkReapplyPlan(args: {
	previousMasterId: string | null;
	nextMasterId: string | null;
	previousCueId: string | null;
	nextCueId: string | null;
	rememberedCueId: string | null;
	cueClearedByOperator: boolean;
	outputs: readonly HeadphoneOutput[];
}): PinnedSinkReapplyPlan {
	if (args.previousMasterId !== null && typeof args.previousMasterId !== 'string') {
		throw new TypeError('previous master id must be a string or null');
	}
	if (args.nextMasterId !== null && typeof args.nextMasterId !== 'string') {
		throw new TypeError('next master id must be a string or null');
	}
	if (args.previousCueId !== null && typeof args.previousCueId !== 'string') {
		throw new TypeError('previous cue id must be a string or null');
	}
	if (args.nextCueId !== null && typeof args.nextCueId !== 'string') {
		throw new TypeError('next cue id must be a string or null');
	}
	if (args.rememberedCueId !== null && typeof args.rememberedCueId !== 'string') {
		throw new TypeError('remembered cue id must be a string or null');
	}
	if (typeof args.cueClearedByOperator !== 'boolean') {
		throw new TypeError('cueClearedByOperator must be boolean');
	}
	const applyMaster =
		args.nextMasterId !== null && args.nextMasterId !== args.previousMasterId;
	const rememberedPresent =
		args.rememberedCueId !== null &&
		args.outputs.some((output) => output.id === args.rememberedCueId);
	if (args.cueClearedByOperator) {
		return {
			applyMaster,
			applyCue: false,
			clearCue: false,
			restoreCue: false,
			keepTwoOutputs: false
		};
	}
	if (args.previousCueId !== null && args.nextCueId === null) {
		return {
			applyMaster,
			applyCue: false,
			clearCue: true,
			restoreCue: false,
			keepTwoOutputs: true
		};
	}
	if (args.previousCueId === null && args.nextCueId === null && rememberedPresent) {
		return {
			applyMaster,
			applyCue: true,
			clearCue: false,
			restoreCue: true,
			keepTwoOutputs: true
		};
	}
	if (args.nextCueId !== null && args.nextCueId === args.previousCueId) {
		return {
			applyMaster,
			applyCue: false,
			clearCue: false,
			restoreCue: false,
			keepTwoOutputs: true
		};
	}
	if (args.nextCueId !== null && args.nextCueId !== args.previousCueId) {
		return {
			applyMaster,
			applyCue: true,
			clearCue: false,
			restoreCue: false,
			keepTwoOutputs: true
		};
	}
	return {
		applyMaster,
		applyCue: false,
		clearCue: false,
		restoreCue: false,
		keepTwoOutputs: false
	};
}

export type HeadphoneAcquisitionKind = 'chooser' | 'unlock_and_enumerate' | 'unsupported';

/** Chrome often has setSinkId + enumerateDevices but no selectAudioOutput.
 * In that case I/O unlocks output labels via a short-lived getUserMedia
 * (built-in mic preferred so a Bluetooth headset is not flipped to HFP). */
export function headphoneAcquisitionKind(caps: {
	enumerateDevices: boolean;
	setSinkId: boolean;
	selectAudioOutput: boolean;
}): HeadphoneAcquisitionKind {
	if (typeof caps.enumerateDevices !== 'boolean' || typeof caps.setSinkId !== 'boolean' || typeof caps.selectAudioOutput !== 'boolean') {
		throw new TypeError('headphone acquisition caps must be booleans');
	}
	if (!caps.enumerateDevices || !caps.setSinkId) return 'unsupported';
	if (caps.selectAudioOutput) return 'chooser';
	return 'unlock_and_enumerate';
}

export function outputLooksLikeHeadphones(label: string): boolean {
	if (typeof label !== 'string') throw new TypeError('output label must be a string');
	return /headphone|headset|airpods|bluetooth/i.test(label);
}

export function outputLooksLikeSpeakers(label: string): boolean {
	if (typeof label !== 'string') throw new TypeError('output label must be a string');
	return /speaker|built-in output|macbook/i.test(label) && !outputLooksLikeHeadphones(label);
}

export function inputLooksLikeHandsfree(label: string): boolean {
	if (typeof label !== 'string') throw new TypeError('input label must be a string');
	return /headphone|headset|hands-?free|airpods|bluetooth|\bhfp\b/i.test(label);
}

export function preferredMasterOutputDeviceId(
	outputs: readonly HeadphoneOutput[],
	excludeId: string | null
): string | null {
	if (excludeId !== null && typeof excludeId !== 'string') {
		throw new TypeError('excluded master output id must be a string or null');
	}
	const candidates = outputs.filter((output) => output.id !== excludeId && output.id.trim() !== '');
	const speakers = candidates.find((output) => outputLooksLikeSpeakers(output.label));
	if (speakers !== undefined) return speakers.id;
	const notPhones = candidates.find((output) => !outputLooksLikeHeadphones(output.label));
	return notPhones?.id ?? null;
}

export function dualSinkAssignment(args: {
	outputs: readonly HeadphoneOutput[];
	selectedCueId: string | null;
	selectedMasterId: string | null;
}): { masterId: string | null; cueId: string | null; autoPinnedMaster: boolean } {
	if (args.selectedCueId !== null && typeof args.selectedCueId !== 'string') {
		throw new TypeError('selected cue output id must be a string or null');
	}
	if (args.selectedMasterId !== null && typeof args.selectedMasterId !== 'string') {
		throw new TypeError('selected master output id must be a string or null');
	}
	const cueId =
		args.selectedCueId !== null && args.outputs.some((output) => output.id === args.selectedCueId)
			? args.selectedCueId
			: null;
	const masterStillPresent =
		args.selectedMasterId !== null && args.outputs.some((output) => output.id === args.selectedMasterId)
			? args.selectedMasterId
			: null;
	if (cueId !== null && masterStillPresent === null) {
		const preferred = preferredMasterOutputDeviceId(args.outputs, cueId);
		if (preferred !== null) {
			return { masterId: preferred, cueId, autoPinnedMaster: true };
		}
	}
	if (masterStillPresent === null) {
		const speakers = preferredMasterOutputDeviceId(args.outputs, cueId);
		if (speakers !== null) {
			const label = args.outputs.find((output) => output.id === speakers)?.label ?? '';
			if (outputLooksLikeSpeakers(label)) {
				return { masterId: speakers, cueId, autoPinnedMaster: true };
			}
		}
	}
	return { masterId: masterStillPresent, cueId, autoPinnedMaster: false };
}

export function audioContextSinkIdIsSupported(context: { setSinkId?: unknown }): boolean {
	return typeof context.setSinkId === 'function';
}

export function preferredAudioInputDeviceId(
	devices: readonly Pick<MediaDeviceInfo, 'kind' | 'deviceId' | 'label'>[]
): string | null {
	const inputs = devices.filter((device) => device.kind === 'audioinput' && device.deviceId.trim() !== '');
	const safe = inputs.filter((device) => !inputLooksLikeHandsfree(device.label));
	const builtIn = safe.find((device) => /macbook|built-in|internal microphone/i.test(device.label));
	if (builtIn !== undefined) return builtIn.deviceId;
	const named = safe.find((device) => device.label.trim() !== '' && device.deviceId !== 'default');
	if (named !== undefined) return named.deviceId;
	const namedDefault = safe.find((device) => device.deviceId === 'default');
	if (namedDefault !== undefined) return namedDefault.deviceId;
	return safe[0]?.deviceId ?? null;
}

export function unlockAudioInputConstraints(
	devices: readonly Pick<MediaDeviceInfo, 'kind' | 'deviceId' | 'label'>[],
	selectedInputDeviceId: string | null
): MediaTrackConstraints {
	const preferred = preferredAudioInputDeviceId(devices);
	const selected =
		selectedInputDeviceId !== null &&
		devices.some(
			(device) =>
				device.kind === 'audioinput' &&
				device.deviceId === selectedInputDeviceId &&
				!inputLooksLikeHandsfree(device.label)
		)
			? selectedInputDeviceId
			: null;
	const deviceId = selected ?? preferred;
	const audio: MediaTrackConstraints = {
		echoCancellation: false,
		noiseSuppression: false,
		autoGainControl: false
	};
	if (deviceId !== null) audio.deviceId = { ideal: deviceId };
	return audio;
}

function _probeAcquisitionKind(): HeadphoneAcquisitionKind {
	const enumerateDevices =
		typeof navigator !== 'undefined' &&
		navigator.mediaDevices !== undefined &&
		typeof navigator.mediaDevices.enumerateDevices === 'function';
	const setSinkId =
		typeof HTMLMediaElement !== 'undefined' && typeof HTMLMediaElement.prototype.setSinkId === 'function';
	const selectAudioOutput =
		typeof navigator !== 'undefined' &&
		navigator.mediaDevices !== undefined &&
		typeof (navigator.mediaDevices as Partial<_OutputSelectableMediaDevices>).selectAudioOutput === 'function';
	return headphoneAcquisitionKind({ enumerateDevices, setSinkId, selectAudioOutput });
}

export function setHeadphoneOutputMode(mode: unknown): void {
	assertHeadphoneOutputMode(mode);
	if (mode !== 'two_outputs') {
		_clearHeadphoneSelection();
	}
	mixerState.headphones.output_mode = mode;
	applyHeadphoneMix();
}

/** Insert the practice equal-power pair between master and mute so cue and
 * master share one destination clock. Does not create a second sink. */
export function wirePracticeBlendIntoMasterPath(
	masterGain: GainNode,
	muteGain: GainNode,
	nodes: HeadphoneNodes
): void {
	masterGain.connect(nodes.practiceMasterMix);
	nodes.practiceMasterMix.connect(muteGain);
	nodes.cueSum.connect(nodes.practiceCueMix);
	nodes.practiceCueMix.connect(muteGain);
}

/** Split-cable path: mono master on L, mono cue (plus MIX master) on R of the main output. */
export function wireSplitCableIntoMasterPath(
	masterGain: GainNode,
	muteGain: GainNode,
	nodes: HeadphoneNodes
): void {
	masterGain.connect(nodes.masterSplitter);
	nodes.masterSplitter.connect(nodes.masterLeftHalf, 0, 0);
	nodes.masterSplitter.connect(nodes.masterRightHalf, 1, 0);
	nodes.masterLeftHalf.connect(nodes.masterMono);
	nodes.masterRightHalf.connect(nodes.masterMono);
	nodes.masterMono.connect(nodes.splitLeftGain);
	nodes.splitLeftGain.connect(nodes.splitMerger, 0, 0);
	nodes.masterMono.connect(nodes.splitRightMasterGain);
	nodes.splitRightMasterGain.connect(nodes.splitMerger, 0, 1);
	nodes.cueSum.connect(nodes.cueSplitter);
	nodes.cueSplitter.connect(nodes.cueLeftHalf, 0, 0);
	nodes.cueSplitter.connect(nodes.cueRightHalf, 1, 0);
	nodes.cueLeftHalf.connect(nodes.cueMono);
	nodes.cueRightHalf.connect(nodes.cueMono);
	nodes.cueMono.connect(nodes.splitRightCueGain);
	nodes.splitRightCueGain.connect(nodes.splitMerger, 0, 1);
	nodes.splitMerger.connect(muteGain);
}

function _headphoneError(operation: string, error: unknown): Error {
	const message = error instanceof Error ? error.message : String(error);
	mixerState.headphones.error = `${operation}: ${message}`;
	return new Error(mixerState.headphones.error, { cause: error });
}

function _assertCurrentHeadphoneOperation(generation: number, nodes: HeadphoneNodes | null): void {
	assertHeadphoneOwnership(generation, _headphoneGeneration, nodes === null || nodes === _headphoneNodes);
}

function _requireHeadphoneDeviceApi(): MediaDevices {
	if (typeof navigator === 'undefined' || navigator.mediaDevices === undefined) {
		mixerState.headphones.supported = false;
		throw _headphoneError('headphone output unsupported', 'navigator.mediaDevices is unavailable');
	}
	if (typeof navigator.mediaDevices.enumerateDevices !== 'function') {
		mixerState.headphones.supported = false;
		throw _headphoneError('headphone output unsupported', 'enumerateDevices is unavailable');
	}
	if (typeof HTMLMediaElement === 'undefined' || typeof HTMLMediaElement.prototype.setSinkId !== 'function') {
		mixerState.headphones.supported = false;
		throw _headphoneError('headphone output unsupported', 'HTMLMediaElement.setSinkId is unavailable');
	}
	mixerState.headphones.supported = true;
	return navigator.mediaDevices;
}

interface _OutputSelectableMediaDevices extends MediaDevices {
	selectAudioOutput(): Promise<MediaDeviceInfo>;
}

function _requireHeadphoneOutputAcquisitionApi(): _OutputSelectableMediaDevices {
	const mediaDevices = _requireHeadphoneDeviceApi();
	if (typeof (mediaDevices as Partial<_OutputSelectableMediaDevices>).selectAudioOutput !== 'function') {
		throw _headphoneError(
			'headphone output acquisition unsupported',
			'navigator.mediaDevices.selectAudioOutput is unavailable'
		);
	}
	return mediaDevices as _OutputSelectableMediaDevices;
}

let _watchingDeviceChanges = false;
let _lastMonitorSource: MonitorSource | undefined;
let _headphoneLiveness: ReturnType<typeof installHeadphoneOutputLiveness> | null = null;

function _isAnyDeckPlaying(): boolean {
	return ([1, 2, 3, 4] as const).some((deck) => deckStates[deck].playing);
}

function _shouldMonitorHeadphoneOutput(): boolean {
	return (
		mixerState.headphones.active &&
		mixerState.headphones.output_mode === 'two_outputs' &&
		_isAnyDeckPlaying()
	);
}

function _selectedHeadphoneDeviceStillPresent(): boolean {
	const id = mixerState.headphones.selected_output_device_id;
	if (id === null) return false;
	return mixerState.headphones.outputs.some((output) => output.id === id);
}

function _stopHeadphoneLiveness(): void {
	_headphoneLiveness?.uninstall();
	_headphoneLiveness = null;
	const hp = _headphonesWithLiveness();
	hp.liveness_verdict = 'idle';
	hp.liveness_snapshot = null;
}

function _startHeadphoneLiveness(element: HTMLAudioElement): void {
	_stopHeadphoneLiveness();
	const hp = _headphonesWithLiveness();
	hp.liveness_verdict = 'idle';
	hp.liveness_snapshot = null;
	_headphoneLiveness = installHeadphoneOutputLiveness(
		element,
		{
			pushToast,
			recordPerfEvent: (kind, message, severity) => recordPerfEvent(kind, message, null, severity),
			setInterval: (fn, ms) => setInterval(fn, ms),
			clearInterval: (handle) => clearInterval(handle as ReturnType<typeof setInterval>),
			now: () => performance.now(),
			onSnapshot: (snapshot) => _publishHeadphoneLiveness(snapshot),
			watchDeviceChanges:
				typeof navigator !== 'undefined' && navigator.mediaDevices !== undefined
					? (handler) => {
							navigator.mediaDevices.addEventListener('devicechange', handler);
							return () => navigator.mediaDevices.removeEventListener('devicechange', handler);
						}
					: undefined
		},
		_shouldMonitorHeadphoneOutput,
		_selectedHeadphoneDeviceStillPresent
	);
}
/** Cue device ids that already filled HEAD DELAY this session. */
const _calibratedCueIds = new Set<string>();
/** One in-flight chirp at a time; a failed cal is not remembered so re-select retries. */
let _calibratingCueId: string | null = null;
/** Last CUE id while two_outputs was live, so a vanished sink can restore. */
let _rememberedCueId: string | null = null;
/** MAIN / practice click, not a device vanishing. Blocks auto-restore. */
let _cueClearedByOperator = false;

function _onHeadphoneDeviceChange(): void {
	void refreshHeadphoneOutputs(_lastMonitorSource).catch((error: unknown) => {
		mixerState.headphones.error = error instanceof Error ? error.message : String(error);
	});
}

function _watchHeadphoneDeviceChanges(mediaDevices: MediaDevices): void {
	if (_watchingDeviceChanges) return;
	mediaDevices.addEventListener('devicechange', _onHeadphoneDeviceChange);
	_watchingDeviceChanges = true;
}

function _unwatchHeadphoneDeviceChanges(): void {
	if (!_watchingDeviceChanges) return;
	if (typeof navigator !== 'undefined' && navigator.mediaDevices !== undefined) {
		navigator.mediaDevices.removeEventListener('devicechange', _onHeadphoneDeviceChange);
	}
	_watchingDeviceChanges = false;
}

function _clearHeadphoneSelection(): void {
	const currentElement = _headphoneNodes?.element;
	if (currentElement !== undefined) _detachHeadphoneElement(currentElement);
	_stopHeadphoneLiveness();
	mixerState.headphones.selected_output_device_id = null;
	mixerState.headphones.active = false;
	_cueClearedByOperator = true;
}

async function _unlockHeadphoneOutputLabels(mediaDevices: MediaDevices): Promise<void> {
	const listed = await withHeadphoneOperationTimeout('enumerateDevices', mediaDevices.enumerateDevices());
	const audio = unlockAudioInputConstraints(listed, mixerState.headphones.selected_input_device_id);
	const stream = await withHeadphoneOperationTimeout(
		'getUserMedia',
		mediaDevices.getUserMedia({ audio, video: false })
	);
	for (const track of stream.getTracks()) track.stop();
}

function _requireMasterSinkApi(context: AudioContext): AudioContext & { setSinkId: (sinkId: string) => Promise<void> } {
	if (!audioContextSinkIdIsSupported(context)) {
		throw new Error(
			'AudioContext.setSinkId is unavailable; master follows the OS default. Use Chrome for two-device cue.'
		);
	}
	return context as AudioContext & { setSinkId: (sinkId: string) => Promise<void> };
}

async function _applyMasterSink(deviceId: string, context: AudioContext): Promise<void> {
	const ctx = _requireMasterSinkApi(context);
	await withHeadphoneOperationTimeout('master setSinkId', ctx.setSinkId(deviceId));
	_outputContext = context;
}

async function _reapplyPinnedSinks(
	monitorSource: MonitorSource | undefined,
	plan: PinnedSinkReapplyPlan
): Promise<void> {
	if (monitorSource === undefined) return;
	if (plan.applyMaster) {
		const masterId = mixerState.headphones.selected_master_output_device_id;
		if (masterId !== null) {
			const { context } = monitorSource();
			await _applyMasterSink(masterId, context);
		}
	}
	if (plan.clearCue) {
		const currentElement = _headphoneNodes?.element;
		if (currentElement !== undefined) _detachHeadphoneElement(currentElement);
		_stopHeadphoneLiveness();
		mixerState.headphones.active = false;
		return;
	}
	if (!plan.applyCue && !plan.restoreCue) return;
	const cueId = mixerState.headphones.selected_output_device_id;
	if (cueId === null) return;
	const { context, masterGain } = monitorSource();
	const nodes = ensureHeadphoneGraph(context, masterGain);
	await withHeadphoneOperationTimeout('setSinkId', nodes.element.setSinkId(cueId));
	nodes.element.srcObject = nodes.destination.stream;
	await withHeadphoneOperationTimeout('play', nodes.element.play());
	mixerState.headphones.active = true;
	_startHeadphoneLiveness(nodes.element);
}

/** Build the monitor graph once and hand it back so the engine can wire the
 * per-channel cue gains into `cueSum`. Idempotent: the engine calls it on
 * every graph build and every headphone selection. */
export function ensureHeadphoneGraph(context: AudioContext, masterGain: GainNode): HeadphoneNodes {
	_outputContext = context;
	if (_headphoneNodes !== null) return _headphoneNodes;
	const cueSum = context.createGain();
	const masterMonitor = context.createGain();
	const cueMix = context.createGain();
	const masterMix = context.createGain();
	const level = context.createGain();
	const delay = context.createDelay(HEAD_DELAY_MAX_MS / 1000);
	const practiceCueMix = context.createGain();
	const practiceMasterMix = context.createGain();
	const masterSplitter = context.createChannelSplitter(2);
	const cueSplitter = context.createChannelSplitter(2);
	const masterLeftHalf = context.createGain();
	const masterRightHalf = context.createGain();
	const cueLeftHalf = context.createGain();
	const cueRightHalf = context.createGain();
	const masterMono = context.createGain();
	const cueMono = context.createGain();
	const splitLeftGain = context.createGain();
	const splitRightCueGain = context.createGain();
	const splitRightMasterGain = context.createGain();
	const splitMerger = context.createChannelMerger(2);
	masterLeftHalf.gain.value = 0.5;
	masterRightHalf.gain.value = 0.5;
	cueLeftHalf.gain.value = 0.5;
	cueRightHalf.gain.value = 0.5;
	splitLeftGain.gain.value = 0;
	splitRightCueGain.gain.value = 0;
	splitRightMasterGain.gain.value = 0;
	const destination = context.createMediaStreamDestination();
	const element = _createDetachedHeadphoneElement();
	cueSum.connect(cueMix);
	masterGain.connect(masterMonitor);
	masterMonitor.connect(masterMix);
	cueMix.connect(level);
	masterMix.connect(level);
	level.connect(delay);
	delay.connect(destination);
	_headphoneNodes = {
		cueSum,
		masterMonitor,
		cueMix,
		masterMix,
		level,
		delay,
		destination,
		element,
		practiceCueMix,
		practiceMasterMix,
		masterSplitter,
		cueSplitter,
		masterLeftHalf,
		masterRightHalf,
		cueLeftHalf,
		cueRightHalf,
		masterMono,
		cueMono,
		splitLeftGain,
		splitRightCueGain,
		splitRightMasterGain,
		splitMerger
	};
	applyHeadphoneMix();
	return _headphoneNodes;
}

function _createDetachedHeadphoneElement(): HTMLAudioElement {
	return new Audio();
}

function _detachHeadphoneElement(element: HTMLAudioElement): void {
	element.pause();
	element.srcObject = null;
}

function _disposeHeadphoneGraph(): void {
	_stopHeadphoneLiveness();
	const nodes = _headphoneNodes;
	_headphoneNodes = null;
	_outputContext = null;
	if (nodes === null) return;
	for (const node of [
		nodes.cueSum,
		nodes.masterMonitor,
		nodes.cueMix,
		nodes.masterMix,
		nodes.level,
		nodes.delay,
		nodes.destination,
		nodes.practiceCueMix,
		nodes.practiceMasterMix,
		nodes.masterSplitter,
		nodes.cueSplitter,
		nodes.masterLeftHalf,
		nodes.masterRightHalf,
		nodes.cueLeftHalf,
		nodes.cueRightHalf,
		nodes.masterMono,
		nodes.cueMono,
		nodes.splitLeftGain,
		nodes.splitRightCueGain,
		nodes.splitRightMasterGain,
		nodes.splitMerger
	]) {
		node.disconnect();
	}
	_detachHeadphoneElement(nodes.element);
	for (const track of nodes.destination.stream.getTracks()) track.stop();
}

/** Route teardown: retire every in-flight operation, then release the graph. */
export function disposeHeadphoneMonitor(): void {
	_headphoneGeneration += 1;
	_lastMonitorSource = undefined;
	_rememberedCueId = null;
	_cueClearedByOperator = false;
	_unwatchHeadphoneDeviceChanges();
	_disposeHeadphoneGraph();
}

export async function refreshHeadphoneOutputs(monitorSource?: MonitorSource): Promise<void> {
	if (monitorSource !== undefined) _lastMonitorSource = monitorSource;
	const generation = _headphoneGeneration;
	let devices: MediaDeviceInfo[];
	try {
		devices = await withHeadphoneOperationTimeout(
			'enumerateDevices',
			_requireHeadphoneDeviceApi().enumerateDevices()
		);
		_assertCurrentHeadphoneOperation(generation, null);
	} catch (error) {
		_assertCurrentHeadphoneOperation(generation, null);
		throw _headphoneError('headphone output enumeration failed', error);
	}
	mixerState.headphones.outputs = devices
		.filter((device) => device.kind === 'audiooutput')
		.map((device) => ({ id: device.deviceId, label: device.label }));
	mixerState.headphones.inputs = devices
		.filter((device) => device.kind === 'audioinput')
		.map((device) => ({ id: device.deviceId, label: device.label }));
	const previousMasterId = mixerState.headphones.selected_master_output_device_id;
	const previousCueId = mixerState.headphones.selected_output_device_id;
	if (previousCueId !== null) _rememberedCueId = previousCueId;
	const reconciled = reconcileHeadphoneOutputRefresh(
		mixerState.headphones.active,
		mixerState.headphones.selected_output_device_id,
		mixerState.headphones.outputs
	);
	mixerState.headphones.active = reconciled.active;
	mixerState.headphones.selected_output_device_id = reconciled.selected_output_device_id;
	const assignment = dualSinkAssignment({
		outputs: mixerState.headphones.outputs,
		selectedCueId: mixerState.headphones.selected_output_device_id,
		selectedMasterId: mixerState.headphones.selected_master_output_device_id
	});
	mixerState.headphones.selected_master_output_device_id = assignment.masterId;
	const plan = pinnedSinkReapplyPlan({
		previousMasterId,
		nextMasterId: assignment.masterId,
		previousCueId,
		nextCueId: mixerState.headphones.selected_output_device_id,
		rememberedCueId: _rememberedCueId,
		cueClearedByOperator: _cueClearedByOperator,
		outputs: mixerState.headphones.outputs
	});
	if (plan.keepTwoOutputs) {
		mixerState.headphones.output_mode = 'two_outputs';
	}
	if (plan.restoreCue && _rememberedCueId !== null) {
		mixerState.headphones.selected_output_device_id = _rememberedCueId;
		mixerState.headphones.active = true;
	}
	const inputStillPresent =
		mixerState.headphones.selected_input_device_id !== null &&
		mixerState.headphones.inputs.some((input) => input.id === mixerState.headphones.selected_input_device_id)
			? mixerState.headphones.selected_input_device_id
			: null;
	mixerState.headphones.selected_input_device_id =
		inputStillPresent ?? preferredAudioInputDeviceId(devices);
	mixerState.headphones.error = null;
	applyHeadphoneMix();
	_watchHeadphoneDeviceChanges(_requireHeadphoneDeviceApi());
	try {
		await _reapplyPinnedSinks(monitorSource ?? _lastMonitorSource, plan);
		_assertCurrentHeadphoneOperation(generation, null);
	} catch (error) {
		_assertCurrentHeadphoneOperation(generation, null);
		throw _headphoneError('headphone output enumeration failed', error);
	}
}

/** Must be called from a visible user gesture. Uses the browser output
 * chooser when `selectAudioOutput` exists; otherwise unlocks output labels
 * with a short-lived getUserMedia on the built-in mic (not a Bluetooth
 * headset mic) and enumerates sinks so the operator can pick headphones. */
export async function acquireHeadphoneOutput(monitorSource: MonitorSource): Promise<void> {
	const generation = _headphoneGeneration;
	try {
		const kind = _probeAcquisitionKind();
		if (kind === 'unsupported') {
			_requireHeadphoneDeviceApi();
		}
		if (kind === 'chooser') {
			const device = await withHeadphoneOperationTimeout(
				'selectAudioOutput',
				_requireHeadphoneOutputAcquisitionApi().selectAudioOutput()
			);
			_assertCurrentHeadphoneOperation(generation, null);
			mixerState.headphones.outputs = mergeHeadphoneOutput(mixerState.headphones.outputs, device);
			await selectHeadphoneOutput(device.deviceId, monitorSource);
			_assertCurrentHeadphoneOperation(generation, null);
			return;
		}
		const mediaDevices = _requireHeadphoneDeviceApi();
		await _unlockHeadphoneOutputLabels(mediaDevices);
		_assertCurrentHeadphoneOperation(generation, null);
		await refreshHeadphoneOutputs(monitorSource);
		_assertCurrentHeadphoneOperation(generation, null);
		const bluetooth = mixerState.headphones.outputs.filter((output) =>
			monitorLabelIsBluetooth(output.label)
		);
		if (bluetooth.length === 1 && mixerState.headphones.selected_output_device_id === null) {
			await selectHeadphoneOutput(bluetooth[0].id, monitorSource);
			_assertCurrentHeadphoneOperation(generation, null);
		}
	} catch (error) {
		_assertCurrentHeadphoneOperation(generation, null);
		throw _headphoneError('headphone output acquisition failed', error);
	}
}

async function _playCueLatencyTrain(
	nodes: HeadphoneNodes,
	samples: Float32Array,
	sampleRate: number
): Promise<void> {
	const ctx = nodes.level.context;
	const buffer = ctx.createBuffer(1, samples.length, sampleRate);
	buffer.copyToChannel(samples, 0);
	const src = ctx.createBufferSource();
	src.buffer = buffer;
	src.connect(nodes.destination);
	await new Promise<void>((resolve, reject) => {
		src.onended = () => resolve();
		src.onerror = () => reject(new Error('cue latency chirp failed to play'));
		try {
			src.start();
		} catch (error) {
			reject(error instanceof Error ? error : new Error(String(error)));
		}
	});
	src.disconnect();
}

function _sleepMs(ms: number): Promise<void> {
	return new Promise((resolve) => setTimeout(resolve, ms));
}

async function _recordCueLatencyCapture(
	stream: MediaStream,
	durationMs: number,
	sampleRate: number
): Promise<Float32Array> {
	const ctx = new AudioContext({ sampleRate });
	try {
		await ctx.resume();
		const frames = Math.ceil((durationMs / 1000) * ctx.sampleRate);
		const out = new Float32Array(frames);
		let offset = 0;
		const src = ctx.createMediaStreamSource(stream);
		const processor = ctx.createScriptProcessor(2048, 1, 1);
		const silent = ctx.createGain();
		silent.gain.value = 0;
		src.connect(processor);
		processor.connect(silent);
		silent.connect(ctx.destination);
		await new Promise<void>((resolve, reject) => {
			const timeoutId = setTimeout(
				() => reject(new Error(`cue latency record timed out after ${durationMs}ms`)),
				durationMs + 1500
			);
			processor.onaudioprocess = (event: AudioProcessingEvent) => {
				const input = event.inputBuffer.getChannelData(0);
				const n = Math.min(input.length, frames - offset);
				out.set(input.subarray(0, n), offset);
				offset += n;
				if (offset >= frames) {
					clearTimeout(timeoutId);
					processor.onaudioprocess = null;
					resolve();
				}
			};
		});
		processor.disconnect();
		src.disconnect();
		silent.disconnect();
		return out;
	} finally {
		await ctx.close();
	}
}

async function _calibrateFirstSelectCueLatency(
	deviceId: string,
	nodes: HeadphoneNodes
): Promise<void> {
	const mediaDevices = _requireHeadphoneDeviceApi();
	const listed = await withHeadphoneOperationTimeout('enumerateDevices', mediaDevices.enumerateDevices());
	const audio = unlockAudioInputConstraints(listed, mixerState.headphones.selected_input_device_id);
	const stream = await withHeadphoneOperationTimeout(
		'getUserMedia',
		mediaDevices.getUserMedia({ audio, video: false })
	);
	try {
		const sampleRate = nodes.level.context.sampleRate;
		const captureMs = cueLatencyCaptureMs();
		const measured = await measureCueLatencyMs({
			sampleRate,
			clickMs: CUE_LATENCY_CLICK_MS,
			periodMs: CUE_LATENCY_PERIOD_MS,
			clickCount: CUE_LATENCY_CLICK_COUNT,
			playAndRecord: async (reference) => {
				const recording = _recordCueLatencyCapture(stream, captureMs, sampleRate);
				await _sleepMs(CUE_LATENCY_PREROLL_MS);
				await _playCueLatencyTrain(nodes, reference, sampleRate);
				return recording;
			}
		});
		if (mixerState.headphones.selected_output_device_id !== deviceId) {
			return;
		}
		setHeadDelayMs(measured);
		_calibratedCueIds.add(deviceId);
	} finally {
		for (const track of stream.getTracks()) track.stop();
	}
}

function _maybeCalibrateCueLatency(deviceId: string, previousCueId: string | null, nodes: HeadphoneNodes): void {
	const label =
		mixerState.headphones.outputs.find((output) => output.id === deviceId)?.label ?? '';
	if (
		!shouldCalibrateCueLatency({
			previousCueId,
			nextCueId: deviceId,
			label,
			alreadyCalibrated: _calibratedCueIds.has(deviceId)
		})
	) {
		return;
	}
	if (_calibratingCueId === deviceId) return;
	if (_calibratingCueId !== null) return;
	_calibratingCueId = deviceId;
	void _calibrateFirstSelectCueLatency(deviceId, nodes)
		.catch((error: unknown) => {
			if (mixerState.headphones.selected_output_device_id !== deviceId) return;
			mixerState.headphones.error =
				error instanceof Error ? error.message : String(error);
		})
		.finally(() => {
			if (_calibratingCueId === deviceId) _calibratingCueId = null;
			const liveId = mixerState.headphones.selected_output_device_id;
			const liveNodes = _headphoneNodes;
			if (liveId !== null && liveId !== deviceId && liveNodes !== null) {
				_maybeCalibrateCueLatency(liveId, deviceId, liveNodes);
			}
		});
}

export async function selectHeadphoneOutput(
	deviceId: string,
	monitorSource: MonitorSource
): Promise<void> {
	const generation = _headphoneGeneration;
	const previousId = mixerState.headphones.selected_output_device_id;
	const previousActive = mixerState.headphones.active;
	const previousMode = mixerState.headphones.output_mode;
	let nodes: HeadphoneNodes | null = null;
	let candidate: HTMLAudioElement | null = null;
	try {
		_requireHeadphoneDeviceApi();
		assertHeadphoneOutputSelection(deviceId, mixerState.headphones.outputs);
		const plan = cueOutputChangePlan({
			currentCueId: previousId,
			nextCueId: deviceId,
			currentActive: previousActive
		});
		if (plan.skip) {
			const live = _headphoneNodes;
			if (live !== null) {
				if (mixerState.headphones.active) _startHeadphoneLiveness(live.element);
				_maybeCalibrateCueLatency(deviceId, previousId, live);
			}
			return;
		}
		mixerState.headphones.selected_output_device_id = deviceId;
		mixerState.headphones.output_mode = 'two_outputs';
		mixerState.headphones.error = null;
		const { context, masterGain } = monitorSource();
		nodes = ensureHeadphoneGraph(context, masterGain);
		if (plan.applyMixBeforePlay) applyHeadphoneMix();
		const nextElement = _createDetachedHeadphoneElement();
		candidate = nextElement;
		const liveNodes = nodes;
		const assignment = dualSinkAssignment({
			outputs: mixerState.headphones.outputs,
			selectedCueId: deviceId,
			selectedMasterId: mixerState.headphones.selected_master_output_device_id
		});
		const cueReady = (async () => {
			await withHeadphoneOperationTimeout('setSinkId', nextElement.setSinkId(deviceId));
			_assertCurrentHeadphoneOperation(generation, liveNodes);
			nextElement.srcObject = liveNodes.destination.stream;
			_assertCurrentHeadphoneOperation(generation, liveNodes);
			await withHeadphoneOperationTimeout('play', nextElement.play());
			_assertCurrentHeadphoneOperation(generation, liveNodes);
		})();
		const masterReady = (async () => {
			if (!plan.pinMasterInParallel) return;
			if (assignment.autoPinnedMaster && assignment.masterId !== null) {
				try {
					await selectMasterOutput(assignment.masterId, monitorSource);
					_assertCurrentHeadphoneOperation(generation, nodes);
				} catch (error) {
					mixerState.headphones.error =
						error instanceof Error ? error.message : String(error);
				}
			}
		})();
		await Promise.all([cueReady, masterReady]);
		const transaction = headphoneReselectionResult(true);
		const previous = nodes.element;
		if (!transaction.replaceCurrentElement || !transaction.publishSelection || !transaction.detachPrevious) {
			throw new Error('accepted headphone candidate did not produce a complete replacement transaction');
		}
		nodes.element = nextElement;
		mixerState.headphones.selected_output_device_id = deviceId;
		mixerState.headphones.active = true;
		mixerState.headphones.output_mode = 'two_outputs';
		_rememberedCueId = deviceId;
		_cueClearedByOperator = false;
		applyHeadphoneMix();
		_detachHeadphoneElement(previous);
		candidate = null;
		_lastMonitorSource = monitorSource;
		_startHeadphoneLiveness(nodes.element);
		_maybeCalibrateCueLatency(deviceId, previousId, nodes);
	} catch (error) {
		if (candidate !== null) _detachHeadphoneElement(candidate);
		mixerState.headphones.selected_output_device_id = previousId;
		mixerState.headphones.active = previousActive;
		mixerState.headphones.output_mode = previousMode;
		_assertCurrentHeadphoneOperation(generation, nodes);
		throw _headphoneError('headphone output selection failed', error);
	}
}

export async function selectMasterOutput(
	deviceId: string,
	monitorSource: MonitorSource
): Promise<void> {
	const generation = _headphoneGeneration;
	const previousId = mixerState.headphones.selected_master_output_device_id;
	try {
		_requireHeadphoneDeviceApi();
		assertHeadphoneOutputSelection(deviceId, mixerState.headphones.outputs);
		if (sinkSelectIsNoop(previousId, deviceId, previousId !== null)) {
			return;
		}
		mixerState.headphones.selected_master_output_device_id = deviceId;
		const { context, masterGain } = monitorSource();
		ensureHeadphoneGraph(context, masterGain);
		await _applyMasterSink(deviceId, context);
		_assertCurrentHeadphoneOperation(generation, null);
		_lastMonitorSource = monitorSource;
		mixerState.headphones.selected_master_output_device_id = deviceId;
		mixerState.headphones.error = null;
	} catch (error) {
		mixerState.headphones.selected_master_output_device_id = previousId;
		_assertCurrentHeadphoneOperation(generation, null);
		throw _headphoneError('master output selection failed', error);
	}
}

export async function selectAudioInput(deviceId: string): Promise<void> {
	const generation = _headphoneGeneration;
	try {
		_requireHeadphoneDeviceApi();
		assertHeadphoneOutputSelection(deviceId, mixerState.headphones.inputs);
		_assertCurrentHeadphoneOperation(generation, null);
		mixerState.headphones.selected_input_device_id = deviceId;
		const chosen = mixerState.headphones.inputs.find((input) => input.id === deviceId);
		if (chosen !== undefined && inputLooksLikeHandsfree(chosen.label)) {
			mixerState.headphones.error =
				'audio input looks like a headphone or handsfree mic; that can collapse Bluetooth to HFP and drop quality';
		} else {
			mixerState.headphones.error = null;
		}
	} catch (error) {
		_assertCurrentHeadphoneOperation(generation, null);
		throw _headphoneError('audio input selection failed', error);
	}
}
