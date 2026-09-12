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
import { persistMixerConfig } from '$lib/player/mixer-config';
import { mixerState } from '$lib/player/state.svelte';

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

/** Main-output cue/master gains. Practice with no monitor selected reuses
 * `headphoneMixGains`; any selected monitor (or `two_outputs`) is master-only. */
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
	return headphoneMixGains(mix);
}

/** Split-cable L/R gains. Left is master-only mono; right carries cue and MIX master. */
export function splitCableGains(
	mode: unknown,
	mix: number
): { left: number; rightCue: number; rightMaster: number } {
	assertHeadphoneOutputMode(mode);
	if (mode !== 'split_cable') {
		return { left: 0, rightCue: 0, rightMaster: 0 };
	}
	const { cue, master } = headphoneMixGains(mix);
	return { left: 1, rightCue: cue, rightMaster: master };
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
	const gains = headphoneMixGains(mix);
	_setMonitorParam(nodes, nodes.cueMix.gain, gains.cue);
	_setMonitorParam(nodes, nodes.masterMix.gain, gains.master);
	_setMonitorParam(nodes, nodes.level.gain, mixerState.headphones.level);
	const practice = practiceMainGains(
		mixerState.headphones.output_mode,
		mixerState.headphones.selected_output_device_id,
		mix
	);
	_setMonitorParam(nodes, nodes.practiceCueMix.gain, practice.cue);
	_setMonitorParam(nodes, nodes.practiceMasterMix.gain, practice.master);
	const split = splitCableGains(mixerState.headphones.output_mode, mix);
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

export function setHeadphoneOutputMode(mode: unknown): void {
	assertHeadphoneOutputMode(mode);
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

/** Build the monitor graph once and hand it back so the engine can wire the
 * per-channel cue gains into `cueSum`. Idempotent: the engine calls it on
 * every graph build and every headphone selection. */
export function ensureHeadphoneGraph(context: AudioContext, masterGain: GainNode): HeadphoneNodes {
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
	const nodes = _headphoneNodes;
	_headphoneNodes = null;
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
	_disposeHeadphoneGraph();
}

export async function refreshHeadphoneOutputs(): Promise<void> {
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
	const reconciled = reconcileHeadphoneOutputRefresh(
		mixerState.headphones.active,
		mixerState.headphones.selected_output_device_id,
		mixerState.headphones.outputs
	);
	if (reconciled.selected_output_device_id === null && mixerState.headphones.selected_output_device_id !== null) {
		const currentElement = _headphoneNodes?.element;
		if (currentElement !== undefined) _detachHeadphoneElement(currentElement);
	}
	mixerState.headphones.active = reconciled.active;
	mixerState.headphones.selected_output_device_id = reconciled.selected_output_device_id;
	if (reconciled.selected_output_device_id === null && mixerState.headphones.output_mode === 'two_outputs') {
		mixerState.headphones.output_mode = 'practice';
	}
	mixerState.headphones.error = null;
	applyHeadphoneMix();
}

/** Must be called from a visible user gesture so the browser can open its
 * output chooser. This never requests microphone capture. */
export async function acquireHeadphoneOutput(monitorSource: MonitorSource): Promise<void> {
	const generation = _headphoneGeneration;
	try {
		const device = await withHeadphoneOperationTimeout(
			'selectAudioOutput',
			_requireHeadphoneOutputAcquisitionApi().selectAudioOutput()
		);
		_assertCurrentHeadphoneOperation(generation, null);
		mixerState.headphones.outputs = mergeHeadphoneOutput(mixerState.headphones.outputs, device);
		await selectHeadphoneOutput(device.deviceId, monitorSource);
		_assertCurrentHeadphoneOperation(generation, null);
	} catch (error) {
		_assertCurrentHeadphoneOperation(generation, null);
		throw _headphoneError('headphone output acquisition failed', error);
	}
}

export async function selectHeadphoneOutput(
	deviceId: string,
	monitorSource: MonitorSource
): Promise<void> {
	const generation = _headphoneGeneration;
	let nodes: HeadphoneNodes | null = null;
	let candidate: HTMLAudioElement | null = null;
	try {
		_requireHeadphoneDeviceApi();
		assertHeadphoneOutputSelection(deviceId, mixerState.headphones.outputs);
		const { context, masterGain } = monitorSource();
		nodes = ensureHeadphoneGraph(context, masterGain);
		candidate = _createDetachedHeadphoneElement();
		await withHeadphoneOperationTimeout('setSinkId', candidate.setSinkId(deviceId));
		_assertCurrentHeadphoneOperation(generation, nodes);
		candidate.srcObject = nodes.destination.stream;
		_assertCurrentHeadphoneOperation(generation, nodes);
		await withHeadphoneOperationTimeout('play', candidate.play());
		_assertCurrentHeadphoneOperation(generation, nodes);
		const transaction = headphoneReselectionResult(true);
		const previous = nodes.element;
		if (!transaction.replaceCurrentElement || !transaction.publishSelection || !transaction.detachPrevious) {
			throw new Error('accepted headphone candidate did not produce a complete replacement transaction');
		}
		nodes.element = candidate;
		mixerState.headphones.selected_output_device_id = deviceId;
		mixerState.headphones.active = true;
		mixerState.headphones.output_mode = 'two_outputs';
		mixerState.headphones.error = null;
		applyHeadphoneMix();
		_detachHeadphoneElement(previous);
		candidate = null;
	} catch (error) {
		if (candidate !== null) _detachHeadphoneElement(candidate);
		_assertCurrentHeadphoneOperation(generation, nodes);
		throw _headphoneError('headphone output selection failed', error);
	}
}
