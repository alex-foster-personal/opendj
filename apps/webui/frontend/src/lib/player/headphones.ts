/**
 * The headphone / cue monitor: its own sink, its own graph, its own state.
 *
 * Extracted from `audio-engine.svelte.ts` under convention D5 -- the feature
 * file keeps the call sites, a subsystem with a real boundary keeps its own
 * arithmetic AND its own state. This is the one part of the player that owns a
 * SECOND audio sink: a dedicated cue `AudioContext` pinned with
 * `AudioContext.setSinkId`, fed from the engine graph through an AudioWorklet
 * sender/receiver bridge (SharedArrayBuffer ring when cross-origin-isolated,
 * MessageChannel otherwise). Everything that follows from that -- the
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

import {
	HEAD_DELAY_MAX_MS,
	HEADPHONE_OPERATION_TIMEOUT_MS,
	HEADPHONE_OUTPUT_MODES,
	PARAM_SMOOTH_S,
	assertHeadDelayMs,
	assertHeadphoneAlignmentMode,
	assertHeadphoneOutputMode,
	assertMasterDelayMs,
	headDelaySeconds,
	masterDelaySeconds,
	type HeadphoneOutputMode
} from '$lib/player/constants';
import {
	CUE_BRIDGE_TARGET_FRAMES,
	CueBridgeRing
} from '$lib/player/cue-bridge-ring';
import { deriveAlignment } from '$lib/player/cue-align-policy';
import type { CueAlignBus } from '$lib/player/cue-align.svelte';
import type { CueBridgeWireResult } from '$lib/player/cue-bridge-wiring';
import { capturedSignalLevel } from '$lib/player/cue-latency';
import {
	IO_OPERATION_TIMEOUT_ERROR_NAME,
	ioDeviceAccessForFailure,
	ioDeviceAccessForListing,
	listNativeShellDevices,
	ioDeviceAccessRequestWasNotGranted,
	ioOperationTimedOut,
	listIoDevices,
	savedIoDeviceNotices,
	systemDefaultOutputOnly,
	type SavedIoDevice
} from '$lib/player/io-device-access';
import type { NativeCueSinkClient } from '$lib/player/cue-native-sink-client';
import {
	nativeCueSinkConfig,
	nativeDeviceId,
	nativeOutputsAsHeadphoneOutputs,
	type NativeCueSinkEvent
} from '$lib/player/cue-native-sink';
import {
	assertMasterCanSound,
	masterRepairedFromCue,
	outputCannotSound,
	physicalOutputId,
	sameDeviceSplitText,
	sameOutputDevice
} from '$lib/player/main-cue-collision';
import { loadMixerConfig, persistMixerConfig } from '$lib/player/mixer-config';
import { deckStates, mixerState } from '$lib/player/state.svelte';
import type { LivenessVerdict } from '$lib/rb/audio-output-liveness';
// Types only: the monitor is imported when the cue output starts (see
// _startHeadphoneLiveness), so it is not in the initial load of "/".
import type { HeadphoneOutputSnapshot, installCueBridgeHeadphoneLiveness } from '$lib/rb/headphone-output-liveness';
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

/** Legal `HeadphoneState.output_mode` values. Defined in player/constants.ts
 * (a leaf) since CUEOUT-14 so the alignment policy can validate a mode
 * without importing this graph; re-exported here for every existing importer. */
export { HEADPHONE_OUTPUT_MODES, assertHeadphoneOutputMode };
export type { HeadphoneOutputMode };

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

export { CUE_BRIDGE_TARGET_FRAMES };

/** Knowable cue-bridge latency parts for calibration (round 6 reads this). */
export interface CueBridgeLatencyReport {
	bridge_buffer_ms: number;
	main_base_latency_ms: number;
	main_output_latency_ms: number;
	cue_base_latency_ms: number;
	cue_output_latency_ms: number;
	total_ms: number;
}

export function cueBridgeLatencyReport(): CueBridgeLatencyReport | null {
	const main = _outputContext;
	const nodes = _headphoneNodes;
	if (main === null || nodes === null) return null;
	const native = nodes.nativeCue;
	if (native !== null) {
		// CUEOUT-22: the shell's ring holds the same target fill as the in-page
		// bridge, and the device's own latency is what CoreAudio reports for it.
		const bridge_buffer_ms = CueBridgeRing.bufferLatencyMs(main.sampleRate);
		const main_base_latency_ms = main.baseLatency * 1000;
		const main_output_latency_ms = main.outputLatency * 1000;
		const cue_output_latency_ms = native.opened?.device_latency_ms ?? 0;
		return {
			bridge_buffer_ms,
			main_base_latency_ms,
			main_output_latency_ms,
			cue_base_latency_ms: 0,
			cue_output_latency_ms,
			total_ms: bridge_buffer_ms + main_base_latency_ms + main_output_latency_ms + cue_output_latency_ms
		};
	}
	const cue = nodes.cueContext;
	if (cue === null) return null;
	const bridge_buffer_ms = CueBridgeRing.bufferLatencyMs(cue.sampleRate);
	const main_base_latency_ms = main.baseLatency * 1000;
	const main_output_latency_ms = main.outputLatency * 1000;
	const cue_base_latency_ms = cue.baseLatency * 1000;
	const cue_output_latency_ms = cue.outputLatency * 1000;
	return {
		bridge_buffer_ms,
		main_base_latency_ms,
		main_output_latency_ms,
		cue_base_latency_ms,
		cue_output_latency_ms,
		total_ms:
			bridge_buffer_ms +
			main_base_latency_ms +
			main_output_latency_ms +
			cue_base_latency_ms +
			cue_output_latency_ms
	};
}

/** The monitor graph: channel cue sum and a master tap, blended equal-power,
 * through one level gain and HEAD DELAY into the cue bridge input. Practice
 * mode also owns a second equal-power pair that sits in the main output path. */
export interface HeadphoneNodes {
	cueSum: GainNode;
	masterMonitor: GainNode;
	cueMix: GainNode;
	masterMix: GainNode;
	level: GainNode;
	delay: DelayNode;
	/** Chirp target and live cue feed into the bridge sender. */
	bridgeInput: GainNode;
	bridgeSender: AudioWorkletNode | null;
	cueContext: (AudioContext & { setSinkId?: (sinkId: string) => Promise<void> }) | null;
	bridgeReceiver: AudioWorkletNode | null;
	cueDeviceId: string | null;
	bridgeControl: Int32Array | null;
	/** CUEOUT-22: the Mac app's native cue output, in place of `cueContext`. */
	nativeCue: NativeCueSinkClient | null;
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
	/** Observers only: application-bus level does not prove physical output. */
	masterSignalAnalyser: AnalyserNode | null;
	cueSignalAnalyser: AnalyserNode | null;
	meterSilence: GainNode | null;
}

let _headphoneNodes: HeadphoneNodes | null = null;
/** True only while the engine has wired the monitor blend to channels 3/4 of
 * the same multichannel destination that carries master on 1/2. */
let _multichannelMonitorActive = false;
/** The engine AudioContext the master mix is pinned onto via setSinkId. */
let _outputContext: AudioContext | null = null;

/** The live monitor graph, for the calibration effects in cue-align-audio.ts.
 * Both are null until the headphone graph is built. */
export function liveCalibrationGraph(): { ctx: AudioContext | null; nodes: HeadphoneNodes | null } {
	return { ctx: _outputContext, nodes: _headphoneNodes };
}
let _cueBridgeReady: Promise<void> | null = null;
let _bridgeUnderrunCount = 0;
/** Bumped by every teardown. An operation that started under an older
 * generation refuses to publish rather than resurrect a disposed monitor. */
let _headphoneGeneration = 0;
let _signalMeterFrame: number | null = null;

function _clearSignal(name: 'master' | 'cue' | 'input', state: 'inactive' | 'unavailable'): void {
	const signal = mixerState.headphones.signals[name];
	signal.state = state;
	signal.rms = null;
	signal.peak = null;
	signal.measured_at = null;
}

function _publishSignal(name: 'master' | 'cue' | 'input', samples: Float32Array): void {
	const signal = mixerState.headphones.signals[name];
	const level = capturedSignalLevel(samples);
	signal.state = 'measured';
	signal.rms = level.rms;
	signal.peak = level.peak;
	signal.measured_at = new Date().toISOString();
}

/** The calibration capture's live mic level, published to the IO panel's
 * INPUT meter while a cue-alignment capture is pulling frames. */
export function publishCalibrationInputSignal(samples: Float32Array): void {
	_publishSignal('input', samples);
}

export function clearCalibrationInputSignal(): void {
	_clearSignal('input', 'inactive');
}

function _stopSignalMeters(): void {
	if (_signalMeterFrame !== null && typeof cancelAnimationFrame === 'function') {
		cancelAnimationFrame(_signalMeterFrame);
	}
	_signalMeterFrame = null;
	_clearSignal('master', 'unavailable');
	_clearSignal('cue', 'unavailable');
	_clearSignal('input', 'inactive');
}

/** Sample actual AnalyserNode buffers only while the browser provides an
 * animation clock.  There is deliberately no timer-made level estimate. */
function _startSignalMeters(nodes: HeadphoneNodes): void {
	_stopSignalMeters();
	if (
		nodes.masterSignalAnalyser === null ||
		nodes.cueSignalAnalyser === null ||
		typeof requestAnimationFrame !== 'function'
	) {
		return;
	}
	const master = new Float32Array(nodes.masterSignalAnalyser.fftSize);
	const cue = new Float32Array(nodes.cueSignalAnalyser.fftSize);
	let lastContextTime = -Infinity;
	const sample = () => {
		if (_headphoneNodes !== nodes) return;
		const contextTime = nodes.level.context.currentTime;
		if (
			nodes.level.context.state !== 'running' ||
			!Number.isFinite(contextTime) ||
			contextTime <= lastContextTime
		) {
			_clearSignal('master', 'inactive');
			_clearSignal('cue', 'inactive');
			_signalMeterFrame = requestAnimationFrame(sample);
			return;
		}
		lastContextTime = contextTime;
		nodes.masterSignalAnalyser?.getFloatTimeDomainData(master);
		nodes.cueSignalAnalyser?.getFloatTimeDomainData(cue);
		_publishSignal('master', master);
		_publishSignal('cue', cue);
		_signalMeterFrame = requestAnimationFrame(sample);
	};
	_signalMeterFrame = requestAnimationFrame(sample);
}

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

/** Pure targets for `applyHeadphoneMix` (issue #3982 regression tests). */
export interface HeadphoneMixTargets {
	monitorLive: boolean;
	monitorCueMix: number;
	monitorMasterMix: number;
	monitorLevel: number;
	practiceCueMix: number;
	practiceMasterMix: number;
	splitLeft: number;
	splitRightCue: number;
	splitRightMaster: number;
}

export function headphoneMixTargetGains(params: {
	output_mode: unknown;
	selected_output_device_id: string | null;
	mix: number;
	level: number;
	active: boolean;
	/** A multichannel interface is carrying the cue on its own channel pair. */
	multichannel_monitor_active: boolean;
}): HeadphoneMixTargets {
	const { mix, level, active, output_mode, selected_output_device_id, multichannel_monitor_active } =
		params;
	const gains = headphoneMixGains(mix);
	const monitorLive = multichannel_monitor_active || (output_mode === 'two_outputs' && active);
	// MAIN/practice always blends cue into the speaker path; a stale cue device id left
	// in state after leaving two_outputs must not silence channel CUE on speakers.
	const practiceDeviceId = output_mode === 'practice' ? null : selected_output_device_id;
	const practice = practiceMainGains(output_mode, practiceDeviceId, mix);
	const split = splitCableGains(output_mode, mix, level);
	return {
		monitorLive,
		monitorCueMix: monitorLive ? gains.cue : 0,
		monitorMasterMix: monitorLive ? gains.master : 0,
		monitorLevel: monitorLive ? level : 0,
		practiceCueMix: practiceCueWithGain(practice.cue, level),
		practiceMasterMix: practice.master,
		splitLeft: split.left,
		splitRightCue: split.rightCue,
		splitRightMaster: split.rightMaster
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
	return ['setSinkId', 'resume', 'publish'];
}

export function headphoneReselectionStages(): readonly string[] {
	return ['setSinkId', 'resume', 'publish'];
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
		? { replaceCurrentElement: true, publishSelection: true, detachPrevious: false }
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
		timeoutId = setTimeout(() => {
			const error = new Error(`headphone ${operation} timed out after ${timeoutMs}ms`);
			error.name = IO_OPERATION_TIMEOUT_ERROR_NAME;
			reject(error);
		}, timeoutMs);
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
	const targets = headphoneMixTargetGains({
		output_mode: mixerState.headphones.output_mode,
		selected_output_device_id: mixerState.headphones.selected_output_device_id,
		mix: mixerState.headphones.mix,
		level: mixerState.headphones.level,
		active: mixerState.headphones.active,
		multichannel_monitor_active: _multichannelMonitorActive
	});
	_setMonitorParam(nodes, nodes.cueMix.gain, targets.monitorCueMix);
	_setMonitorParam(nodes, nodes.masterMix.gain, targets.monitorMasterMix);
	_setMonitorParam(nodes, nodes.level.gain, targets.monitorLevel);
	_setMonitorParam(nodes, nodes.practiceCueMix.gain, targets.practiceCueMix);
	_setMonitorParam(nodes, nodes.practiceMasterMix.gain, targets.practiceMasterMix);
	_setMonitorParam(nodes, nodes.splitLeftGain.gain, targets.splitLeft);
	_setMonitorParam(nodes, nodes.splitRightCueGain.gain, targets.splitRightCue);
	_setMonitorParam(nodes, nodes.splitRightMasterGain.gain, targets.splitRightMaster);
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

/**
 * CUEOUT-14: move both delays in state and on the graph WITHOUT persisting.
 * Calibration applies its plan through this while it verifies, so a reload or
 * crash mid-verification boots with the last saved delays, never an unverified
 * one. Only a verified result goes through the persisting setters.
 */
export function applyUnsavedAlignmentDelays(delays: { head_delay_ms: unknown; master_delay_ms: unknown }): void {
	assertHeadDelayMs(delays.head_delay_ms);
	assertMasterDelayMs(delays.master_delay_ms);
	mixerState.headphones.head_delay_ms = delays.head_delay_ms;
	mixerState.headphones.master_delay_ms = delays.master_delay_ms;
	applyHeadphoneMix();
	_applyMasterDelay();
}

/** CUEOUT-14: the room delay line. ONE DelayNode, created here (so the
 * engine keeps no `createDelay` of its own) and inserted by `_ensureGraph`
 * as the LAST node before `ctx.destination`, after `_masterMuteGain`. The
 * monitor tap (`masterGain -> masterMonitor`) is upstream, so the phones
 * never pay it. Seeded from the persisted value so a reload plays the room
 * where it was left. */
const MASTER_DELAY_LINE_S = 2;
let _masterDelayNode: DelayNode | null = null;

function _applyMasterDelay(): void {
	const node = _masterDelayNode;
	if (node === null) return;
	node.delayTime.setValueAtTime(
		masterDelaySeconds(mixerState.headphones.master_delay_ms),
		node.context.currentTime
	);
}

export function createMasterDelayNode(context: AudioContext): DelayNode {
	const node = context.createDelay(MASTER_DELAY_LINE_S);
	node.delayTime.value = masterDelaySeconds(mixerState.headphones.master_delay_ms);
	_masterDelayNode = node;
	return node;
}

/** The room delay line's input: the verification chirp enters here, after master mute. */
export function masterDelayNode(): DelayNode | null {
	return _masterDelayNode;
}

export function setMasterDelayMs(value: unknown): void {
	assertMasterDelayMs(value);
	mixerState.headphones.master_delay_ms = value;
	persistMixerConfig({ master_delay_ms: value });
	_applyMasterDelay();
}

/** Switch how the last measured offset is split, and re-apply it from the
 * persisted `last_calibration` so no re-measurement is needed. */
export function setAlignmentMode(mode: unknown): void {
	assertHeadphoneAlignmentMode(mode);
	mixerState.headphones.alignment_mode = mode;
	persistMixerConfig({ alignment_mode: mode });
	const last = loadMixerConfig().last_calibration;
	if (last === null) return;
	const plan = deriveAlignment(mode, last.cue_latency_ms - last.master_latency_ms);
	setHeadDelayMs(plan.head_delay_ms);
	setMasterDelayMs(plan.master_delay_ms);
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

/** Headphone cluster alert line from the serialized headphone read model. */
export function headphoneLivenessAlertForState(
	state: import('$lib/rb/mixer-types').HeadphoneState
): string | null {
	return headphoneLivenessAlertText(state.liveness_verdict);
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

/** Sample-accurate lag for a static DelayNode delayTime (loopback acceptance
 * helper). `bus` picks the contract the delay is checked against: the cue
 * monitor line (0..500) or the CUEOUT-14 room line (0..1500). */
export function clickTrainLagMs(opts: {
	delayMs: number;
	sampleRate: number;
	bufferSize: number;
	clickPeriodMs: number;
	clickCount: number;
	bus?: CueAlignBus;
}): number {
	const { delayMs, sampleRate, clickPeriodMs, clickCount } = opts;
	const delaySeconds = (opts.bus ?? 'cue') === 'master' ? masterDelaySeconds(delayMs) : headDelaySeconds(delayMs);
	const delaySamples = Math.round(delaySeconds * sampleRate);
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

export type MicUnlockDecision = 'already_unlocked' | 'ask' | 'declined';

/** Whether pressing I/O should touch the microphone at all.
 *
 * The browser hides output device NAMES until a page has held some media
 * permission, so the labels are a consequence of the permission rather than
 * the point of it. That means the mic is only ever a means, and it is never
 * worth opening when it cannot help:
 *
 * - `granted`: the labels are already readable. Opening a stream would buy
 *   nothing and costs a recording indicator, plus the risk of collapsing a
 *   Bluetooth headset to its handsfree profile.
 * - `denied`: the operator has said no. Asking again is refused by the
 *   browser anyway, so the only honest move is to skip it and say what the
 *   consequence is.
 * - anything else (`prompt`, or no Permissions API to ask): the operator just
 *   pressed I/O, which is the consent, so ask.
 */
export function micUnlockDecision(permission: unknown): MicUnlockDecision {
	if (permission !== null && permission !== undefined && typeof permission !== 'string') {
		throw new TypeError('microphone permission state must be a string, null or undefined');
	}
	if (permission === 'granted') return 'already_unlocked';
	if (permission === 'denied') return 'declined';
	return 'ask';
}

/**
 * IOPIN-14: `micUnlockDecision`, corrected by what the listing actually shows.
 *
 * `granted` is the Permissions API's claim that names are readable. When the
 * listing just read is still withheld, the claim did not hold for this
 * document (observed Thu 1 Oct 2026 in Chromium 149: `granted`, and
 * enumerateDevices still returned its empty placeholders), and skipping the
 * stream would leave the operator pressing a grant button that does nothing.
 * The operator pressed that button, which is the consent, so ask.
 */
export function labelUnlockDecision(permission: unknown, namesWithheld: boolean): MicUnlockDecision {
	if (typeof namesWithheld !== 'boolean') throw new TypeError('namesWithheld must be a boolean');
	const decision = micUnlockDecision(permission);
	if (decision === 'already_unlocked' && namesWithheld) return 'ask';
	return decision;
}

/** What the operator is told when they have declined the microphone. It is not
 * a failure: two-output cue still works for any device they have already
 * picked, and practice and split-cable never needed a device at all. Names the
 * PERMISSION rather than the microphone, because the microphone is not off:
 * the page's access to it is, which is the thing the operator can change. */
export const MIC_DECLINED_NOTICE =
	'Microphone permission is off, so the browser hides audio device names. Device picking needs ' +
	'it once; practice and SPLIT do not need it at all.';

/** What the operator is told when the machine has NO microphone at all. Not a
 * refusal and not a failure: there is nothing to grant, so opening a stream
 * could never have unlocked the names, and there is no action that would
 * change that. Same supported state as a declined microphone, with the thing
 * that is absent named, because a pane that says only "acquisition failed"
 * sends the operator looking for a fault on a machine that has none. */
export const MIC_ABSENT_NOTICE =
	'Microphone unavailable, so the browser hides audio device names. Device picking needs one ' +
	'once; practice and SPLIT do not need it at all.';

/** True when a getUserMedia rejection means the machine HAS no such device,
 * as opposed to refusing to open one it has. The two DOMException names are
 * the current one and the legacy alias browsers still emit; any other
 * rejection is a real failure and must keep travelling. */
export function microphoneIsMissing(error: unknown): boolean {
	if (typeof error !== 'object' || error === null) return false;
	const name = (error as { name?: unknown }).name;
	return name === 'NotFoundError' || name === 'DevicesNotFoundError';
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

/** Transports a label guess must never auto-pick as the room: a virtual
 * device (BlackHole, a loopback) plays to nothing, and a display may have no
 * speakers. The operator can still choose either explicitly. */
const _AUTO_PICK_EXCLUDED_TRANSPORTS: ReadonlySet<string> = new Set(['virtual', 'display']);

export function preferredMasterOutputDeviceId(
	outputs: readonly HeadphoneOutput[],
	excludeId: string | null
): string | null {
	if (excludeId !== null && typeof excludeId !== 'string') {
		throw new TypeError('excluded master output id must be a string or null');
	}
	// CUEOUT-26: never the jack-muted speakers, and never the excluded (cue)
	// device under another name: the speaker/jack pair is one physical output.
	const candidates = outputs.filter(
		(output) =>
			output.id.trim() !== '' &&
			!outputCannotSound(outputs, output.id) &&
			(excludeId === null || !sameOutputDevice(outputs, output.id, excludeId))
	);
	const speakers = candidates.find((output) => outputLooksLikeSpeakers(output.label));
	if (speakers !== undefined) return speakers.id;
	const notPhones = candidates.find(
		(output) =>
			!outputLooksLikeHeadphones(output.label) &&
			(output.transport === undefined || !_AUTO_PICK_EXCLUDED_TRANSPORTS.has(output.transport))
	);
	return notPhones?.id ?? null;
}

export type DualSinkAssignment = {
	masterId: string | null;
	cueId: string | null;
	autoPinnedMaster: boolean;
	/** CUEOUT-26: MAIN and CUE are one physical output, so the cue runs as
	 * split cue (master L / cue R) on `masterId` instead of two outputs. */
	splitSameDevice?: true;
};

export function dualSinkAssignment(args: {
	outputs: readonly HeadphoneOutput[];
	selectedCueId: string | null;
	selectedMasterId: string | null;
	/** CUEOUT-22: where the room already plays (the macOS default output in
	 * the Mac app). Preferred over a label guess so auto-pinning MASTER keeps
	 * the room where it is instead of moving it to the laptop speakers. */
	currentRoomId?: string | null;
}): DualSinkAssignment {
	if (args.selectedCueId !== null && typeof args.selectedCueId !== 'string') {
		throw new TypeError('selected cue output id must be a string or null');
	}
	if (args.selectedMasterId !== null && typeof args.selectedMasterId !== 'string') {
		throw new TypeError('selected master output id must be a string or null');
	}
	const present = (id: string | null | undefined): id is string =>
		id !== undefined && id !== null && args.outputs.some((output) => output.id === id);
	const cueId = present(args.selectedCueId) ? args.selectedCueId : null;
	const splitOn = (deviceId: string, autoPinnedMaster: boolean): DualSinkAssignment => ({
		masterId: physicalOutputId(args.outputs, deviceId),
		cueId,
		autoPinnedMaster,
		splitSameDevice: true
	});
	// CUEOUT-26: a MAIN the headphone jack has muted is no MAIN: re-picked below.
	const usableMaster =
		present(args.selectedMasterId) && !outputCannotSound(args.outputs, args.selectedMasterId)
			? args.selectedMasterId
			: null;
	// MAIN on the CUE device is not two outputs: the room would get nothing
	// while every probe reads healthy (silver, Mon 5 Oct 2026). CUEOUT-26: it
	// is one output, so it runs as split cue on that device.
	if (cueId !== null && usableMaster !== null && sameOutputDevice(args.outputs, usableMaster, cueId)) {
		return splitOn(cueId, false);
	}
	const masterStillPresent = usableMaster;
	const room =
		present(args.currentRoomId) &&
		!outputCannotSound(args.outputs, args.currentRoomId) &&
		(cueId === null || !sameOutputDevice(args.outputs, args.currentRoomId, cueId))
			? args.currentRoomId
			: null;
	if (masterStillPresent === null && room !== null) {
		return { masterId: room, cueId, autoPinnedMaster: true };
	}
	if (cueId !== null && masterStillPresent === null) {
		const preferred = preferredMasterOutputDeviceId(args.outputs, cueId);
		if (preferred !== null) {
			return { masterId: preferred, cueId, autoPinnedMaster: true };
		}
		// No other output can sound (a MacBook with wired headphones in its
		// jack): the room and the cue are one output, so split cue on it.
		return splitOn(cueId, true);
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

/** `unknown` rather than a `{ setSinkId?: unknown }` shape: the real
 * `AudioContext`/`HTMLMediaElement` types in this project's DOM lib do not
 * declare the experimental `setSinkId`, so a structurally-typed parameter
 * makes every real caller a TS "weak type" mismatch (zero declared overlap).
 * `unknown` sidesteps that at the boundary; the runtime check is unchanged. */
export function audioContextSinkIdIsSupported(context: unknown): boolean {
	return (
		typeof context === 'object' &&
		context !== null &&
		typeof (context as { setSinkId?: unknown }).setSinkId === 'function'
	);
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
	const audio: MediaTrackConstraints = {
		echoCancellation: false,
		noiseSuppression: false,
		autoGainControl: false
	};
	// The operator's pick is `exact`: Chromium treats `ideal` as a hint and
	// opens the system default mic instead. The automatic fallback stays a hint.
	if (selected !== null) audio.deviceId = { exact: selected };
	else if (preferred !== null) audio.deviceId = { ideal: preferred };
	return audio;
}

function _probeAcquisitionKind(): HeadphoneAcquisitionKind {
	const enumerateDevices =
		typeof navigator !== 'undefined' &&
		navigator.mediaDevices !== undefined &&
		typeof navigator.mediaDevices.enumerateDevices === 'function';
	const setSinkId =
		typeof AudioContext !== 'undefined' && audioContextSinkIdIsSupported(AudioContext.prototype);
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
	// The operator picked this mode; it is no longer CUEOUT-26's automatic split.
	mixerState.headphones.split_reason = null;
	mixerState.headphones.output_mode = mode;
	applyHeadphoneMix();
}

/** Activate the same CUE/MASTER/LEVEL monitor blend for a discrete 3/4 output
 * pair. Wiring stays in the engine; this function only makes its gains live. */
export function setMultichannelMonitorActive(active: boolean): void {
	_multichannelMonitorActive = active;
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

const HEADPHONE_DIAGNOSTIC_WINDOW_MS = 60_000;
let _lastHeadphoneDiagnosticAt = -Infinity;

function _safeErrorClass(error: unknown): string {
	const candidate = error instanceof Error ? error.name : typeof error;
	return /^[A-Za-z0-9_.-]{1,64}$/.test(candidate) ? candidate : 'unknown';
}

/** One bounded, privacy-safe record for repeated audio-route failures.  Keep
 * device ids, browser messages and media paths out: those may be present in a
 * DOMException and are not needed to diagnose topology. */
function _recordHeadphoneDiagnostic(operation: string, error: unknown): void {
	const now = Date.now();
	if (now - _lastHeadphoneDiagnosticAt < HEADPHONE_DIAGNOSTIC_WINDOW_MS) return;
	_lastHeadphoneDiagnosticAt = now;
	const hp = mixerState.headphones;
	const fmt = (value: number | null) => (value === null ? 'na' : value.toFixed(3));
	recordPerfEvent(
		'headphone-diagnostic',
		[
			`operation=${operation.replace(/[^A-Za-z0-9_.-]/g, '_')}`,
			`error_class=${_safeErrorClass(error)}`,
			`mode=${hp.output_mode}`,
			`supported=${hp.supported}`,
			`cue_active=${hp.active}`,
			`master_route=${hp.routes.master.state}`,
			`cue_route=${hp.routes.cue.state}`,
			`master_rms=${fmt(hp.signals.master.rms)}`,
			`cue_rms=${fmt(hp.signals.cue.rms)}`,
			`input_rms=${fmt(hp.signals.input.rms)}`
		].join(' '),
		null,
		'error'
	);
}

/** Calibration/session code uses this for failures that occur above a device
 * operation. It retains the same rate, privacy and deferred Sentry policy. */
export function recordHeadphoneFailureDiagnostic(operation: string, error: unknown): void {
	_recordHeadphoneDiagnostic(operation, error);
}

function _headphoneError(operation: string, error: unknown): Error {
	const message = error instanceof Error ? error.message : String(error);
	mixerState.headphones.error = `${operation}: ${message}`;
	_recordHeadphoneDiagnostic(operation, error);
	return new Error(mixerState.headphones.error, { cause: error });
}

function _assertCurrentHeadphoneOperation(generation: number, nodes: HeadphoneNodes | null): void {
	assertHeadphoneOwnership(generation, _headphoneGeneration, nodes === null || nodes === _headphoneNodes);
}

/** CUEOUT-22: the installed Mac app's native cue output, created on first
 * use when the shell announced one. Null in Chrome and in any browser tab. */
let _nativeCueSink: NativeCueSinkClient | null = null;
/** The macOS default output at the last native listing: where the room plays. */
let _nativeRoomOutputId: string | null = null;
let _nativeEventsUnsubscribe: (() => void) | null = null;
/** Bumped by every dispose, so a client import still in flight across a
 * teardown never resurrects the sink the teardown just closed. */
let _nativeSinkEpoch = 0;

export function nativeCueSinkAvailable(): boolean {
	return nativeCueSinkConfig() !== null;
}

/** The live client, created on first use. Loaded lazily: the client and its
 * worker only matter in the Mac app, and a static import put them on the
 * initial load of "/" for every Chrome tab (library bundle budget). */
async function _nativeSink(): Promise<NativeCueSinkClient | null> {
	if (_nativeCueSink !== null && _nativeCueSink.disconnected === null) return _nativeCueSink;
	const config = nativeCueSinkConfig();
	if (config === null) return null;
	const epoch = _nativeSinkEpoch;
	const { NativeCueSinkClient } = await import('$lib/player/cue-native-sink-client');
	if (epoch !== _nativeSinkEpoch) return null;
	// A concurrent caller may have created the client while this one awaited.
	if (_nativeCueSink !== null && _nativeCueSink.disconnected === null) return _nativeCueSink;
	_nativeEventsUnsubscribe?.();
	_nativeCueSink?.dispose();
	const client = new NativeCueSinkClient(config);
	_nativeEventsUnsubscribe = client.onEvent(_onNativeCueEvent);
	_nativeCueSink = client;
	return client;
}

function _disposeNativeSink(): void {
	_nativeSinkEpoch += 1;
	_nativeEventsUnsubscribe?.();
	_nativeEventsUnsubscribe = null;
	_nativeCueSink?.dispose();
	_nativeCueSink = null;
	_nativeRoomOutputId = null;
}

function _onNativeCueEvent(event: NativeCueSinkEvent): void {
	switch (event.type) {
		case 'devices_changed':
			_onHeadphoneDeviceChange();
			return;
		case 'device_lost': {
			// The shell already stopped the device and never falls back to
			// another one, so the cue cannot land in the room. Say so; the
			// membership refresh that follows reconciles the selection and
			// restores the device if it comes back.
			_stopHeadphoneLiveness();
			mixerState.headphones.active = false;
			const message = 'headphone cue device disconnected; cue is off until it comes back';
			mixerState.headphones.error = message;
			recordPerfEvent('native-cue-device-lost', message, null, 'warn');
			pushToast(`NO HEADPHONE OUTPUT: ${message}`, 'error');
			_onHeadphoneDeviceChange();
			return;
		}
		case 'master_reasserted':
			// The shell put the macOS default back on the pinned MAIN. The page
			// did not call setMaster again, so record the pin here too.
			if (mixerState.headphones.selected_master_output_device_id !== null) {
				mixerState.headphones.routes.master = { state: 'selected', selected: true };
			}
			recordPerfEvent(
				'native-master-reasserted',
				`macOS moved the default output to ${event.from_uid ?? 'another device'}; MASTER was put back`,
				null,
				'info'
			);
			return;
		case 'master_pin_released': {
			// CUEOUT-26: the shell dropped a MAIN pin that could not sound or
			// that the system kept overriding. MAIN now follows the system
			// output; say why, then let a refresh re-plan (split cue when MAIN
			// and CUE are one output) instead of re-pinning the same device.
			mixerState.headphones.selected_master_output_device_id = null;
			mixerState.headphones.routes.master = { state: 'default', selected: false };
			mixerState.headphones.error = event.message;
			recordPerfEvent(`native-master-pin-released-${event.reason}`, event.message, null, 'warn');
			pushToast(event.message, 'error');
			_onHeadphoneDeviceChange();
			return;
		}
		case 'stats':
			return;
		case 'disconnected':
			if (mixerState.headphones.active && _headphoneNodes?.nativeCue != null) {
				mixerState.headphones.active = false;
				mixerState.headphones.error = `headphone cue output stopped: ${event.reason}`;
				recordPerfEvent('native-cue-disconnected', event.reason, null, 'error');
			}
			return;
	}
}

/** Output listing and selection need `navigator.mediaDevices` in a browser;
 * the Mac app lists outputs natively and needs nothing from it. */
function _requireOutputSelectionApi(): void {
	if (nativeCueSinkAvailable()) {
		mixerState.headphones.supported = true;
		return;
	}
	requireHeadphoneDeviceApi();
}

export function requireHeadphoneDeviceApi(): MediaDevices {
	if (typeof navigator === 'undefined' || navigator.mediaDevices === undefined) {
		mixerState.headphones.supported = false;
		throw _headphoneError('headphone output unsupported', 'navigator.mediaDevices is unavailable');
	}
	if (typeof navigator.mediaDevices.enumerateDevices !== 'function') {
		mixerState.headphones.supported = false;
		throw _headphoneError('headphone output unsupported', 'enumerateDevices is unavailable');
	}
	mixerState.headphones.supported = true;
	return navigator.mediaDevices;
}

function _requireCueBridgeApi(mainContext: AudioContext): void {
	if (!audioContextSinkIdIsSupported(mainContext)) {
		throw _headphoneError(
			'headphone output unsupported',
			'AudioContext.setSinkId is unavailable; cue monitor needs a pinnable AudioContext. Use Chrome for two-device cue.'
		);
	}
	if (typeof AudioWorkletNode === 'undefined' || typeof mainContext.audioWorklet?.addModule !== 'function') {
		throw _headphoneError(
			'headphone output unsupported',
			'AudioWorkletNode is unavailable; cue monitor needs an AudioWorklet bridge. Use Chrome for two-device cue.'
		);
	}
}

function _requireCueSinkApi(
	context: AudioContext
): AudioContext & { setSinkId: (sinkId: string) => Promise<void> } {
	_requireCueBridgeApi(context);
	return _requireMasterSinkApi(context);
}

async function _applyCueSink(deviceId: string, nodes: HeadphoneNodes): Promise<void> {
	const native = nodes.nativeCue;
	if (native !== null) {
		const sampleRate = _outputContext?.sampleRate;
		if (sampleRate === undefined) throw new Error('cue bridge has no main context');
		await withHeadphoneOperationTimeout('native cue open', native.open(deviceId, sampleRate));
		nodes.cueDeviceId = deviceId;
		return;
	}
	const ctx = nodes.cueContext;
	if (ctx === null) {
		throw new Error('cue bridge is not initialized');
	}
	await applyCueSinkTransaction(_requireCueSinkApi(ctx), deviceId, nodes);
}

type _CueSinkContext = Pick<AudioContext, 'state' | 'resume' | 'suspend'> & {
	setSinkId: (sinkId: string) => Promise<void>;
};

/**
 * CUEOUT-09: move the ONE shared cue context to `deviceId` as a transaction.
 * The context is live, so a half-applied change is audible: on any failure it
 * goes back to the device `holder` recorded. A setSinkId that timed out may
 * still land later, but calls on one context apply in order, so the restore
 * queued behind it wins. With nothing to restore to, or a restore that fails,
 * the context is suspended: silent beats playing on hardware the UI does not show.
 */
export async function applyCueSinkTransaction(
	ctx: _CueSinkContext,
	deviceId: string,
	holder: { cueDeviceId: string | null }
): Promise<void> {
	const previousId = holder.cueDeviceId;
	try {
		await withHeadphoneOperationTimeout('cue setSinkId', ctx.setSinkId(deviceId));
		holder.cueDeviceId = deviceId;
		if (ctx.state === 'suspended') {
			await withHeadphoneOperationTimeout('cue resume', ctx.resume());
		}
	} catch (error) {
		await _restoreCueSink(ctx, previousId, holder);
		throw error;
	}
}

async function _restoreCueSink(
	ctx: _CueSinkContext,
	previousId: string | null,
	holder: { cueDeviceId: string | null }
): Promise<void> {
	try {
		if (previousId === null) throw new Error('no previous cue device to restore');
		await withHeadphoneOperationTimeout('cue restore setSinkId', ctx.setSinkId(previousId));
		holder.cueDeviceId = previousId;
	} catch (restoreError) {
		holder.cueDeviceId = null;
		const detail = restoreError instanceof Error ? restoreError.message : String(restoreError);
		recordPerfEvent('cue-sink-restore-failed', `silencing cue: ${detail}`, null, 'error');
		await withHeadphoneOperationTimeout('cue silence', ctx.suspend());
	}
}

/**
 * A bridge worklet threw, so its node outputs silence for good while the cue
 * context keeps running. Say so, and tear the bridge down: a dead bridge left
 * attached is what `_ensureCueBridge` would reuse, so re-selecting the output
 * could never recover. Returns the message it published.
 */
export async function failCueBridge(
	side: 'sender' | 'receiver',
	nodes: Pick<
		HeadphoneNodes,
		'bridgeSender' | 'bridgeReceiver' | 'bridgeInput' | 'cueContext' | 'cueDeviceId' | 'bridgeControl'
	> &
		Partial<Pick<HeadphoneNodes, 'nativeCue'>>
): Promise<string> {
	const message = `cue bridge ${side} worklet failed, so the headphones are silent; re-select the headphone output to rebuild it`;
	recordPerfEvent('cue-bridge-processor-error', message, null, 'error');
	pushToast(`NO HEADPHONE OUTPUT: ${message}`, 'error');
	mixerState.headphones.error = message;
	mixerState.headphones.active = false;
	// Zero the live monitor gains now: a rebuilt bridge must not start on the stale mix.
	applyHeadphoneMix();
	_stopHeadphoneLiveness();
	// The sender has no outputs; its only edge is the incoming one, so cut that or
	// the main graph keeps every dead worklet alive until route teardown.
	if (nodes.bridgeSender !== null) nodes.bridgeInput.disconnect(nodes.bridgeSender);
	nodes.bridgeSender?.disconnect();
	nodes.bridgeReceiver?.disconnect();
	const cueContext = nodes.cueContext;
	const native = nodes.nativeCue ?? null;
	nodes.nativeCue = null;
	nodes.bridgeSender = null;
	nodes.bridgeReceiver = null;
	nodes.bridgeControl = null;
	nodes.cueContext = null;
	nodes.cueDeviceId = null;
	_cueBridgeReady = null;
	if (cueContext !== null) await _closeCueContext(cueContext);
	if (native !== null) {
		native.detachPcm();
		await native.close().catch((closeError: unknown) => {
			const detail = closeError instanceof Error ? closeError.message : String(closeError);
			recordPerfEvent('native-cue-close-failed', detail, null, 'warn');
		});
	}
	return message;
}

async function _closeCueContext(cueContext: AudioContext): Promise<void> {
	await cueContext.close().catch((closeError: unknown) => {
		const detail = closeError instanceof Error ? closeError.message : String(closeError);
		recordPerfEvent('cue-bridge-context-close-failed', detail, null, 'warn');
	});
}

async function _ensureCueBridge(mainContext: AudioContext, nodes: HeadphoneNodes): Promise<void> {
	if (nodes.bridgeSender !== null && (nodes.cueContext !== null || nodes.nativeCue !== null)) return;
	if (_cueBridgeReady !== null) {
		await _cueBridgeReady;
		return;
	}
	// Chrome never awaits here, so its start stays synchronous up to the cache below.
	const native = nativeCueSinkAvailable() ? await _nativeSink() : null;
	if (native !== null) {
		if (_cueBridgeReady !== null) {
			await _cueBridgeReady;
			return;
		}
		await _ensureNativeCueBridge(mainContext, nodes, native);
		return;
	}
	_requireCueBridgeApi(mainContext);
	// Declared first so the failure path can tell whether it is still the cached start.
	let ready: Promise<void> | null = null;
	ready = (async () => {
		const cueContext = new AudioContext({ sampleRate: mainContext.sampleRate });
		// Silent until `applyCueSinkTransaction` pins the headphone sink and resumes it:
		// a running context plays on the room default output in the meantime.
		await cueContext.suspend();
		let wired: CueBridgeWireResult;
		try {
			// On demand: the wiring and the worklet it names are only fetched once a cue
			// output is actually used, so neither is part of the initial load of "/".
			const { wireCueBridgeNodes } = await import('$lib/player/cue-bridge-wiring');
			wired = await wireCueBridgeNodes(mainContext, cueContext, {
				onUnderrun: () => {
					_bridgeUnderrunCount += 1;
				},
				onProcessorError: (side) => void failCueBridge(side, nodes)
			});
		} catch (error) {
			// Not cached: a failed start must not poison every later output selection,
			// and the half-built context must not outlive it.
			if (_cueBridgeReady === ready) _cueBridgeReady = null;
			await _closeCueContext(cueContext);
			throw error;
		}
		if (_headphoneNodes !== nodes) {
			// The route tore the graph down while this start was in flight. Publishing
			// onto the detached nodes would orphan a live AudioContext.
			wired.bridgeSender.disconnect();
			wired.bridgeReceiver.disconnect();
			await _closeCueContext(cueContext);
			throw new Error('cue bridge start finished after the headphone graph was disposed');
		}
		nodes.bridgeInput.connect(wired.bridgeSender);
		wired.bridgeReceiver.connect(cueContext.destination);
		nodes.bridgeSender = wired.bridgeSender;
		nodes.cueContext = cueContext;
		nodes.bridgeReceiver = wired.bridgeReceiver;
		nodes.bridgeControl = wired.bridgeControl;
	})();
	_cueBridgeReady = ready;
	await ready;
}

/** CUEOUT-22: the sender worklet feeds the shell's native output through the
 * relay worker; there is no receiver and no second AudioContext. */
async function _ensureNativeCueBridge(
	mainContext: AudioContext,
	nodes: HeadphoneNodes,
	native: NativeCueSinkClient
): Promise<void> {
	if (typeof AudioWorkletNode === 'undefined' || typeof mainContext.audioWorklet?.addModule !== 'function') {
		throw _headphoneError(
			'headphone output unsupported',
			'AudioWorkletNode is unavailable; the cue monitor needs an AudioWorklet bridge'
		);
	}
	let ready: Promise<void> | null = null;
	ready = (async () => {
		let wired: { bridgeSender: AudioWorkletNode; relayPort: MessagePort };
		try {
			const { wireNativeCueSender } = await import('$lib/player/cue-bridge-wiring');
			wired = await wireNativeCueSender(mainContext, () => void failCueBridge('sender', nodes));
		} catch (error) {
			if (_cueBridgeReady === ready) _cueBridgeReady = null;
			throw error;
		}
		if (_headphoneNodes !== nodes) {
			wired.bridgeSender.disconnect();
			wired.relayPort.close();
			throw new Error('cue bridge start finished after the headphone graph was disposed');
		}
		native.attachPcmPort(wired.relayPort);
		nodes.bridgeInput.connect(wired.bridgeSender);
		nodes.bridgeSender = wired.bridgeSender;
		nodes.nativeCue = native;
	})();
	_cueBridgeReady = ready;
	await ready;
}

interface _OutputSelectableMediaDevices extends MediaDevices {
	selectAudioOutput(): Promise<MediaDeviceInfo>;
}

function _requireHeadphoneOutputAcquisitionApi(): _OutputSelectableMediaDevices {
	const mediaDevices = requireHeadphoneDeviceApi();
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
let _headphoneLiveness: ReturnType<typeof installCueBridgeHeadphoneLiveness> | null = null;
/** Bumped by every stop, so a monitor whose module finishes loading after a stop is never installed. */
let _headphoneLivenessGeneration = 0;

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
	_headphoneLivenessGeneration += 1;
	_headphoneLiveness?.uninstall();
	_headphoneLiveness = null;
	const hp = _headphonesWithLiveness();
	hp.liveness_verdict = 'idle';
	hp.liveness_snapshot = null;
}

function _readCueBridgeHealth(): { bufferMs: number; underrunCount: number } {
	const nodes = _headphoneNodes;
	const cue = nodes?.cueContext;
	const bufferMs = cue === null || cue === undefined ? 0 : CueBridgeRing.bufferLatencyMs(cue.sampleRate);
	let underrunCount = _bridgeUnderrunCount;
	const control = nodes?.bridgeControl;
	if (control !== null && control !== undefined) {
		underrunCount = control[2] ?? 0;
	}
	return { bufferMs, underrunCount };
}

function _startHeadphoneLiveness(nodes: HeadphoneNodes): void {
	_stopHeadphoneLiveness();
	const cue = nodes.cueContext;
	if (cue === null) return;
	const hp = _headphonesWithLiveness();
	hp.liveness_verdict = 'idle';
	hp.liveness_snapshot = null;
	const generation = _headphoneLivenessGeneration;
	void import('$lib/rb/headphone-output-liveness')
		.then(({ installCueBridgeHeadphoneLiveness: install }) => {
			if (generation !== _headphoneLivenessGeneration) return;
			_headphoneLiveness = _installCueBridgeLiveness(install, cue);
		})
		.catch((error: unknown) => {
			const message = `headphone output monitor failed to load: ${error instanceof Error ? error.message : String(error)}`;
			recordPerfEvent('headphone-output-liveness-load-failed', message, null, 'error');
			pushToast(message, 'error');
		});
}

/** Suspend and resume the cue context so the browser re-opens its output device. */
async function _rebindCueContext(cue: AudioContext, reason: 'dead' | 'stalled'): Promise<void> {
	try {
		await withHeadphoneOperationTimeout('cue rebind suspend', cue.suspend());
		await withHeadphoneOperationTimeout('cue rebind resume', cue.resume());
		recordPerfEvent('headphone-output-rebind', `cue context re-bound after ${reason} output`, null, 'info');
	} catch (error) {
		const detail = error instanceof Error ? error.message : String(error);
		recordPerfEvent('headphone-output-rebind-failed', `${reason}: ${detail}`, null, 'error');
	}
}

function _installCueBridgeLiveness(
	install: typeof installCueBridgeHeadphoneLiveness,
	cue: AudioContext
): ReturnType<typeof installCueBridgeHeadphoneLiveness> {
	return install(
		cue,
		{
			pushToast,
			recordPerfEvent: (kind, message, severity) => recordPerfEvent(kind, message, null, severity),
			setInterval: (fn, ms) => setInterval(fn, ms),
			clearInterval: (handle) => clearInterval(handle as ReturnType<typeof setInterval>),
			now: () => performance.now(),
			onSnapshot: (snapshot) => _publishHeadphoneLiveness(snapshot),
			// The cue context's own recovery: the base detector's default would
			// suspend and resume the ROOM context and leave this one dead.
			rebindDeadOutput: () => void _rebindCueContext(cue, 'dead'),
			recoverOutput: () => void _rebindCueContext(cue, 'stalled'),
			watchDeviceChanges:
				typeof navigator !== 'undefined' && navigator.mediaDevices !== undefined
					? (handler) => {
							navigator.mediaDevices.addEventListener('devicechange', handler);
							return () => navigator.mediaDevices.removeEventListener('devicechange', handler);
						}
					: undefined
		},
		_shouldMonitorHeadphoneOutput,
		_selectedHeadphoneDeviceStillPresent,
		_readCueBridgeHealth
	);
}
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

/**
 * CUEOUT-26: MAIN and CUE are one physical output, so run the cue as split
 * cue on it (master L / cue R, `split_cable`) instead of two outputs: close
 * the separate cue sink, pin MAIN to that output, and say so. The saved CUE is
 * kept (this is not the operator clearing it), but it is not auto-restored as
 * a second sink while the split holds.
 */
async function _enterSameDeviceSplit(deviceId: string, monitorSource: MonitorSource | undefined): Promise<void> {
	if (monitorSource === undefined) {
		throw new Error(`split cue on ${deviceId} needs the audio graph to pin MAIN, and none is running yet`);
	}
	_stopHeadphoneLiveness();
	await silenceVanishedCueOutput(_headphoneNodes);
	mixerState.headphones.selected_output_device_id = null;
	mixerState.headphones.active = false;
	mixerState.headphones.routes.cue = { state: 'default', selected: false };
	_cueClearedByOperator = true;
	if (mixerState.headphones.selected_master_output_device_id !== deviceId) {
		const { context, masterGain } = monitorSource();
		ensureHeadphoneGraph(context, masterGain);
		await _applyMasterSink(deviceId, context);
		_lastMonitorSource = monitorSource;
	}
	mixerState.headphones.selected_master_output_device_id = deviceId;
	mixerState.headphones.output_mode = 'split_cable';
	mixerState.headphones.split_reason = 'same_device';
	applyHeadphoneMix();
	const label = mixerState.headphones.outputs.find((output) => output.id === deviceId)?.label ?? deviceId;
	const message = sameDeviceSplitText(label);
	recordPerfEvent('cue-split-same-device', message, null, 'warn');
	pushToast(message, 'info');
}

/** CUEOUT-26: the output an automatic split ran on is gone, so the split
 * ends: back to master-only two outputs, never a split on whatever remains. */
function _exitSameDeviceSplitIfItsOutputVanished(): string | null {
	const hp = mixerState.headphones;
	if (hp.split_reason !== 'same_device' || hp.output_mode !== 'split_cable') return null;
	const sharedId = hp.selected_master_output_device_id;
	if (sharedId !== null && hp.outputs.some((output) => output.id === sharedId)) return null;
	hp.output_mode = 'two_outputs';
	hp.split_reason = null;
	hp.selected_master_output_device_id = null;
	hp.routes.master = { state: 'default', selected: false };
	return `The output split cue was running on (${sharedId ?? 'unknown'}) is gone; split cue is off and MAIN follows the system output.`;
}

function _clearHeadphoneSelection(): void {
	_stopHeadphoneLiveness();
	mixerState.headphones.selected_output_device_id = null;
	mixerState.headphones.active = false;
	mixerState.headphones.routes.cue = { state: 'default', selected: false };
	_cueClearedByOperator = true;
	_rememberSavedOutput('cue', null);
}

/** The browser's own answer, or null when it will not be asked (Safari has no
 * `microphone` descriptor). Never throws: an unknown state means ask. */
async function _microphonePermissionState(): Promise<string | null> {
	try {
		if (typeof navigator === 'undefined' || navigator.permissions === undefined) return null;
		const status = await navigator.permissions.query({ name: 'microphone' as PermissionName });
		return status.state;
	} catch {
		return null;
	}
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
		mixerState.headphones.routes.master = {
			state: 'unsupported',
			selected: mixerState.headphones.selected_master_output_device_id !== null
		};
		throw new Error(
			'AudioContext.setSinkId is unavailable; master follows the OS default. Use Chrome for two-device cue.'
		);
	}
	return context as AudioContext & { setSinkId: (sinkId: string) => Promise<void> };
}

async function _applyMasterSink(deviceId: string, context: AudioContext): Promise<void> {
	const native = nativeCueSinkAvailable() ? await _nativeSink() : null;
	if (native !== null) {
		// The webview plays the room mix on the macOS default output, so MASTER
		// is pinned by making the device the default; the shell puts it back if
		// macOS moves the default (a headphone plug, a Bluetooth reconnect)
		// while the pin holds (CUEOUT-09 P0).
		await withHeadphoneOperationTimeout('native master select', native.setMaster(deviceId));
		_outputContext = context;
		mixerState.headphones.routes.master = { state: 'selected', selected: true };
		return;
	}
	const ctx = _requireMasterSinkApi(context);
	await withHeadphoneOperationTimeout('master setSinkId', ctx.setSinkId(deviceId));
	_outputContext = context;
	mixerState.headphones.routes.master = { state: 'selected', selected: true };
}

/**
 * The cue device went away. Suspend its context rather than leave it running:
 * Chrome may reroute a context whose sink vanished to the default output, which
 * would put the monitor mix in the room. A restored device resumes it only
 * after its sink lands (`applyCueSinkTransaction`).
 */
export async function silenceVanishedCueOutput(
	nodes: (Pick<HeadphoneNodes, 'cueContext'> & Partial<Pick<HeadphoneNodes, 'nativeCue'>>) | null
): Promise<void> {
	const native = nodes?.nativeCue ?? null;
	if (native !== null) {
		await withHeadphoneOperationTimeout('native cue close after device vanished', native.close());
		return;
	}
	const cueContext = nodes?.cueContext ?? null;
	if (cueContext === null || cueContext.state !== 'running') return;
	await withHeadphoneOperationTimeout('cue suspend after device vanished', cueContext.suspend());
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
		_stopHeadphoneLiveness();
		mixerState.headphones.active = false;
		await silenceVanishedCueOutput(_headphoneNodes);
		return;
	}
	if (!plan.applyCue && !plan.restoreCue) return;
	const cueId = mixerState.headphones.selected_output_device_id;
	if (cueId === null) return;
	const { context, masterGain } = monitorSource();
	const nodes = ensureHeadphoneGraph(context, masterGain);
	await _ensureCueBridge(context, nodes);
	await _applyCueSink(cueId, nodes);
	mixerState.headphones.active = true;
	_startHeadphoneLiveness(nodes);
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
	const canMeasureBuses = typeof context.createAnalyser === 'function';
	const masterSignalAnalyser = canMeasureBuses ? context.createAnalyser() : null;
	const cueSignalAnalyser = canMeasureBuses ? context.createAnalyser() : null;
	const meterSilence = canMeasureBuses ? context.createGain() : null;
	if (meterSilence !== null) meterSilence.gain.value = 0;
	masterLeftHalf.gain.value = 0.5;
	masterRightHalf.gain.value = 0.5;
	cueLeftHalf.gain.value = 0.5;
	cueRightHalf.gain.value = 0.5;
	splitLeftGain.gain.value = 0;
	splitRightCueGain.gain.value = 0;
	splitRightMasterGain.gain.value = 0;
	const bridgeInput = context.createGain();
	bridgeInput.gain.value = 1;
	cueSum.connect(cueMix);
	masterGain.connect(masterMonitor);
	masterMonitor.connect(masterMix);
	cueMix.connect(level);
	masterMix.connect(level);
	level.connect(delay);
	delay.connect(bridgeInput);
	// Keep analyser taps in the pulled graph through a silent sink.  They
	// observe the app buses and add no audible path or physical-output claim.
	if (masterSignalAnalyser !== null && cueSignalAnalyser !== null && meterSilence !== null) {
		masterMonitor.connect(masterSignalAnalyser);
		// HEADPHONE CUE follows MIX/LEVEL, not an upstream pre-volume branch.
		level.connect(cueSignalAnalyser);
		masterSignalAnalyser.connect(meterSilence);
		cueSignalAnalyser.connect(meterSilence);
		meterSilence.connect(context.destination);
	}
	_headphoneNodes = {
		cueSum,
		masterMonitor,
		cueMix,
		masterMix,
		level,
		delay,
		bridgeInput,
		bridgeSender: null,
		cueContext: null,
		bridgeReceiver: null,
		cueDeviceId: null,
		bridgeControl: null,
		nativeCue: null,
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
		splitMerger,
		masterSignalAnalyser,
		cueSignalAnalyser,
		meterSilence
	};
	applyHeadphoneMix();
	_startSignalMeters(_headphoneNodes);
	return _headphoneNodes;
}

/**
 * The cue bus, for a source that is NOT one of the four decks.
 *
 * `cueSum` is the single point every output mode already honours: `practice`
 * blends it into the main path (`wirePracticeBlendIntoMasterPath`),
 * `two_outputs` sends it only to the monitor sink, and `split_cable` puts it
 * on the right leg. So anything joined here inherits the whole CUEOUT routing
 * policy, including CUEOUT-09's rule that the MAIN speaker line is never
 * interrupted, instead of carrying a second copy of it.
 *
 * Returns null rather than building the graph: a caller that is not the engine
 * has no master bus to hand `ensureHeadphoneGraph`, and "no engine yet" is a
 * refusal the caller must report, never something to paper over.
 */
export function peekCueBus(): { context: AudioContext; cueSum: GainNode } | null {
	if (_outputContext === null || _headphoneNodes === null) return null;
	return { context: _outputContext, cueSum: _headphoneNodes.cueSum };
}

function _disposeHeadphoneGraph(): void {
	_stopHeadphoneLiveness();
	_stopSignalMeters();
	const nodes = _headphoneNodes;
	_headphoneNodes = null;
	_outputContext = null;
	_cueBridgeReady = null;
	_bridgeUnderrunCount = 0;
	if (nodes === null) return;
	nodes.bridgeSender?.disconnect();
	nodes.bridgeReceiver?.disconnect();
	nodes.bridgeInput.disconnect();
	if (nodes.cueContext !== null) {
		void nodes.cueContext.close();
	}
	if (nodes.nativeCue !== null) {
		// Dropping the socket is what closes the device on the shell side.
		_disposeNativeSink();
	}
	for (const node of [
		nodes.cueSum,
		nodes.masterMonitor,
		nodes.cueMix,
		nodes.masterMix,
		nodes.level,
		nodes.delay,
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
		nodes.splitMerger,
		...(nodes.masterSignalAnalyser === null ? [] : [nodes.masterSignalAnalyser]),
		...(nodes.cueSignalAnalyser === null ? [] : [nodes.cueSignalAnalyser]),
		...(nodes.meterSilence === null ? [] : [nodes.meterSilence])
	]) {
		node.disconnect();
	}
}

/** Route teardown: retire every in-flight operation, then release the graph. */
export function disposeHeadphoneMonitor(): void {
	_headphoneGeneration += 1;
	_lastMonitorSource = undefined;
	_rememberedCueId = null;
	_cueClearedByOperator = false;
	_unwatchHeadphoneDeviceChanges();
	releaseHeadphoneGraphOfFailedBuild();
}

/**
 * IOPIN-12: release the monitor graph of an engine graph build that THREW,
 * and nothing else. The build ran lazily inside a headphone or master output
 * selection (`monitorSource()` in its try block), so retiring in-flight
 * operations here, as route teardown does, would make that selection's own
 * catch report "stale headphone operation" in place of the build's real
 * error. Route state (remembered cue, device watch, monitor source) belongs
 * to the still-mounted route and is kept for the next build.
 */
export function releaseHeadphoneGraphOfFailedBuild(): void {
	_masterDelayNode = null;
	_multichannelMonitorActive = false;
	_disposeHeadphoneGraph();
	_disposeNativeSink();
}

/** Whether this browser or shell can pin an output at all. Asked of the
 * prototype so the answer does not need an engine context to exist yet. */
function _outputPinningIsSupported(): boolean {
	// CUEOUT-22: the Mac app pins both outputs natively, without setSinkId.
	if (nativeCueSinkAvailable()) return true;
	return typeof AudioContext !== 'undefined' && audioContextSinkIdIsSupported(AudioContext.prototype);
}

let _ioDeviceAccessAttempts = 0;

/** How many enumeration attempts have published an access state. A caller
 * that asks for an enumeration reads this before and after, so a request that
 * was rejected before it ever ran is told from one that ran and failed. */
export function ioDeviceAccessAttempts(): number {
	return _ioDeviceAccessAttempts;
}

/**
 * IOPIN-14: record that the device list could NOT be read, and why.
 *
 * The output list keeps what an earlier attempt read, or falls back to the
 * system default alone: the machine is still playing through something, and
 * a failed question is never drawn as "no devices".
 */
export function publishIoDeviceAccessFailure(
	status: 'api_missing' | 'enumeration_failed' | 'timeout',
	error: unknown
): void {
	_ioDeviceAccessAttempts += 1;
	mixerState.headphones.device_access = ioDeviceAccessForFailure({
		status,
		error,
		outputPinning: _outputPinningIsSupported()
	});
	if (mixerState.headphones.outputs.length === 0) {
		mixerState.headphones.outputs = systemDefaultOutputOnly();
	}
}

function _savedOutputs(): { master: SavedIoDevice | null; cue: SavedIoDevice | null } {
	return loadMixerConfig().saved_outputs;
}

/** Remember the operator's pick by id and name, so a later session can say
 * what became of it. `null` forgets the role. */
function _rememberSavedOutput(role: 'master' | 'cue', deviceId: string | null): void {
	const listed = deviceId === null ? undefined : mixerState.headphones.outputs.find((output) => output.id === deviceId);
	persistMixerConfig({
		saved_outputs: {
			..._savedOutputs(),
			[role]: listed === undefined ? null : { id: listed.id, label: listed.label }
		}
	});
}

export async function refreshHeadphoneOutputs(monitorSource?: MonitorSource): Promise<void> {
	if (monitorSource !== undefined) _lastMonitorSource = monitorSource;
	const generation = _headphoneGeneration;
	const native = nativeCueSinkAvailable() ? await _nativeSink() : null;
	let mediaDevices: MediaDevices | null = null;
	if (native === null) {
		try {
			mediaDevices = requireHeadphoneDeviceApi();
		} catch (error) {
			publishIoDeviceAccessFailure('api_missing', error);
			throw error;
		}
		// Watch before listing: a listing that fails today must still be retried
		// by the plug-in event that fixes it. The native sink pushes its own
		// device changes instead (`_onNativeCueEvent`).
		_watchHeadphoneDeviceChanges(mediaDevices);
	}
	let devices: MediaDeviceInfo[];
	let permission: string | null;
	let listing: ReturnType<typeof listIoDevices>;
	try {
		if (native !== null) {
			// CUEOUT-22: outputs come from the shell, already named, so no
			// microphone grant is needed to list them. Inputs (AUDIO IN, the
			// calibration mic) still come from the webview when it exposes them.
			const listed = await withHeadphoneOperationTimeout('native output listing', native.list());
			const room = listed.find((device) => device.is_default);
			_nativeRoomOutputId = room === undefined ? null : nativeDeviceId(room.uid);
			mixerState.headphones.supported = true;
			devices =
				typeof navigator !== 'undefined' && typeof navigator.mediaDevices?.enumerateDevices === 'function'
					? await withHeadphoneOperationTimeout('enumerateDevices', navigator.mediaDevices.enumerateDevices())
					: [];
			_assertCurrentHeadphoneOperation(generation, null);
			permission = await _microphonePermissionState();
			listing = listNativeShellDevices(nativeOutputsAsHeadphoneOutputs(listed), devices);
		} else {
			devices = await withHeadphoneOperationTimeout(
				'enumerateDevices',
				(mediaDevices as MediaDevices).enumerateDevices()
			);
			_assertCurrentHeadphoneOperation(generation, null);
			permission = await _microphonePermissionState();
			listing = listIoDevices(devices);
		}
		_assertCurrentHeadphoneOperation(generation, null);
	} catch (error) {
		_assertCurrentHeadphoneOperation(generation, null);
		publishIoDeviceAccessFailure(ioOperationTimedOut(error) ? 'timeout' : 'enumeration_failed', error);
		throw _headphoneError('headphone output enumeration failed', error);
	}
	mixerState.headphones.outputs = listing.outputs;
	mixerState.headphones.inputs = listing.inputs;
	const splitEnded = _exitSameDeviceSplitIfItsOutputVanished();
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
		selectedMasterId: mixerState.headphones.selected_master_output_device_id,
		currentRoomId: _nativeCueSink === null ? null : _nativeRoomOutputId
	});
	// CUEOUT-26: a split is applied below by _enterSameDeviceSplit, which pins
	// MAIN itself; leave the previous MAIN in place until that pin lands.
	if (assignment.splitSameDevice !== true) {
		mixerState.headphones.selected_master_output_device_id = assignment.masterId;
	}
	if (assignment.masterId === null) {
		mixerState.headphones.routes.master = { state: 'default', selected: false };
	}
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
	_ioDeviceAccessAttempts += 1;
	const access = ioDeviceAccessForListing({
		listing,
		permission,
		outputPinning: _outputPinningIsSupported()
	});
	// A withheld listing proves nothing about which devices are present, so
	// the saved picks are only compared against a real one.
	if (access.status === 'listed') {
		access.notices.push(
			...savedIoDeviceNotices({
				saved: _savedOutputs(),
				outputs: listing.outputs,
				selectedMasterId: mixerState.headphones.selected_master_output_device_id,
				selectedCueId: mixerState.headphones.selected_output_device_id
			})
		);
	}
	const repaired = masterRepairedFromCue({
		previousMasterId,
		cueId: assignment.cueId,
		nextMasterId: assignment.masterId,
		autoPinnedMaster: assignment.autoPinnedMaster
	});
	if (repaired !== null) {
		const label = (id: string): string => mixerState.headphones.outputs.find((output) => output.id === id)?.label ?? id;
		const message = `MAIN output was on the headphone CUE device ${label(repaired.from)}; moved MAIN to ${label(repaired.to)} so the room can hear the mix.`;
		access.notices.push(message);
		recordPerfEvent('main-output-repaired-from-cue', message, null, 'warn');
	}
	if (splitEnded !== null) {
		access.notices.push(splitEnded);
		recordPerfEvent('cue-split-same-device-ended', splitEnded, null, 'warn');
	}
	mixerState.headphones.device_access = access;
	applyHeadphoneMix();
	if (assignment.splitSameDevice === true && assignment.masterId !== null) {
		try {
			await _enterSameDeviceSplit(assignment.masterId, monitorSource ?? _lastMonitorSource);
			_assertCurrentHeadphoneOperation(generation, null);
		} catch (error) {
			_assertCurrentHeadphoneOperation(generation, null);
			throw _headphoneError('split cue on the shared output failed', error);
		}
		return;
	}
	try {
		await _reapplyPinnedSinks(monitorSource ?? _lastMonitorSource, plan);
		_assertCurrentHeadphoneOperation(generation, null);
		// Persist only once the repaired MAIN is actually pinned, so a saved
		// conflicting assignment cannot come back on the next launch.
		if (repaired !== null) _rememberSavedOutput('master', repaired.to);
	} catch (error) {
		_assertCurrentHeadphoneOperation(generation, null);
		throw _headphoneError('headphone output enumeration failed', error);
	}
}

/** Must be called from a visible user gesture. Uses the browser output
 * chooser when `selectAudioOutput` exists; otherwise enumerates sinks so the
 * operator can pick headphones, unlocking the names with a short-lived
 * getUserMedia on the built-in mic (not a Bluetooth headset mic) ONLY when
 * that would actually help -- see `micUnlockDecision`. A declined microphone
 * is a supported state, not a failure. */
export async function acquireHeadphoneOutput(monitorSource: MonitorSource): Promise<void> {
	const generation = _headphoneGeneration;
	try {
		if (nativeCueSinkAvailable()) {
			// The shell names every output itself, so outputs need no unlock.
			// The webview's inputs (AUDIO IN, the calibration mic) still do when
			// it withholds them, and this press is the operator's consent.
			await refreshHeadphoneOutputs(monitorSource);
			_assertCurrentHeadphoneOperation(generation, null);
			// A declined or absent microphone only leaves the inputs unnamed: the
			// outputs are already named, so cue picking below still runs.
			if (mixerState.headphones.device_access.status !== 'listed' && (await _unlockWebviewInputs(generation))) {
				await refreshHeadphoneOutputs(monitorSource);
				_assertCurrentHeadphoneOperation(generation, null);
			}
			await _autoSelectSoleBluetoothCue(monitorSource, generation);
			return;
		}
		const kind = _probeAcquisitionKind();
		if (kind === 'unsupported') {
			requireHeadphoneDeviceApi();
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
		const mediaDevices = requireHeadphoneDeviceApi();
		// List first: a hung or denied permission prompt must never leave the I/O
		// selects empty, so the unlabelled devices are always selectable and the
		// unlock failure is still raised (and shown) afterwards.
		await refreshHeadphoneOutputs(monitorSource);
		_assertCurrentHeadphoneOperation(generation, null);
		const namesWithheld = mixerState.headphones.device_access.status !== 'listed';
		const decision = labelUnlockDecision(await _microphonePermissionState(), namesWithheld);
		_assertCurrentHeadphoneOperation(generation, null);
		if (decision === 'declined') {
			// Not an error: nothing failed, the operator chose this. Saying so here
			// and returning leaves the unlabelled sinks selectable and never opens
			// a stream the browser would refuse anyway.
			mixerState.headphones.error = MIC_DECLINED_NOTICE;
			return;
		}
		if (decision === 'ask') {
			try {
				await _unlockHeadphoneOutputLabels(mediaDevices);
			} catch (error) {
				if (!microphoneIsMissing(error)) throw error;
				// A machine with no microphone is the `declined` case with the
				// choice taken out of the operator's hands: the sinks listed
				// above stay selectable, their names stay hidden, and nothing
				// here failed. Any other rejection still travels.
				_assertCurrentHeadphoneOperation(generation, null);
				mixerState.headphones.error = MIC_ABSENT_NOTICE;
				return;
			}
			_assertCurrentHeadphoneOperation(generation, null);
		}
		await refreshHeadphoneOutputs(monitorSource);
		_assertCurrentHeadphoneOperation(generation, null);
		await _autoSelectSoleBluetoothCue(monitorSource, generation);
	} catch (error) {
		_assertCurrentHeadphoneOperation(generation, null);
		throw _headphoneError('headphone output acquisition failed', error);
	}
}

/**
 * IOPIN-14, `request` mode only (the dev server): name the devices when I/O
 * opens, asking for the microphone grant the browser needs IF it has never
 * been asked.
 *
 * The request is made only when the Permissions API answers `prompt`. A
 * `granted` origin lists without a stream and so without a prompt, which is
 * what makes this a once-per-origin question; `denied`, and a browser with no
 * such permission to query, are left to the named state and its button.
 *
 * Unlike `acquireHeadphoneOutput` this never selects a device: opening I/O
 * changes no route (IOPIN-03). The stream is stopped as soon as it opens.
 */
export async function requestIoDeviceNames(monitorSource?: MonitorSource): Promise<void> {
	const generation = _headphoneGeneration;
	await refreshHeadphoneOutputs(monitorSource);
	_assertCurrentHeadphoneOperation(generation, null);
	if (mixerState.headphones.device_access.status !== 'permission_needed') return;
	const permission = await _microphonePermissionState();
	_assertCurrentHeadphoneOperation(generation, null);
	if (permission !== 'prompt') return;
	let requestError: unknown = null;
	try {
		await _unlockHeadphoneOutputLabels(requireHeadphoneDeviceApi());
	} catch (error) {
		requestError = error;
	}
	_assertCurrentHeadphoneOperation(generation, null);
	// List again whatever the answer was: a grant names the devices, a refusal
	// turns the state into `permission_denied` with how to re-allow.
	await refreshHeadphoneOutputs(monitorSource);
	_assertCurrentHeadphoneOperation(generation, null);
	if (requestError === null || ioDeviceAccessRequestWasNotGranted(requestError)) return;
	if (microphoneIsMissing(requestError)) {
		mixerState.headphones.error = MIC_ABSENT_NOTICE;
		return;
	}
	// Recorded on the panel and in the diagnostic ring, not thrown: the listing
	// above is the state, and this is why it is still withheld.
	_headphoneError('audio device access request failed', requestError);
}

/** CUEOUT-22: ask the webview for the microphone grant that names its inputs.
 * False when nothing was opened. A declined or absent microphone sets no
 * error: the panel's access state already says why AUDIO IN is unnamed, and
 * the MIC_* notices talk about hidden OUTPUT names, which the shell names. */
async function _unlockWebviewInputs(generation: number): Promise<boolean> {
	const decision = labelUnlockDecision(await _microphonePermissionState(), true);
	_assertCurrentHeadphoneOperation(generation, null);
	if (decision === 'declined') return false;
	try {
		await _unlockHeadphoneOutputLabels(requireHeadphoneDeviceApi());
	} catch (error) {
		if (!microphoneIsMissing(error)) throw error;
		_assertCurrentHeadphoneOperation(generation, null);
		return false;
	}
	_assertCurrentHeadphoneOperation(generation, null);
	return true;
}

async function _autoSelectSoleBluetoothCue(monitorSource: MonitorSource, generation: number): Promise<void> {
	const bluetooth = mixerState.headphones.outputs.filter((output) => monitorLabelIsBluetooth(output.label));
	if (bluetooth.length === 1 && mixerState.headphones.selected_output_device_id === null) {
		await selectHeadphoneOutput(bluetooth[0].id, monitorSource);
		_assertCurrentHeadphoneOperation(generation, null);
	}
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
	try {
		_requireOutputSelectionApi();
		assertHeadphoneOutputSelection(deviceId, mixerState.headphones.outputs);
		const plan = cueOutputChangePlan({
			currentCueId: previousId,
			nextCueId: deviceId,
			currentActive: previousActive
		});
		if (plan.skip) {
			const live = _headphoneNodes;
			if (live !== null && mixerState.headphones.active) _startHeadphoneLiveness(live);
			return;
		}
		const sameDevice = dualSinkAssignment({
			outputs: mixerState.headphones.outputs,
			selectedCueId: deviceId,
			selectedMasterId: mixerState.headphones.selected_master_output_device_id,
			currentRoomId: _nativeCueSink === null ? null : _nativeRoomOutputId
		});
		if (sameDevice.splitSameDevice === true && sameDevice.masterId !== null) {
			// CUEOUT-26: this CUE is the room's own output, so it cannot be a
			// second sink: split cue on it, and remember it as the operator's CUE.
			await _enterSameDeviceSplit(sameDevice.masterId, monitorSource);
			_assertCurrentHeadphoneOperation(generation, null);
			_rememberedCueId = deviceId;
			_rememberSavedOutput('cue', deviceId);
			_lastMonitorSource = monitorSource;
			return;
		}
		mixerState.headphones.selected_output_device_id = deviceId;
		mixerState.headphones.output_mode = 'two_outputs';
		mixerState.headphones.split_reason = null;
		mixerState.headphones.error = null;
		const { context, masterGain } = monitorSource();
		nodes = ensureHeadphoneGraph(context, masterGain);
		await _ensureCueBridge(context, nodes);
		if (plan.applyMixBeforePlay) applyHeadphoneMix();
		const liveNodes = nodes;
		const assignment = dualSinkAssignment({
			outputs: mixerState.headphones.outputs,
			selectedCueId: deviceId,
			selectedMasterId: mixerState.headphones.selected_master_output_device_id,
			currentRoomId: _nativeCueSink === null ? null : _nativeRoomOutputId
		});
		const cueReady = (async () => {
			await _applyCueSink(deviceId, liveNodes);
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
		if (!transaction.replaceCurrentElement || !transaction.publishSelection) {
			throw new Error('accepted headphone sink pin did not produce a complete publish transaction');
		}
		mixerState.headphones.selected_output_device_id = deviceId;
		mixerState.headphones.active = true;
		mixerState.headphones.routes.cue = { state: 'selected', selected: true };
		mixerState.headphones.output_mode = 'two_outputs';
		_rememberedCueId = deviceId;
		_cueClearedByOperator = false;
		_rememberSavedOutput('cue', deviceId);
		applyHeadphoneMix();
		_lastMonitorSource = monitorSource;
		_startHeadphoneLiveness(nodes);
	} catch (error) {
		mixerState.headphones.selected_output_device_id = previousId;
		mixerState.headphones.active = previousActive;
		mixerState.headphones.output_mode = previousMode;
		mixerState.headphones.routes.cue = { state: 'failed', selected: previousId !== null };
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
	// CUEOUT-26: refused before the try, so a refusal never marks the live
	// MAIN route failed; the current MAIN keeps playing.
	assertMasterCanSound(deviceId, mixerState.headphones.outputs);
	const cueId = mixerState.headphones.selected_output_device_id;
	if (
		mixerState.headphones.output_mode === 'two_outputs' &&
		cueId !== null &&
		sameOutputDevice(mixerState.headphones.outputs, deviceId, cueId)
	) {
		// CUEOUT-25 refused this; CUEOUT-26: MAIN on the CUE's own output is
		// split cue on that output (master L / cue R), never a silent room.
		await _enterSameDeviceSplit(physicalOutputId(mixerState.headphones.outputs, cueId), monitorSource);
		_assertCurrentHeadphoneOperation(generation, null);
		_rememberSavedOutput('master', deviceId);
		return;
	}
	const leavingSplitFrom =
		mixerState.headphones.split_reason === 'same_device' &&
		previousId !== null &&
		!sameOutputDevice(mixerState.headphones.outputs, deviceId, previousId)
			? previousId
			: null;
	try {
		_requireOutputSelectionApi();
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
		_rememberSavedOutput('master', deviceId);
	} catch (error) {
		mixerState.headphones.selected_master_output_device_id = previousId;
		if (mixerState.headphones.routes.master.state !== 'unsupported') {
			mixerState.headphones.routes.master = { state: 'failed', selected: previousId !== null };
		}
		_assertCurrentHeadphoneOperation(generation, null);
		throw _headphoneError('master output selection failed', error);
	}
	if (leavingSplitFrom !== null) {
		// A MAIN on a genuinely different output ends the automatic split: the
		// shared output goes back to being the separate HEADPHONE CUE sink.
		mixerState.headphones.split_reason = null;
		mixerState.headphones.output_mode = 'two_outputs';
		_cueClearedByOperator = false;
		await selectHeadphoneOutput(leavingSplitFrom, monitorSource);
	}
}

export async function selectAudioInput(deviceId: string): Promise<void> {
	const generation = _headphoneGeneration;
	try {
		requireHeadphoneDeviceApi();
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
