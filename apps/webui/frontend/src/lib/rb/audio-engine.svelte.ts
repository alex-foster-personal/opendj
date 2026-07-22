/**
 * Client-side Web Audio engine for the /performance rekordbox-parity build
 * (build unit: audio-engine). Implements the AudioEngine contract from
 * $lib/rb/types and owns the per-deck DeckState rune stores.
 *
 * Graph per deck (COMPONENT-MAP 1.6):
 *   AudioBufferSourceNode -> TRIM gain -> lowshelf (250 Hz) ->
 *   peaking (1200 Hz) -> highshelf (5 kHz) -> channel fader gain ->
 *   crossfader gain -> master gain -> destination
 *
 * Requirements (mini-PRD):
 *   ✔︎ ✅ 🎯 load(): fetch audio and required analysis via api-rb ->
 *     decodeAudioData -> per-deck chain;
 *     backend 404 (AUDIO_FILE_MISSING etc) -> pushToast + deckLoadErrors set
 *     + reject. Never a silent fallback.
 *     [if] load() of a stable_id whose file is missing [then] toast shown,
 *       deckLoadErrors[deck] holds the code, promise rejects ⛔️
 *     [if] load() succeeds [then] DeckState title/bpm/key/duration populated
 *     [if] a second load() starts before the first resolves [then] the stale
 *       result never clobbers the newer one ⛔️
 *     [if] required analysis retrieval fails [then] load rejects and no
 *       unusable deck candidate is published ⛔️
 *   ✔︎ Reactive transport: position_ms advances via rAF clock math while
 *     playing; remaining time derivable from duration_ms - position_ms;
 *     deckEffectiveBpm() = bpm * pitch.
 *     [if] play() then 1s elapses [then] position_ms ~= 1000 * pitch
 *   ✔︎ CUE semantics: pause() stores the cue at the pause position;
 *     pressCue() while playing returns-to-cue and pauses; while paused it
 *     jumps the playhead to the cue; play() resumes from there.
 *   ✔︎ ✅ 🎯 Loop: engageBeatLoop(beats) converts beats -> seconds from track bpm;
 *     seamless audio via buffer-source loopStart/loopEnd; UI clock wraps the
 *     position manually with the same bounds.
 *     [if] loop engaged and linear clock passes out point [then] position_ms
 *       wraps to in point, audio does not glitch
 *     [if] a requested loop end exceeds decoded duration [then] it clamps to
 *       decoded duration
 *     [if] a requested loop already fits [then] its endpoints stay exact
 *     [if] a requested loop starts at decoded duration [then ⛔️] RangeError
 *   ✔︎ Pitch: setPitch validates 0 < ratio and that it fits the selected
 *     +-8 / +-16 / WIDE range; rebases the clock so position stays correct.
 *     [if] setPitch(1.2) while range is 16 [then ⛔️] RangeError
 *   ✔︎ Mixer: TRIM / 3-band EQ / channel fader / crossfader (A-B assign
 *     matrix with THRU bypass) / master all drive real AudioNodes.
 *   ✔︎ ✅ 🎯 Presented transport: active position/audible state comes from an
 *     acknowledged schedule mirror evaluated at getOutputTimestamp().
 *     [if] render time leads output time [then] UI stays on presented audio
 *     [if] an old schedule/timestamp arrives [then] it cannot rewind state
 *     [if] output reports {0,0} or repeats a timestamp [then] pending state
 *       remains intact and the repeated timestamp is accepted
 *     [if] an existing boundary enters the unsafe horizon [then] a newer
 *       command queues at a fresh safe boundary without losing the old one
 *     [if] 10,000 revisions are presented [then] obsolete schedule history
 *       is pruned while the effective current and future revisions remain
 *     [if] play or seek follows a pending pause [then] the pause is
 *       superseded through the processor schedule instead of moving a live
 *       frozen cursor ⛔️
 *     [if] Beat Sync or sync mode changes during a pending start [then] the
 *       desired playing deck is rescheduled before it becomes audible
 *     [if] AudioContext time stalls or sync state changes while waiting
 *       [then] the wait rejects within a bounded interval ⛔️
 *   ✔︎ Route teardown owns the complete audio lifetime: cancel the clock,
 *     disconnect processors/nodes, close AudioContext, and reset state.
 *     [if] /performance unmounts while audio is live [then] no sound remains
 *     [if] one disconnect fails [then] every later resource is still silenced ⛔️
 *     [if] an old schedule rejects after unmount [then] it cannot poison a
 *       newly mounted route's processor or deck state ⛔️
 *
 * Rune module: this file MUST stay .svelte.ts (rune_outside_svelte
 * otherwise - RECON-FRONTEND 10.1, same bug class as stores.svelte.ts fix).
 */

import { pushToast } from '$lib/stores.svelte';
import {
	fetchAnlz,
	fetchAudioArrayBuffer,
	fetchStemAudioArrayBuffers,
	getTrack,
	probeStemArtifact,
	RbApiError
} from '$lib/rb/api-rb';
import type { DemucsStemPart, Track } from '$lib/rb/api-rb';
import {
	computeFollowerSyncPlan,
	quantizeToNearestBeat,
	validateBeatGrid
} from '$lib/rb/beat-sync-math';
import {
	StretchDeckProcessor,
	type StretchScheduleChange
} from '$lib/rb/stretch-adapter';
import {
	AlignedStemDeckProcessor,
	DEMUCS_PARTS,
	readyStemDeckState,
	STEM_CONTROLS,
	unavailableStemDeckState,
	type StemBuffers
} from '$lib/rb/stem-graph';
import type {
	AnlzBeat,
	AnlzCue,
	AudioEngine,
	CrossfaderAssign,
	DeckAudioSnapshot,
	DeckId,
	DeckState,
	EqBand,
	HeadphoneOutputDevice,
	HeadphoneState,
	HotCue,
	LoopState,
	MixerChannelState,
	MixerState,
	StemControl,
	StemDeckState,
	SyncMode
} from '$lib/rb/types';

// -------------------------------------------------------------- constants

export const DECK_IDS: readonly DeckId[] = [1, 2, 3, 4] as const;

/** Pitch fader range in percent; 100 renders as WIDE in the jog readout. */
export type PitchRange = 8 | 16 | 100;
export const PITCH_RANGES: readonly PitchRange[] = [8, 16, 100] as const;

const EQ_FREQ_LOW_HZ = 250;
const EQ_FREQ_MID_HZ = 1200;
const EQ_FREQ_HIGH_HZ = 5000;
const EQ_MID_Q = 1.0;
/** Knob 0 -> full cut (DJ-mixer style deep cut), knob 1 -> gentle boost. */
const EQ_MIN_DB = -26;
const EQ_MAX_DB = 6;
/** TRIM knob 0..1 maps linearly to 0..2x amplitude (0.5 = unity). */
const TRIM_MAX_GAIN = 2;
/** Smoothing time-constant for AudioParam changes (anti-zipper). */
const PARAM_SMOOTH_S = 0.01;
/** Future schedule margin after the slower deck processor's reported latency. */
const SYNC_SCHEDULE_SAFETY_S = 0.1;
const ANALYSER_FFT_SIZE = 4096;
const CONTEXT_WAIT_POLL_MS = 25;
const CONTEXT_WAIT_STALL_TIMEOUT_MS = 500;
const HEADPHONE_OPERATION_TIMEOUT_MS = 5_000;

// ------------------------------------------------------------ rune stores

function _emptyDeckState(deck_id: DeckId): DeckState {
	return {
		deck_id,
		stable_id: null,
		title: null,
		artist: null,
		bpm: null,
		key: null,
		key_shift_semitones: 0,
		duration_ms: null,
		position_ms: 0,
		playing: false,
		audible: false,
		transport_pending: false,
		cue_ms: null,
		pitch: 1,
		quantize_enabled: true,
		beat_sync_enabled: true,
		master_tempo_enabled: true,
		slip_enabled: false,
		slip_active: false,
		slip_position_ms: null,
		sync_mode: 'bar',
		sync_error: null,
		processor_error: null,
		stems: unavailableStemDeckState(),
		loop: null,
		hot_cues: [],
		anlz: null,
		anlz_error: null,
		is_master: false
	};
}

function _defaultChannel(deck_id: DeckId): MixerChannelState {
	return {
		deck_id,
		trim: 0.5,
		eq_high: 0.5,
		eq_mid: 0.5,
		eq_low: 0.5,
		fader: 1,
		// Screenshot assign-matrix default: odd decks -> bus A, even -> bus B.
		assign: deck_id % 2 === 1 ? 'A' : 'B',
		cue_enabled: false
	};
}

function _defaultHeadphones(): HeadphoneState {
	return {
		mix: 0.5,
		level: 0.5,
		selected_output_device_id: null,
		outputs: [],
		supported: false,
		active: false,
		error: null
	};
}

/** Per-deck reactive UI state, keyed 1-4. Deep-reactive $state proxy. */
export const deckStates: Record<DeckId, DeckState> = $state({
	1: _emptyDeckState(1),
	2: _emptyDeckState(2),
	3: _emptyDeckState(3),
	4: _emptyDeckState(4)
});

/** Explicit audio-load error per deck (backend code or decode message);
 * null = no failed load. DeckState has no audio-error field by contract,
 * so the failure state lives here, never swallowed. */
export const deckLoadErrors: Record<DeckId, string | null> = $state({
	1: null,
	2: null,
	3: null,
	4: null
});

/** Selected pitch range per deck (jog dial readout: +-8 / +-16 / WIDE). */
export const pitchRanges: Record<DeckId, PitchRange> = $state({
	1: 16,
	2: 16,
	3: 16,
	4: 16
});

/** Whole mixer surface (channel order on screen: 3 1 2 4). */
export const mixerState: MixerState = $state({
	channels: {
		1: _defaultChannel(1),
		2: _defaultChannel(2),
		3: _defaultChannel(3),
		4: _defaultChannel(4)
	},
	crossfader: 0.5,
	master: 1,
	headphones: _defaultHeadphones()
});

/** Per-deck store accessor (contract: singleton engine + accessor). */
export function getDeckState(deck: DeckId): DeckState {
	return deckStates[deck];
}

/** Pitch-adjusted BPM for the jog readout; null until a track with a BPM
 * is loaded. Reactive when read inside $derived. */
export function deckEffectiveBpm(deck: DeckId): number | null {
	const st = deckStates[deck];
	return st.bpm === null ? null : st.bpm * st.pitch;
}

/** Remaining track time in ms (for the -MM:SS.d readout); null until a
 * track is loaded. */
export function deckRemainingMs(deck: DeckId): number | null {
	const st = deckStates[deck];
	return st.duration_ms === null ? null : Math.max(0, st.duration_ms - st.position_ms);
}

// -------------------------------------------- non-reactive audio runtime
// AudioNodes and AudioBuffers stay OUT of $state on purpose: proxying
// native audio objects breaks identity checks and buys nothing reactive.

interface _ChannelNodes {
	analyser: AnalyserNode;
	trim: GainNode;
	low: BiquadFilterNode;
	mid: BiquadFilterNode;
	high: BiquadFilterNode;
	cue: GainNode;
	fader: GainNode;
	xf: GainNode;
}

interface _ClockSegment {
	active: boolean;
	loop: LoopState | null;
	startContextTime: number;
	startPositionSec: number;
	tempoRatio: number;
	masterTempoEnabled?: boolean;
	keyShiftSemitones?: number;
}

export interface PresentedTransportSchedule extends _ClockSegment {
	revision: number;
	supersededByRevision: number | null;
}

export interface PresentedTransportTimeline {
	paused_position_sec: number;
	presented_position_sec: number;
	presented_active: boolean;
	desired_revision: number;
	presented_revision: number;
	last_presentation_context_time_s: number | null;
	last_presentation_performance_time_ms: number | null;
	schedules: PresentedTransportSchedule[];
}

export interface PresentedTransportObservation {
	accepted: boolean;
	output_started: boolean;
	presentation_context_time_s: number | null;
	position_sec: number;
	audible: boolean;
	transport_pending: boolean;
	desired_revision: number;
	presented_revision: number;
}

export interface DeckTransportClock {
	source: 'paused_cursor' | 'audio_output';
	presentation_context_time_s: number | null;
	desired_revision: number;
	presented_revision: number;
}

interface _PendingSegment {
	active: boolean;
	loop: LoopState | null;
	startContextTime: number;
	startPositionSec: number;
	tempoRatio: number;
	masterTempoEnabled?: boolean;
	keyShiftSemitones?: number;
}

type _DeckProcessor = StretchDeckProcessor | AlignedStemDeckProcessor;
/** A confirmed loop-entry schedule is the sole anchor for SLIP's hidden
 * playhead. It deliberately uses control time, not the audible UI clock. */
export interface SlipAnchor {
	startContextTime: number;
	startPositionSec: number;
	tempoRatio: number;
	durationSec: number;
}

interface _DeckRuntime {
	processor: _DeckProcessor | null;
	durationSec: number;
	latencySec: number;
	controlActive: boolean;
	controlLoop: LoopState | null;
	controlTempoRatio: number;
	controlMasterTempoEnabled: boolean;
	controlKeyShiftSemitones: number;
	/** ctx.currentTime at the moment the current processor segment starts. */
	startCtxTime: number;
	/** Track offset (seconds) at the moment the segment started. */
	startOffsetSec: number;
	/** Monotonic token guarding against stale load() results. */
	loadToken: number;
	nodes: _ChannelNodes | null;
	pending: _PendingSegment[];
	presentation: PresentedTransportTimeline;
	nextScheduleRevision: number;
	desiredActive: boolean;
	scheduleIntentCount: number;
	scheduleTail: Promise<void>;
	swapTail: Promise<void>;
	slipAnchor: SlipAnchor | null;
}

function _emptyRuntime(): _DeckRuntime {
	return {
		processor: null,
		durationSec: 0,
		latencySec: 0,
		controlActive: false,
		controlLoop: null,
		controlTempoRatio: 1,
		controlMasterTempoEnabled: true,
		controlKeyShiftSemitones: 0,
		startCtxTime: 0,
		startOffsetSec: 0,
		loadToken: 0,
		nodes: null,
		pending: [],
		presentation: createPresentedTransportTimeline(0),
		nextScheduleRevision: 0,
		desiredActive: false,
		scheduleIntentCount: 0,
		scheduleTail: Promise.resolve(),
		swapTail: Promise.resolve(),
		slipAnchor: null
	};
}

let _ctx: AudioContext | null = null;
let _masterGain: GainNode | null = null;
interface _HeadphoneNodes {
	cueSum: GainNode;
	masterMonitor: GainNode;
	cueMix: GainNode;
	masterMix: GainNode;
	level: GainNode;
	destination: MediaStreamAudioDestinationNode;
	element: HTMLAudioElement;
}
let _headphoneNodes: _HeadphoneNodes | null = null;
let _headphoneGeneration = 0;
let _rafId: number | null = null;
let _masterDeck: DeckId | null = null;
const _rt: Record<DeckId, _DeckRuntime> = {
	1: _emptyRuntime(),
	2: _emptyRuntime(),
	3: _emptyRuntime(),
	4: _emptyRuntime()
};

export function deckTransportClock(deck: DeckId): DeckTransportClock {
	const presentation = _rt[deck].presentation;
	return {
		source: presentation.presented_active ? 'audio_output' : 'paused_cursor',
		presentation_context_time_s: presentation.last_presentation_context_time_s,
		desired_revision: presentation.desired_revision,
		presented_revision: presentation.presented_revision
	};
}

interface AudioDisconnectable {
	disconnect(): void;
}

interface AudioContextDisposable {
	readonly state: AudioContextState;
	close(): Promise<void>;
}

interface AudioResources {
	rafId: number | null;
	processors: readonly AudioDisconnectable[];
	nodes: readonly AudioDisconnectable[];
	masterGain: AudioDisconnectable | null;
	context: AudioContextDisposable | null;
}

/**
 * Synchronously silence an owned audio graph, then await context shutdown.
 * Disconnection happens before the first await so route teardown can never
 * leave playback running invisibly while AudioContext.close() settles.
 */
export async function disposeAudioResources(
	resources: AudioResources,
	cancelFrame?: (rafId: number) => void
): Promise<void> {
	const failures: unknown[] = [];
	const attempt = (operation: () => void): void => {
		try {
			operation();
		} catch (error) {
			failures.push(error);
		}
	};
	const rafId = resources.rafId;
	if (rafId !== null) {
		attempt(() => {
			const cancel = cancelFrame ?? globalThis.cancelAnimationFrame;
			if (cancel === undefined) {
				throw new Error('cannot dispose active audio clock: cancelAnimationFrame is unavailable');
			}
			cancel(rafId);
		});
	}
	for (const processor of resources.processors) attempt(() => processor.disconnect());
	for (const node of resources.nodes) attempt(() => node.disconnect());
	const masterGain = resources.masterGain;
	if (masterGain !== null) attempt(() => masterGain.disconnect());
	if (resources.context !== null && resources.context.state !== 'closed') {
		try {
			await resources.context.close();
		} catch (error) {
			failures.push(error);
		}
	}
	if (failures.length === 1) throw failures[0];
	if (failures.length > 1) throw new AggregateError(failures, 'multiple audio teardown operations failed');
}

export function detachProcessorForDisposal<T extends AudioDisconnectable>(owner: {
	processor: T | null;
}): T | null {
	const processor = owner.processor;
	owner.processor = null;
	return processor;
}

// ---------------------------------------------------------------- _helpers

function _assertUnit(name: string, value: number): void {
	if (!Number.isFinite(value) || value < 0 || value > 1) {
		throw new RangeError(`${name} must be within 0..1, got ${value}`);
	}
}

function _eqDbFromKnob(value: number): number {
	// 0 -> EQ_MIN_DB, 0.5 -> 0 dB (flat), 1 -> EQ_MAX_DB. Piecewise linear.
	if (value <= 0.5) return EQ_MIN_DB * (1 - value * 2);
	return EQ_MAX_DB * (value * 2 - 1);
}

function _setParam(param: AudioParam, value: number): void {
	if (_ctx === null) throw new Error('audio graph not initialised');
	param.setTargetAtTime(value, _ctx.currentTime, PARAM_SMOOTH_S);
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
	outputs: readonly HeadphoneOutputDevice[]
): void {
	if (typeof deviceId !== 'string' || deviceId.trim() === '') {
		throw new TypeError('headphone output device id must be a non-empty string');
	}
	if (!outputs.some((output) => output.id === deviceId)) {
		throw new RangeError(`headphone output ${deviceId} is not an enumerated headphone output`);
	}
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

function _applyHeadphoneMix(): void {
	const nodes = _headphoneNodes;
	if (nodes === null) return;
	const gains = headphoneMixGains(mixerState.headphones.mix);
	_setParam(nodes.cueMix.gain, gains.cue);
	_setParam(nodes.masterMix.gain, gains.master);
	_setParam(nodes.level.gain, mixerState.headphones.level);
}

function _headphoneError(operation: string, error: unknown): Error {
	const message = error instanceof Error ? error.message : String(error);
	mixerState.headphones.error = `${operation}: ${message}`;
	return new Error(mixerState.headphones.error, { cause: error });
}

function _assertCurrentHeadphoneOperation(generation: number, nodes: _HeadphoneNodes | null): void {
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

function _ensureHeadphoneGraph(context: AudioContext, masterGain: GainNode): _HeadphoneNodes {
	if (_headphoneNodes !== null) return _headphoneNodes;
	const cueSum = context.createGain();
	const masterMonitor = context.createGain();
	const cueMix = context.createGain();
	const masterMix = context.createGain();
	const level = context.createGain();
	const destination = context.createMediaStreamDestination();
	const element = _createDetachedHeadphoneElement();
	cueSum.connect(cueMix);
	masterGain.connect(masterMonitor);
	masterMonitor.connect(masterMix);
	cueMix.connect(level);
	masterMix.connect(level);
	level.connect(destination);
	_headphoneNodes = { cueSum, masterMonitor, cueMix, masterMix, level, destination, element };
	_applyHeadphoneMix();
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
	for (const node of [nodes.cueSum, nodes.masterMonitor, nodes.cueMix, nodes.masterMix, nodes.level, nodes.destination]) {
		node.disconnect();
	}
	_detachHeadphoneElement(nodes.element);
	for (const track of nodes.destination.stream.getTracks()) track.stop();
}

function _ensureGraph(): AudioContext {
	if (typeof window === 'undefined') {
		throw new Error('AudioEngine requires a browser AudioContext (no SSR usage)');
	}
	if (_ctx !== null) return _ctx;
	_ctx = new AudioContext();
	_masterGain = _ctx.createGain();
	_masterGain.gain.value = mixerState.master;
	_masterGain.connect(_ctx.destination);
	const headphones = _ensureHeadphoneGraph(_ctx, _masterGain);
	for (const deck of DECK_IDS) {
		const ch = mixerState.channels[deck];
		const analyser = _ctx.createAnalyser();
		analyser.fftSize = ANALYSER_FFT_SIZE;
		analyser.minDecibels = -120;
		analyser.maxDecibels = 0;
		analyser.smoothingTimeConstant = 0;
		const trim = _ctx.createGain();
		trim.gain.value = ch.trim * TRIM_MAX_GAIN;
		const low = _ctx.createBiquadFilter();
		low.type = 'lowshelf';
		low.frequency.value = EQ_FREQ_LOW_HZ;
		low.gain.value = _eqDbFromKnob(ch.eq_low);
		const mid = _ctx.createBiquadFilter();
		mid.type = 'peaking';
		mid.frequency.value = EQ_FREQ_MID_HZ;
		mid.Q.value = EQ_MID_Q;
		mid.gain.value = _eqDbFromKnob(ch.eq_mid);
		const high = _ctx.createBiquadFilter();
		high.type = 'highshelf';
		high.frequency.value = EQ_FREQ_HIGH_HZ;
		high.gain.value = _eqDbFromKnob(ch.eq_high);
		const cue = _ctx.createGain();
		cue.gain.value = ch.cue_enabled ? 1 : 0;
		const fader = _ctx.createGain();
		fader.gain.value = ch.fader;
		const xf = _ctx.createGain();
		xf.gain.value = _xfGainFor(ch.assign, mixerState.crossfader);
		analyser.connect(trim);
		trim.connect(low);
		low.connect(mid);
		mid.connect(high);
		high.connect(cue);
		cue.connect(headphones.cueSum);
		high.connect(fader);
		fader.connect(xf);
		xf.connect(_masterGain);
		_rt[deck].nodes = { analyser, trim, low, mid, high, cue, fader, xf };
	}
	return _ctx;
}

/** Equal-power crossfade gain for one bus assignment at position x (0..1). */
function _xfGainFor(assign: CrossfaderAssign, x: number): number {
	if (assign === 'THRU') return 1;
	if (assign === 'A') return Math.cos((x * Math.PI) / 2);
	return Math.cos(((1 - x) * Math.PI) / 2);
}

function _applyCrossfader(): void {
	const x = mixerState.crossfader;
	for (const deck of DECK_IDS) {
		const nodes = _rt[deck].nodes;
		if (nodes === null) continue;
		_setParam(nodes.xf.gain, _xfGainFor(mixerState.channels[deck].assign, x));
	}
}

export function masterTempoSemitones(tempoRatio: number, enabled: boolean): number {
	if (!Number.isFinite(tempoRatio) || tempoRatio <= 0) {
		throw new RangeError(`tempo ratio must be a finite positive number, got ${tempoRatio}`);
	}
	return enabled ? 0 : 12 * Math.log2(tempoRatio);
}

/** Parsed, canonical Camelot key. `root` is a chromatic pitch class where C
 * is 0. Only numbered Camelot notation is accepted, never a guessed musical
 * key label. */
export interface CamelotKey {
	number: number;
	mode: 'A' | 'B';
	root: number;
}

const CAMELOT_ROOTS: Record<CamelotKey['mode'], readonly number[]> = {
	// 1A = Ab minor through 12A = C# minor.
	A: [8, 3, 10, 5, 0, 7, 2, 9, 4, 11, 6, 1],
	// 1B = B major through 12B = E major.
	B: [11, 6, 1, 8, 3, 10, 5, 0, 7, 2, 9, 4]
};

function _pitchClass(semitones: number): number {
	return ((semitones % 12) + 12) % 12;
}

/** Return null for non-Camelot metadata so callers can fail explicitly at
 * their operation boundary rather than inventing a harmonic relationship. */
export function parseCamelotKey(value: string | null): CamelotKey | null {
	if (typeof value !== 'string') return null;
	const match = /^(1[0-2]|[1-9])([ab])$/i.exec(value.trim());
	if (match === null) return null;
	const number = Number(match[1]);
	const mode = match[2].toUpperCase() as CamelotKey['mode'];
	return { number, mode, root: CAMELOT_ROOTS[mode][number - 1] };
}

function _assertKeyShift(semitones: number): asserts semitones is number {
	if (!Number.isInteger(semitones)) {
		throw new TypeError(`key shift must be an integer number of semitones, got ${semitones}`);
	}
	if (semitones < -12 || semitones > 12) {
		throw new RangeError(`key shift must be within -12..12 semitones, got ${semitones}`);
	}
}

function _shiftCamelotKey(key: CamelotKey, semitones: number): CamelotKey {
	_assertKeyShift(semitones);
	const root = _pitchClass(key.root + semitones);
	const number = CAMELOT_ROOTS[key.mode].indexOf(root) + 1;
	if (number === 0) throw new Error(`Camelot ${key.mode} root ${root} cannot be represented`);
	return { number, mode: key.mode, root };
}

function _camelotCircularDistance(left: number, right: number): number {
	const raw = Math.abs(left - right);
	return Math.min(raw, 12 - raw);
}

/** AlphaTheta/Pioneer least-change families: same-wheel and cross-wheel keys
 * are compatible at the same Camelot number and one step either direction.
 * That makes the six named relationships (A/A and A/B, each same/+1/-1)
 * symmetric and preserves 1 <-> 12 wraparound. */
export function camelotKeysAreCompatible(
	deckKey: string | null,
	masterKey: string | null
): boolean {
	const deck = parseCamelotKey(deckKey);
	const master = parseCamelotKey(masterKey);
	if (deck === null || master === null) return false;
	return _camelotCircularDistance(deck.number, master.number) <= 1;
}

function _assertEffectiveAudibleSemitones(name: string, value: number): void {
	if (!Number.isFinite(value)) {
		throw new RangeError(`${name} effective audible semitones must be finite, got ${value}`);
	}
}

function _circularPitchDistance(left: number, right: number): number {
	const distance = Math.abs(_pitchClass(left - right));
	return Math.min(distance, 12 - distance);
}

function _keySyncNudgeCandidates(): number[] {
	const candidates: number[] = [];
	for (let magnitude = 0; magnitude <= 12; magnitude += 1) {
		if (magnitude === 0) candidates.push(0);
		else candidates.push(-magnitude, magnitude);
	}
	return candidates;
}

/** Pick the smallest integer manual nudge whose audible pitch is closest to
 * one of the six Pioneer-compatible Camelot family roots. The source deck
 * mode is retained, while each deck and master may already have a fractional
 * Signalsmith offset from Master Tempo-off tempo compensation. */
export function deriveKeySyncNudge(
	deckKey: string | null,
	masterKey: string | null,
	deckEffectiveAudibleSemitones: number,
	masterEffectiveAudibleSemitones: number,
	deckManualShiftSemitones: number
): number {
	const deck = parseCamelotKey(deckKey);
	const master = parseCamelotKey(masterKey);
	if (deck === null) throw new Error('KEY SYNC requires a parseable Camelot key on the deck');
	if (master === null) throw new Error('KEY SYNC requires a parseable Camelot key on the master');
	_assertEffectiveAudibleSemitones('deck', deckEffectiveAudibleSemitones);
	_assertEffectiveAudibleSemitones('master', masterEffectiveAudibleSemitones);
	_assertKeyShift(deckManualShiftSemitones);

	let bestNudge: number | null = null;
	let bestDistance = Number.POSITIVE_INFINITY;
	for (const nudge of _keySyncNudgeCandidates()) {
		const nextManualShift = deckManualShiftSemitones + nudge;
		if (nextManualShift < -12 || nextManualShift > 12) continue;
		const deckAudibleRoot = deck.root + deckEffectiveAudibleSemitones + nudge;
		for (let number = 1; number <= 12; number += 1) {
			if (_camelotCircularDistance(number, master.number) > 1) continue;
			const familyRoot = CAMELOT_ROOTS[deck.mode][number - 1] + masterEffectiveAudibleSemitones;
			const distance = _circularPitchDistance(deckAudibleRoot, familyRoot);
			if (distance < bestDistance - 1e-12) {
				bestDistance = distance;
				bestNudge = nudge;
			}
		}
	}
	if (bestNudge === null) {
		throw new RangeError('KEY SYNC cannot apply a compatible nudge within -12..12 manual semitones');
	}
	return bestNudge;
}

/** Legacy zero-offset convenience wrapper, returning the final manual shift. */
export function deriveKeySyncSemitones(
	deckKey: string | null,
	masterKey: string | null,
	masterKeyShiftSemitones = 0
): number {
	_assertKeyShift(masterKeyShiftSemitones);
	return deriveKeySyncNudge(deckKey, masterKey, 0, masterKeyShiftSemitones, 0);
}

/** Signalsmith receives one native semitone field. Key shift composes additively
 * with the existing Master Tempo compensation, never by changing transport rate. */
export function composeStretchSemitones(
	tempoRatio: number,
	masterTempoEnabled: boolean,
	keyShiftSemitones: number
): number {
	_assertKeyShift(keyShiftSemitones);
	return masterTempoSemitones(tempoRatio, masterTempoEnabled) + keyShiftSemitones;
}

export interface DeckControlSettings {
	tempoRatio: number;
	masterTempoEnabled: boolean;
	keyShiftSemitones: number;
}

export interface PausedDeckControlUpdate {
	tempoRatio?: number;
	masterTempoEnabled?: boolean;
	keyShiftSemitones?: number;
}

/** Paused controls are the desired DSP settings for the next schedule. Keep
 * them separate from DeckState's output-presented read model. */
export function applyPausedDeckControlSettings(
	current: DeckControlSettings,
	update: PausedDeckControlUpdate
): DeckControlSettings {
	if (!Number.isFinite(current.tempoRatio) || current.tempoRatio <= 0) {
		throw new RangeError(`control tempo ratio must be finite and positive, got ${current.tempoRatio}`);
	}
	if (typeof current.masterTempoEnabled !== 'boolean') {
		throw new TypeError('control Master Tempo must be boolean');
	}
	_assertKeyShift(current.keyShiftSemitones);
	if (
		update.tempoRatio !== undefined &&
		(!Number.isFinite(update.tempoRatio) || update.tempoRatio <= 0)
	) {
		throw new RangeError(`updated tempo ratio must be finite and positive, got ${update.tempoRatio}`);
	}
	if (
		update.masterTempoEnabled !== undefined &&
		typeof update.masterTempoEnabled !== 'boolean'
	) {
		throw new TypeError('updated Master Tempo must be boolean');
	}
	if (update.keyShiftSemitones !== undefined) _assertKeyShift(update.keyShiftSemitones);
	return {
		tempoRatio: update.tempoRatio ?? current.tempoRatio,
		masterTempoEnabled: update.masterTempoEnabled ?? current.masterTempoEnabled,
		keyShiftSemitones: update.keyShiftSemitones ?? current.keyShiftSemitones
	};
}

export function quantizedPositionMs(
	beats: readonly AnlzBeat[],
	positionMs: number,
	quantizeEnabled: boolean
): number {
	if (!Number.isFinite(positionMs) || positionMs < 0) {
		throw new RangeError(`positionMs must be a finite non-negative number, got ${positionMs}`);
	}
	if (!quantizeEnabled) return positionMs;
	return quantizeToNearestBeat(beats, positionMs / 1000) * 1000;
}

export function pausedSeekClock(
	positionMs: number,
	durationMs: number
): { position_ms: number; start_offset_sec: number } {
	if (!Number.isFinite(durationMs) || durationMs <= 0) {
		throw new RangeError(`duration must be finite and positive, got ${durationMs}`);
	}
	if (!Number.isFinite(positionMs) || positionMs < 0 || positionMs > durationMs) {
		throw new RangeError(`position must be within track duration 0..${durationMs}, got ${positionMs}`);
	}
	return { position_ms: positionMs, start_offset_sec: positionMs / 1000 };
}

export function decodedTransportDurationMs(decodedDurationSec: number): number {
	if (!Number.isFinite(decodedDurationSec) || decodedDurationSec <= 0) {
		throw new RangeError(
			`decoded audio duration must be finite and positive, got ${decodedDurationSec}`
		);
	}
	return decodedDurationSec * 1000;
}

export function createPresentedTransportTimeline(
	pausedPositionSec: number
): PresentedTransportTimeline {
	if (!Number.isFinite(pausedPositionSec) || pausedPositionSec < 0) {
		throw new RangeError(
			`pausedPositionSec must be finite and non-negative, got ${pausedPositionSec}`
		);
	}
	return {
		paused_position_sec: pausedPositionSec,
		presented_position_sec: pausedPositionSec,
		presented_active: false,
		desired_revision: 0,
		presented_revision: 0,
		last_presentation_context_time_s: null,
		last_presentation_performance_time_ms: null,
		schedules: []
	};
}

export function setPausedTransportTimelineCursor(
	timeline: PresentedTransportTimeline,
	positionSec: number,
	durationSec: number
): void {
	const clock = pausedSeekClock(positionSec * 1000, durationSec * 1000);
	if (timeline.presented_active || timeline.desired_revision !== timeline.presented_revision) {
		throw new Error('paused transport cursor cannot move while audio is active or pending');
	}
	timeline.paused_position_sec = clock.start_offset_sec;
	timeline.presented_position_sec = clock.start_offset_sec;
}

export function acknowledgePresentedTransportSchedule(
	timeline: PresentedTransportTimeline,
	schedule: Omit<PresentedTransportSchedule, 'supersededByRevision'>
): void {
	if (!Number.isInteger(schedule.revision) || schedule.revision <= 0) {
		throw new RangeError(`schedule revision must be a positive integer, got ${schedule.revision}`);
	}
	if (!Number.isFinite(schedule.startContextTime) || schedule.startContextTime < 0) {
		throw new RangeError(
			`schedule startContextTime must be finite and non-negative, got ${schedule.startContextTime}`
		);
	}
	if (!Number.isFinite(schedule.startPositionSec) || schedule.startPositionSec < 0) {
		throw new RangeError(
			`schedule startPositionSec must be finite and non-negative, got ${schedule.startPositionSec}`
		);
	}
	if (!Number.isFinite(schedule.tempoRatio) || schedule.tempoRatio <= 0) {
		throw new RangeError(
			`schedule tempoRatio must be finite and positive, got ${schedule.tempoRatio}`
		);
	}

	let supersededByRevision: number | null = null;
	if (schedule.revision <= timeline.desired_revision) {
		supersededByRevision = timeline.desired_revision;
	} else {
		for (const existing of timeline.schedules) {
			if (
				existing.supersededByRevision === null &&
				existing.revision > timeline.presented_revision &&
				existing.startContextTime >= schedule.startContextTime
			) {
				existing.supersededByRevision = schedule.revision;
			}
		}
		timeline.desired_revision = schedule.revision;
	}
	const masterTempoEnabled = schedule.masterTempoEnabled ?? true;
	const keyShiftSemitones = schedule.keyShiftSemitones ?? 0;
	_assertKeyShift(keyShiftSemitones);
	timeline.schedules.push({
		...schedule,
		loop: schedule.loop === null ? null : { ...schedule.loop },
		masterTempoEnabled,
		keyShiftSemitones,
		supersededByRevision
	});
	_prunePresentedTransportSchedules(timeline);
}

function _laterPresentedSchedule(
	candidate: PresentedTransportSchedule,
	selected: PresentedTransportSchedule | null
): boolean {
	return (
		selected === null ||
		candidate.startContextTime > selected.startContextTime ||
		(candidate.startContextTime === selected.startContextTime &&
			candidate.revision > selected.revision)
	);
}

function _effectivePresentedScheduleAt(
	timeline: PresentedTransportTimeline,
	contextTime: number
): PresentedTransportSchedule | null {
	let selected: PresentedTransportSchedule | null = null;
	for (const candidate of timeline.schedules) {
		if (
			candidate.supersededByRevision === null &&
			candidate.startContextTime <= contextTime &&
			_laterPresentedSchedule(candidate, selected)
		) {
			selected = candidate;
		}
	}
	return selected;
}

function _prunePresentedTransportSchedules(timeline: PresentedTransportTimeline): void {
	const presentedAt = timeline.last_presentation_context_time_s;
	const effective =
		presentedAt === null ? null : _effectivePresentedScheduleAt(timeline, presentedAt);
	timeline.schedules = timeline.schedules.filter(
		(schedule) =>
			schedule.supersededByRevision === null &&
			(presentedAt === null || schedule === effective || schedule.startContextTime > presentedAt)
	);
}

function _presentedObservation(
	timeline: PresentedTransportTimeline,
	accepted: boolean,
	outputStarted: boolean
): PresentedTransportObservation {
	return {
		accepted,
		output_started: outputStarted,
		presentation_context_time_s: timeline.last_presentation_context_time_s,
		position_sec: timeline.presented_position_sec,
		audible: timeline.presented_active,
		transport_pending: timeline.presented_revision !== timeline.desired_revision,
		desired_revision: timeline.desired_revision,
		presented_revision: timeline.presented_revision
	};
}

export function observePresentedTransportTimeline(
	timeline: PresentedTransportTimeline,
	outputTimestamp: { contextTime: number; performanceTime: number },
	durationSec: number
): PresentedTransportObservation {
	if (!Number.isFinite(durationSec) || durationSec <= 0) {
		throw new RangeError(`durationSec must be finite and positive, got ${durationSec}`);
	}
	const { contextTime, performanceTime } = outputTimestamp;
	if (
		!Number.isFinite(contextTime) ||
		!Number.isFinite(performanceTime) ||
		contextTime < 0 ||
		performanceTime < 0
	) {
		throw new RangeError(
			`output timestamp must contain finite non-negative values, got ` +
				`contextTime=${contextTime}, performanceTime=${performanceTime}`
		);
	}
	// Chromium may expose the current performance clock while the output frame
	// remains at zero during device warmup. Context time is presentation truth.
	if (contextTime === 0) {
		return _presentedObservation(
			timeline,
			false,
			timeline.last_presentation_context_time_s !== null
		);
	}
	const previousContextTime = timeline.last_presentation_context_time_s;
	if (previousContextTime !== null && contextTime < previousContextTime) {
		return _presentedObservation(timeline, false, true);
	}

	const selected = _effectivePresentedScheduleAt(timeline, contextTime);
	if (selected !== null && selected.revision < timeline.presented_revision) {
		throw new Error(
			`presented schedule revision regressed from ${timeline.presented_revision} to ` +
				`${selected.revision} at contextTime=${contextTime}`
		);
	}

	if (selected === null) {
		timeline.presented_position_sec = timeline.paused_position_sec;
		timeline.presented_active = false;
	} else {
		const crossesNewRevision = selected.revision > timeline.presented_revision;
		const positionSec = selected.active
			? _positionForSegment(selected, contextTime, durationSec)
			: crossesNewRevision
				? selected.startPositionSec
				: timeline.paused_position_sec;
		const audible = selected.active && !deckReachedEnd(positionSec, durationSec, selected.loop);
		timeline.presented_position_sec = positionSec;
		timeline.presented_active = audible;
		timeline.presented_revision = Math.max(timeline.presented_revision, selected.revision);
		if (!audible) timeline.paused_position_sec = positionSec;
	}
	timeline.last_presentation_context_time_s = contextTime;
	// Performance time is correlation/diagnostic data, never transport authority.
	timeline.last_presentation_performance_time_ms = performanceTime;
	_prunePresentedTransportSchedules(timeline);
	return _presentedObservation(timeline, true, true);
}

export function seekSyncMaster(
	deck: DeckId,
	playing: boolean,
	beatSyncEnabled: boolean,
	master: DeckId | null
): DeckId | null {
	return playing && beatSyncEnabled && master !== null && master !== deck ? master : null;
}

export function syncChangeRequiresReschedule(
	deck: DeckId,
	desiredActive: boolean,
	beatSyncEnabled: boolean,
	master: DeckId | null
): boolean {
	if (typeof desiredActive !== 'boolean' || typeof beatSyncEnabled !== 'boolean') {
		throw new TypeError('sync desired-active and Beat Sync flags must be boolean');
	}
	if (!DECK_IDS.includes(deck) || (master !== null && !DECK_IDS.includes(master))) {
		throw new RangeError(`sync deck ids must be within 1..4, got deck=${deck}, master=${master}`);
	}
	return desiredActive && beatSyncEnabled && master !== null && master !== deck;
}

export function quantizedLoopEndpointsMs(
	beats: readonly AnlzBeat[],
	loop: { in_ms: number; out_ms: number },
	quantizeEnabled: boolean
): { in_ms: number; out_ms: number } {
	if (
		!Number.isFinite(loop.in_ms) ||
		!Number.isFinite(loop.out_ms) ||
		loop.in_ms < 0 ||
		loop.out_ms <= loop.in_ms
	) {
		throw new RangeError(`loop requires finite 0 <= in_ms < out_ms, got ${loop.in_ms}..${loop.out_ms}`);
	}
	if (!quantizeEnabled) return { ...loop };
	const snapped = {
		in_ms: quantizeToNearestBeat(beats, loop.in_ms / 1000) * 1000,
		out_ms: quantizeToNearestBeat(beats, loop.out_ms / 1000) * 1000
	};
	if (snapped.out_ms <= snapped.in_ms) {
		throw new RangeError(
			`quantized loop collapsed at ${snapped.in_ms}ms; choose endpoints spanning distinct PQTZ beats`
		);
	}
	return snapped;
}

/** Bound a valid loop to the decoded audio duration. Overshoot is expected
 * for a final PQTZ interval that extends past the decoded buffer boundary;
 * an empty loop remains an explicit error. */
export function loopEndpointsWithinDurationMs(
	loop: { in_ms: number; out_ms: number },
	durationMs: number
): { in_ms: number; out_ms: number } {
	if (!Number.isFinite(durationMs) || durationMs <= 0) {
		throw new RangeError(`decoded duration must be finite and positive, got ${durationMs}`);
	}
	if (
		!Number.isFinite(loop.in_ms) ||
		!Number.isFinite(loop.out_ms) ||
		loop.in_ms < 0 ||
		loop.out_ms <= loop.in_ms
	) {
		throw new RangeError(`loop requires finite 0 <= in_ms < out_ms, got ${loop.in_ms}..${loop.out_ms}`);
	}
	const out_ms = Math.min(loop.out_ms, durationMs);
	if (out_ms <= loop.in_ms) {
		throw new RangeError(
			`loop would be empty at decoded duration ${durationMs}ms, got ${loop.in_ms}..${loop.out_ms}`
		);
	}
	return { in_ms: loop.in_ms, out_ms };
}

export function exactBeatLoopRangeMs(
	beats: readonly AnlzBeat[],
	positionMs: number,
	beatCount: number,
	startMs?: number
): { in_ms: number; out_ms: number } {
	if (!Number.isInteger(beatCount) || beatCount <= 0) {
		throw new RangeError(`beatCount must be a positive integer, got ${beatCount}`);
	}
	const anchorMs = startMs ?? positionMs;
	if (!Number.isFinite(anchorMs) || anchorMs < 0) {
		throw new RangeError(`loop anchor must be a finite non-negative number, got ${anchorMs}`);
	}
	const startSec = quantizeToNearestBeat(beats, anchorMs / 1000);
	const startIndex = beats.findIndex((beat) => beat.t === startSec);
	const endIndex = startIndex + beatCount;
	if (startIndex < 0 || endIndex >= beats.length) {
		throw new RangeError(
			`${beatCount} PQTZ beats do not fit from loop anchor ${anchorMs}ms`
		);
	}
	return { in_ms: startSec * 1000, out_ms: beats[endIndex].t * 1000 };
}

export function supersedingScheduleTime(
	requestedContextTime: number,
	pendingContextTime: number | null,
	minimumContextTime = 0
): number {
	if (!Number.isFinite(requestedContextTime) || requestedContextTime < 0) {
		throw new RangeError(
			`requestedContextTime must be finite and non-negative, got ${requestedContextTime}`
		);
	}
	if (!Number.isFinite(minimumContextTime) || minimumContextTime < 0) {
		throw new RangeError(
			`minimumContextTime must be finite and non-negative, got ${minimumContextTime}`
		);
	}
	if (requestedContextTime < minimumContextTime) {
		throw new RangeError(
			`requestedContextTime ${requestedContextTime} precedes minimumContextTime ` +
				`${minimumContextTime}`
		);
	}
	if (pendingContextTime === null) return requestedContextTime;
	if (!Number.isFinite(pendingContextTime) || pendingContextTime < 0) {
		throw new RangeError(
			`pendingContextTime must be finite and non-negative, got ${pendingContextTime}`
		);
	}
	if (pendingContextTime < minimumContextTime) return requestedContextTime;
	return Math.min(requestedContextTime, pendingContextTime);
}

export function commonSyncScheduleTimes(
	syncAtContextTime: number,
	participantCount: number
): number[] {
	if (!Number.isFinite(syncAtContextTime) || syncAtContextTime < 0) {
		throw new RangeError(
			`syncAtContextTime must be finite and non-negative, got ${syncAtContextTime}`
		);
	}
	if (!Number.isInteger(participantCount) || participantCount <= 0) {
		throw new RangeError(`participant count must be a positive integer, got ${participantCount}`);
	}
	return Array.from({ length: participantCount }, () => syncAtContextTime);
}

export function pendingSyncWaitTarget(
	requestedSafeContextTime: number,
	pendingContextTimes: readonly number[]
): number | null {
	if (!Number.isFinite(requestedSafeContextTime) || requestedSafeContextTime < 0) {
		throw new RangeError(
			`requestedSafeContextTime must be finite and non-negative, got ${requestedSafeContextTime}`
		);
	}
	const unsafePendingTimes = pendingContextTimes.filter((contextTime) => {
		if (!Number.isFinite(contextTime) || contextTime < 0) {
			throw new RangeError(
				`pending sync context time must be finite and non-negative, got ${contextTime}`
			);
		}
		return contextTime < requestedSafeContextTime;
	});
	return unsafePendingTimes.length === 0 ? null : Math.max(...unsafePendingTimes);
}

export function safeSyncScheduleTime(
	nowContextTime: number,
	maxLatencySec: number,
	masterReadyContextTime: number,
	safetySec = SYNC_SCHEDULE_SAFETY_S
): number {
	for (const [name, value] of Object.entries({
		nowContextTime,
		maxLatencySec,
		masterReadyContextTime,
		safetySec
	})) {
		if (!Number.isFinite(value) || value < 0) {
			throw new RangeError(`${name} must be finite and non-negative, got ${value}`);
		}
	}
	return Math.max(
		nowContextTime + maxLatencySec + safetySec,
		masterReadyContextTime + safetySec
	);
}

export function safeTransportScheduleTime(
	nowContextTime: number,
	latencySec: number,
	safetySec = SYNC_SCHEDULE_SAFETY_S
): number {
	for (const [name, value] of Object.entries({ nowContextTime, latencySec, safetySec })) {
		if (!Number.isFinite(value) || value < 0) {
			throw new RangeError(`${name} must be finite and non-negative, got ${value}`);
		}
	}
	if (safetySec === 0) throw new RangeError('safetySec must be greater than zero');
	return nowContextTime + latencySec + safetySec;
}

export function projectedTransportPosition(input: {
	now: number;
	startContextTime: number;
	startPositionSec: number;
	tempoRatio: number;
	projectAt: number;
}): number {
	for (const [name, value] of Object.entries(input)) {
		if (!Number.isFinite(value)) throw new RangeError(`${name} must be finite, got ${value}`);
	}
	if (input.tempoRatio <= 0) throw new RangeError('tempoRatio must be positive');
	if (input.projectAt < input.now) throw new RangeError('projectAt must not precede now');
	const projectionEpoch = Math.max(input.now, input.startContextTime);
	return input.startPositionSec + Math.max(0, input.projectAt - projectionEpoch) * input.tempoRatio;
}

/** The key UI must not lead the listener. This accessor resolves only the
 * schedule revision which has crossed the output presentation clock. */
export function presentedKeyShiftSemitonesAt(
	timeline: PresentedTransportTimeline,
	contextTime: number
): number | null {
	if (!Number.isFinite(contextTime) || contextTime < 0) {
		throw new RangeError(`presentation context time must be finite and non-negative, got ${contextTime}`);
	}
	const schedule = _effectivePresentedScheduleAt(timeline, contextTime);
	return schedule?.keyShiftSemitones ?? null;
}

/** KEY SYNC derives harmonic offsets from the acknowledged output clock only.
 * Render/control time may be ahead of the listener and must never leak here. */
export function presentedEffectiveAudibleSemitones(
	timeline: PresentedTransportTimeline
): number {
	const presentedAt = timeline.last_presentation_context_time_s;
	if (presentedAt === null) {
		throw new Error('KEY SYNC requires output presentation truth before deriving effective offsets');
	}
	const schedule = _effectivePresentedScheduleAt(timeline, presentedAt);
	if (schedule === null) {
		throw new Error('KEY SYNC requires an output-presented schedule before deriving effective offsets');
	}
	return composeStretchSemitones(
		schedule.tempoRatio,
		schedule.masterTempoEnabled ?? true,
		schedule.keyShiftSemitones ?? 0
	);
}

export interface KeySyncEffectiveOffsetSource {
	audible: boolean;
	transportPending: boolean;
	pendingMutation: boolean;
	control: DeckControlSettings;
	presentation: PresentedTransportTimeline;
}

/** A truly stopped deck has no listener-facing schedule to read, so its next
 * play must use the desired control values. Every live or queued path remains
 * output-clock authoritative and fails until presentation truth exists. */
export function keySyncEffectiveAudibleSemitones(
	source: KeySyncEffectiveOffsetSource
): number {
	for (const [name, value] of Object.entries({
		audible: source.audible,
		transportPending: source.transportPending,
		pendingMutation: source.pendingMutation
	})) {
		if (typeof value !== 'boolean') throw new TypeError(`KEY SYNC ${name} must be boolean`);
	}
	const hasPresentedActiveSchedule = source.presentation.presented_active;
	const hasPendingPresentationMutation =
		source.presentation.desired_revision !== source.presentation.presented_revision;
	const quiescent =
		!source.audible &&
		!source.transportPending &&
		!source.pendingMutation &&
		!hasPresentedActiveSchedule &&
		!hasPendingPresentationMutation;
	if (quiescent) {
		return composeStretchSemitones(
			source.control.tempoRatio,
			source.control.masterTempoEnabled,
			source.control.keyShiftSemitones
		);
	}
	return presentedEffectiveAudibleSemitones(source.presentation);
}

export function shouldActivateSlip(playing: boolean, slipEnabled: boolean): boolean {
	if (typeof playing !== 'boolean' || typeof slipEnabled !== 'boolean') {
		throw new TypeError('SLIP activation inputs must be boolean');
	}
	return playing && slipEnabled;
}

export function createSlipAnchor(input: SlipAnchor): SlipAnchor {
	for (const [name, value] of Object.entries(input)) {
		if (!Number.isFinite(value)) throw new RangeError(`${name} must be finite, got ${value}`);
	}
	if (input.startContextTime < 0 || input.startPositionSec < 0) {
		throw new RangeError('SLIP anchor time and position must be non-negative');
	}
	if (input.tempoRatio <= 0) throw new RangeError('tempoRatio must be positive');
	if (input.durationSec <= 0 || input.startPositionSec > input.durationSec) {
		throw new RangeError('SLIP anchor position must be within a positive decoded duration');
	}
	return { ...input };
}

/** Hidden SLIP time always advances linearly and clamps at decoded EOF. It
 * never uses loop normalization or replaces the output-presented cursor. */
export function slipHiddenPositionSec(anchor: SlipAnchor, contextTime: number): number {
	const validAnchor = createSlipAnchor(anchor);
	if (!Number.isFinite(contextTime)) {
		throw new RangeError(`contextTime must be finite, got ${contextTime}`);
	}
	const elapsed = Math.max(0, contextTime - validAnchor.startContextTime);
	return Math.min(validAnchor.durationSec, validAnchor.startPositionSec + elapsed * validAnchor.tempoRatio);
}

export function projectedLoopAwareTransportPosition(input: {
	active: boolean;
	loop: LoopState | null;
	startContextTime: number;
	startPositionSec: number;
	tempoRatio: number;
	projectAt: number;
	durationSec: number;
}): number {
	for (const [name, value] of Object.entries(input)) {
		if (name !== 'loop' && name !== 'active' && !Number.isFinite(value)) {
			throw new RangeError(`${name} must be finite, got ${String(value)}`);
		}
	}
	if (input.tempoRatio <= 0) throw new RangeError('tempoRatio must be positive');
	if (input.durationSec <= 0) throw new RangeError('durationSec must be positive');
	return _positionForSegment(
		{
			active: input.active,
			loop: input.loop,
			startContextTime: input.startContextTime,
			startPositionSec: input.startPositionSec,
			tempoRatio: input.tempoRatio
		},
		input.projectAt,
		input.durationSec
	);
}

export function normalizeEngagedLoopPositionSec(
	positionSec: number,
	loop: LoopState | null
): number {
	if (!Number.isFinite(positionSec) || positionSec < 0) {
		throw new RangeError(`positionSec must be finite and non-negative, got ${positionSec}`);
	}
	if (loop === null || !loop.engaged) return positionSec;
	const loopStartSec = loop.in_ms / 1000;
	const loopEndSec = loop.out_ms / 1000;
	const loopSpanSec = loopEndSec - loopStartSec;
	if (!Number.isFinite(loopSpanSec) || loopStartSec < 0 || loopSpanSec <= 0) {
		throw new RangeError(`engaged loop must satisfy 0 <= in_ms < out_ms`);
	}
	if (positionSec >= loopStartSec && positionSec < loopEndSec) return positionSec;
	const wrappedOffsetSec =
		((positionSec - loopStartSec) % loopSpanSec + loopSpanSec) % loopSpanSec;
	return loopStartSec + wrappedOffsetSec;
}

export function normalizeScheduledTransportEntrySec(
	positionSec: number,
	durationSec: number,
	loop: LoopState | null,
	active: boolean
): number {
	if (!Number.isFinite(durationSec) || durationSec <= 0) {
		throw new RangeError(`durationSec must be finite and positive, got ${durationSec}`);
	}
	if (!Number.isFinite(positionSec) || positionSec < 0 || positionSec > durationSec) {
		throw new RangeError(
			`positionSec must be within 0..${durationSec}, got ${positionSec}`
		);
	}
	if (typeof active !== 'boolean') {
		throw new TypeError(`active must be boolean, got ${String(active)}`);
	}
	if (!active) return positionSec;
	const normalizedPositionSec = normalizeEngagedLoopPositionSec(positionSec, loop);
	if (normalizedPositionSec > durationSec) {
		throw new RangeError(
			`normalized loop position ${normalizedPositionSec} exceeds duration ${durationSec}`
		);
	}
	return normalizedPositionSec;
}

export function deckReachedEnd(
	positionSec: number,
	durationSec: number,
	loop: LoopState | null
): boolean {
	return !loop?.engaged && positionSec >= durationSec;
}

export function naturalEndNeedsRevisionedStop(
	playing: boolean,
	observation: Pick<
		PresentedTransportObservation,
		'audible' | 'transport_pending' | 'position_sec'
	>,
	durationSec: number,
	scheduleIntentCount: number
): boolean {
	if (typeof playing !== 'boolean') {
		throw new TypeError(`playing must be boolean, got ${String(playing)}`);
	}
	if (!Number.isFinite(durationSec) || durationSec <= 0) {
		throw new RangeError(`durationSec must be finite and positive, got ${durationSec}`);
	}
	if (
		typeof observation.audible !== 'boolean' ||
		typeof observation.transport_pending !== 'boolean'
	) {
		throw new TypeError('natural-end observation flags must be boolean');
	}
	if (!Number.isFinite(observation.position_sec) || observation.position_sec < 0) {
		throw new RangeError(
			`natural-end position must be finite and non-negative, got ${observation.position_sec}`
		);
	}
	if (!Number.isInteger(scheduleIntentCount) || scheduleIntentCount < 0) {
		throw new RangeError(
			`scheduleIntentCount must be a non-negative integer, got ${scheduleIntentCount}`
		);
	}
	return (
		playing &&
		!observation.audible &&
		!observation.transport_pending &&
		scheduleIntentCount === 0 &&
		observation.position_sec >= durationSec
	);
}

export interface TransportMutationActivity {
	playing: boolean;
	audible: boolean;
	controlActive: boolean;
	pendingScheduleCount: number;
	scheduleIntentCount: number;
}

export function transportNeedsScheduledMutation(
	activity: TransportMutationActivity
): boolean {
	for (const [name, value] of Object.entries({
		playing: activity.playing,
		audible: activity.audible,
		controlActive: activity.controlActive
	})) {
		if (typeof value !== 'boolean') {
			throw new TypeError(`${name} must be boolean, got ${String(value)}`);
		}
	}
	for (const [name, value] of Object.entries({
		pendingScheduleCount: activity.pendingScheduleCount,
		scheduleIntentCount: activity.scheduleIntentCount
	})) {
		if (!Number.isInteger(value) || value < 0) {
			throw new RangeError(`${name} must be a non-negative integer, got ${value}`);
		}
	}
	return (
		activity.playing ||
		activity.audible ||
		activity.controlActive ||
		activity.pendingScheduleCount > 0 ||
		activity.scheduleIntentCount > 0
	);
}

export function nextPlayingMaster(playingDecks: readonly DeckId[]): DeckId | null {
	return DECK_IDS.find((deck) => playingDecks.includes(deck)) ?? null;
}

export function assertDeckLoadConsistency(
	stableId: string | null,
	durationSec: number,
	hasProcessor: boolean
): void {
	const stateLoaded = stableId !== null;
	const runtimeLoaded = hasProcessor && durationSec > 0;
	if (stateLoaded !== runtimeLoaded) {
		throw new Error(
			`inconsistent deck load state: stable_id=${String(stableId)}, ` +
				`duration=${durationSec}, processor=${hasProcessor}`
		);
	}
}

export interface DeckReplacementActivity {
	playing: boolean;
	audible: boolean;
	transportPending: boolean;
	controlActive: boolean;
	pendingScheduleCount: number;
	scheduleIntentCount: number;
}

export function assertDeckReplacementAllowed(
	deck: DeckId,
	activity: DeckReplacementActivity
): void {
	for (const [name, value] of Object.entries({
		playing: activity.playing,
		audible: activity.audible,
		transportPending: activity.transportPending,
		controlActive: activity.controlActive
	})) {
		if (typeof value !== 'boolean') {
			throw new TypeError(`${name} must be boolean, got ${String(value)}`);
		}
	}
	for (const [name, value] of Object.entries({
		pendingScheduleCount: activity.pendingScheduleCount,
		scheduleIntentCount: activity.scheduleIntentCount
	})) {
		if (!Number.isInteger(value) || value < 0) {
			throw new RangeError(`${name} must be a non-negative integer, got ${value}`);
		}
	}
	const activeReasons = Object.entries(activity)
		.filter(([, value]) => value === true || (typeof value === 'number' && value > 0))
		.map(([name]) => name);
	if (activeReasons.length === 0) return;
	throw new Error(
		`load: deck ${deck} must be fully stopped before replacement; active state: ` +
			activeReasons.join(', ')
	);
}

export function loadCandidateCanPublish(candidateToken: number, currentToken: number): boolean {
	for (const [name, value] of Object.entries({ candidateToken, currentToken })) {
		if (!Number.isInteger(value) || value <= 0) {
			throw new RangeError(`${name} must be a positive integer, got ${value}`);
		}
	}
	return candidateToken === currentToken;
}

export function assertPausedMasterSelectionAllowed(
	deck: DeckId,
	audible: boolean,
	otherActiveDecks: readonly DeckId[]
): void {
	if (audible || otherActiveDecks.length === 0) return;
	throw new Error(
		`setDeckMaster: cannot select paused deck ${deck} while decks ` +
			`[${otherActiveDecks.join(',')}] are audible or scheduled to play`
	);
}

export function pausedMasterSelectionBlockers(
	deck: DeckId,
	activity: Readonly<Record<DeckId, Pick<DeckState, 'audible' | 'playing'>>>
): DeckId[] {
	return DECK_IDS.filter(
		(candidate) =>
			candidate !== deck && (activity[candidate].audible || activity[candidate].playing)
	);
}

export function masterSwitchFollowers(
	deck: DeckId,
	activity: Readonly<Record<DeckId, Pick<DeckState, 'playing' | 'beat_sync_enabled'>>>
): DeckId[] {
	return DECK_IDS.filter(
		(candidate) =>
			candidate !== deck &&
			activity[candidate].playing &&
			activity[candidate].beat_sync_enabled
	);
}

function _assertCurrentDeckReplacementAllowed(deck: DeckId): void {
	const st = deckStates[deck];
	const rt = _rt[deck];
	assertDeckReplacementAllowed(deck, {
		playing: st.playing,
		audible: st.audible,
		transportPending: st.transport_pending,
		controlActive: rt.controlActive,
		pendingScheduleCount: rt.pending.length,
		scheduleIntentCount: rt.scheduleIntentCount
	});
}

function _requireLoaded(deck: DeckId, op: string): { st: DeckState; rt: _DeckRuntime } {
	const rt = _rt[deck];
	const st = deckStates[deck];
	if (rt.processor === null || rt.durationSec <= 0 || st.stable_id === null) {
		throw new Error(`${op}: no track loaded on deck ${deck}`);
	}
	return { st, rt };
}

function _durationSec(deck: DeckId): number {
	const rt = _rt[deck];
	if (rt.processor === null || rt.durationSec <= 0) {
		throw new Error(`deck ${deck} has no decoded processor buffers`);
	}
	return rt.durationSec;
}

function _setPausedPosition(deck: DeckId, positionMs: number): void {
	const { st, rt } = _requireLoaded(deck, '_setPausedPosition');
	if (st.playing || st.audible || rt.controlActive || rt.pending.length > 0) {
		throw new Error(`_setPausedPosition: deck ${deck} transport is not paused`);
	}
	const clock = pausedSeekClock(positionMs, rt.durationSec * 1000);
	setPausedTransportTimelineCursor(rt.presentation, clock.start_offset_sec, rt.durationSec);
	st.position_ms = clock.position_ms;
	rt.startOffsetSec = clock.start_offset_sec;
	rt.startCtxTime = _ctx?.currentTime ?? 0;
}

function _requireBeatGrid(st: DeckState, operation: string): readonly AnlzBeat[] {
	const beats = st.anlz?.beatgrid.beats;
	try {
		validateBeatGrid(beats ?? []);
	} catch (error) {
		throw new Error(
			`${operation}: deck ${st.deck_id} requires a valid real PQTZ beat grid: ${String(error)}`,
			{ cause: error }
		);
	}
	return beats ?? [];
}

function _assignMaster(deck: DeckId | null): void {
	_masterDeck = deck;
	for (const candidate of DECK_IDS) deckStates[candidate].is_master = candidate === deck;
}

function _playingMaster(): DeckId | null {
	return _masterDeck !== null && deckStates[_masterDeck].audible ? _masterDeck : null;
}

function _syncMaster(): DeckId | null {
	return _masterDeck !== null && deckStates[_masterDeck].playing ? _masterDeck : null;
}

function _electPlayingMaster(): DeckId | null {
	const next = nextPlayingMaster(DECK_IDS.filter((deck) => deckStates[deck].audible));
	_assignMaster(next);
	return next;
}

function _handleAudibleTransition(deck: DeckId, wasAudible: boolean, audible: boolean): void {
	if (audible && !wasAudible && _playingMaster() === null) {
		_assignMaster(deck);
	} else if (!audible && wasAudible && _masterDeck === deck) {
		_electPlayingMaster();
	}
}

function _recordProcessorFailure(deck: DeckId, error: unknown): void {
	const st = deckStates[deck];
	const rt = _rt[deck];
	const message = error instanceof Error ? error.message : String(error);
	if (rt.processor !== null) rt.processor.disconnect();
	rt.processor = null;
	rt.durationSec = 0;
	rt.latencySec = 0;
	rt.pending = [];
	rt.controlActive = false;
	rt.controlLoop = null;
	rt.controlTempoRatio = 1;
	rt.controlMasterTempoEnabled = true;
	rt.controlKeyShiftSemitones = 0;
	rt.presentation = createPresentedTransportTimeline(0);
	rt.nextScheduleRevision = 0;
	rt.desiredActive = false;
	_clearLoadedTrackState(st);
	st.processor_error = message;
	st.sync_error = message;
	pushToast(`Deck ${deck} processor failed - ${message}`, 'error');
	if (_masterDeck === deck) _electPlayingMaster();
}

export function stretchScheduleChange(
	inputSec: number,
	active: boolean,
	tempoRatio: number,
	masterTempoEnabled: boolean,
	keyShiftSemitones: number,
	loop: LoopState | null
): StretchScheduleChange {
	return {
		active,
		input: inputSec,
		rate: tempoRatio,
		semitones: composeStretchSemitones(tempoRatio, masterTempoEnabled, keyShiftSemitones),
		loopStart: loop !== null && loop.engaged ? loop.in_ms / 1000 : 0,
		loopEnd: loop !== null && loop.engaged ? loop.out_ms / 1000 : 0
	};
}

async function _scheduleDeck(
	deck: DeckId,
	when: number,
	inputSec: number | ((effectiveWhen: number) => number),
	active: boolean,
	tempoRatio?: number,
	masterTempoEnabled?: boolean,
	loop?: LoopState | null,
	keyShiftSemitones?: number
): Promise<number> {
	const rt = _rt[deck];
	const expectedLoadToken = rt.loadToken;
	const expectedProcessor = rt.processor;
	const predecessor = rt.scheduleTail;
	let release!: () => void;
	rt.desiredActive = active;
	rt.scheduleIntentCount += 1;
	rt.scheduleTail = new Promise<void>((resolve) => {
		release = resolve;
	});
	await predecessor;
	try {
		if (rt.loadToken !== expectedLoadToken || rt.processor !== expectedProcessor) {
			throw new Error(`_scheduleDeck: deck ${deck} changed while the command was queued`);
		}
		return await _scheduleDeckSerial(
			deck,
			when,
			inputSec,
			active,
			tempoRatio,
			masterTempoEnabled,
			loop,
			keyShiftSemitones
		);
	} finally {
		rt.scheduleIntentCount -= 1;
		release();
	}
}

async function _scheduleDeckSerial(
	deck: DeckId,
	when: number,
	inputSec: number | ((effectiveWhen: number) => number),
	active: boolean,
	tempoRatio: number | undefined,
	masterTempoEnabled: boolean | undefined,
	loop: LoopState | null | undefined,
	keyShiftSemitones: number | undefined
): Promise<number> {
	const { st, rt } = _requireLoaded(deck, '_scheduleDeck');
	_commitPendingIfDue(deck);
	const processor = rt.processor;
	if (processor === null) throw new Error(`_scheduleDeck: deck ${deck} processor is missing`);
	if (_ctx === null) throw new Error('_scheduleDeck: audio graph not initialised');
	const minimumSafeWhen = safeTransportScheduleTime(_ctx.currentTime, rt.latencySec);
	const safeRequestedWhen = Math.max(when, minimumSafeWhen);
	const latestPending = rt.pending[rt.pending.length - 1] ?? null;
	const effectiveWhen = supersedingScheduleTime(
		safeRequestedWhen,
		latestPending?.startContextTime ?? null,
		minimumSafeWhen
	);
	const scheduledTempoRatio = tempoRatio ?? latestPending?.tempoRatio ?? rt.controlTempoRatio;
	const scheduledMasterTempoEnabled =
		masterTempoEnabled ?? latestPending?.masterTempoEnabled ?? rt.controlMasterTempoEnabled;
	const scheduledKeyShiftSemitones =
		keyShiftSemitones ?? latestPending?.keyShiftSemitones ?? rt.controlKeyShiftSemitones;
	_assertKeyShift(scheduledKeyShiftSemitones);
	const scheduledLoop = loop === undefined ? st.loop : loop;
	const requestedInputSec =
		typeof inputSec === 'function' ? inputSec(effectiveWhen) : inputSec;
	const scheduledInputSec = normalizeScheduledTransportEntrySec(
		requestedInputSec,
		rt.durationSec,
		scheduledLoop,
		active
	);
	const revision = ++rt.nextScheduleRevision;
	try {
		await processor.schedule(
			effectiveWhen,
			stretchScheduleChange(
				scheduledInputSec,
				active,
				scheduledTempoRatio,
				scheduledMasterTempoEnabled,
				scheduledKeyShiftSemitones,
				scheduledLoop
			)
		);
	} catch (error) {
		if (rt.processor === processor) _recordProcessorFailure(deck, error);
		throw error;
	}
	if (rt.processor !== processor) {
		throw new Error(`_scheduleDeck: deck ${deck} processor was replaced before acknowledgement`);
	}
	acknowledgePresentedTransportSchedule(rt.presentation, {
		revision,
		active,
		loop: scheduledLoop,
		startContextTime: effectiveWhen,
		startPositionSec: scheduledInputSec,
		tempoRatio: scheduledTempoRatio,
		masterTempoEnabled: scheduledMasterTempoEnabled,
		keyShiftSemitones: scheduledKeyShiftSemitones
	});
	st.pitch = scheduledTempoRatio;
	st.master_tempo_enabled = scheduledMasterTempoEnabled;
	st.loop = scheduledLoop === null ? null : { ...scheduledLoop };
	st.playing = rt.desiredActive;
	rt.pending = rt.pending.filter((pending) => pending.startContextTime < effectiveWhen);
	rt.pending.push({
		active,
		loop: scheduledLoop === null ? null : { ...scheduledLoop },
		startContextTime: effectiveWhen,
		startPositionSec: scheduledInputSec,
		tempoRatio: scheduledTempoRatio,
		masterTempoEnabled: scheduledMasterTempoEnabled,
		keyShiftSemitones: scheduledKeyShiftSemitones
	});
	st.transport_pending =
		rt.presentation.presented_revision !== rt.presentation.desired_revision;
	_commitPendingIfDue(deck);
	_ensureRaf();
	return scheduledInputSec;
}

function _applyPausedDeckControlSettings(
	st: DeckState,
	rt: _DeckRuntime,
	update: PausedDeckControlUpdate
): DeckControlSettings {
	const next = applyPausedDeckControlSettings(
		{
			tempoRatio: rt.controlTempoRatio,
			masterTempoEnabled: rt.controlMasterTempoEnabled,
			keyShiftSemitones: rt.controlKeyShiftSemitones
		},
		update
	);
	rt.controlTempoRatio = next.tempoRatio;
	rt.controlMasterTempoEnabled = next.masterTempoEnabled;
	rt.controlKeyShiftSemitones = next.keyShiftSemitones;
	st.pitch = next.tempoRatio;
	st.master_tempo_enabled = next.masterTempoEnabled;
	return next;
}

/** Apply a validated key transposition through the same revisioned Signalsmith
 * schedule path as tempo and Master Tempo. A paused deck stores the next DSP
 * value; a live deck preserves its presented transport projection. */
async function _setDeckKeyShift(deck: DeckId, keyShiftSemitones: number): Promise<void> {
	const { st, rt } = _requireLoaded(deck, 'set key shift');
	_assertKeyShift(keyShiftSemitones);
	const plan = planKeyShiftMutation(
		{
			playing: st.playing,
			audible: st.audible,
			controlActive: rt.controlActive,
			pendingScheduleCount: rt.pending.length,
			scheduleIntentCount: rt.scheduleIntentCount
		},
		rt.desiredActive,
		keyShiftSemitones
	);
	if (plan.kind === 'immediate') {
		if (plan.publishedKeyShiftSemitones === null) {
			throw new Error('immediate key shift plan omitted its public value');
		}
		st.key_shift_semitones = plan.publishedKeyShiftSemitones;
		_applyPausedDeckControlSettings(st, rt, {
			keyShiftSemitones: plan.publishedKeyShiftSemitones
		});
		return;
	}
	if (_ctx === null) throw new Error('set key shift: audio graph not initialised');
	await _scheduleDeck(
		deck,
		_futureScheduleTime(deck),
		(effectiveWhen) => _projectPositionAt(deck, effectiveWhen),
		plan.active,
		undefined,
		undefined,
		undefined,
		keyShiftSemitones
	);
}

export interface KeyShiftMutationPlan {
	kind: 'immediate' | 'scheduled';
	active: boolean;
	publishedKeyShiftSemitones: number | null;
}

/** A stop acknowledged by the processor can remain audible at the output.
 * Key changes must join that revisioned schedule instead of publishing ahead
 * of the listener. */
export function planKeyShiftMutation(
	activity: TransportMutationActivity,
	desiredActive: boolean,
	requestedKeyShiftSemitones: number
): KeyShiftMutationPlan {
	if (typeof desiredActive !== 'boolean') {
		throw new TypeError('key shift desired active must be boolean');
	}
	_assertKeyShift(requestedKeyShiftSemitones);
	if (transportNeedsScheduledMutation(activity)) {
		return {
			kind: 'scheduled',
			active: desiredActive,
			publishedKeyShiftSemitones: null
		};
	}
	return {
		kind: 'immediate',
		active: false,
		publishedKeyShiftSemitones: requestedKeyShiftSemitones
	};
}

function _desiredKeyShiftSemitones(deck: DeckId): number {
	const rt = _rt[deck];
	return rt.pending[rt.pending.length - 1]?.keyShiftSemitones ?? rt.controlKeyShiftSemitones;
}

function _effectiveAudibleSemitones(deck: DeckId): number {
	const st = deckStates[deck];
	const rt = _rt[deck];
	return keySyncEffectiveAudibleSemitones({
		audible: st.audible,
		transportPending: st.transport_pending,
		pendingMutation: rt.pending.length > 0 || rt.scheduleIntentCount > 0,
		control: {
			tempoRatio: rt.controlTempoRatio,
			masterTempoEnabled: rt.controlMasterTempoEnabled,
			keyShiftSemitones: rt.controlKeyShiftSemitones
		},
		presentation: rt.presentation
	});
}

function _clearSlip(deck: DeckId): void {
	const st = deckStates[deck];
	st.slip_active = false;
	st.slip_position_ms = null;
	_rt[deck].slipAnchor = null;
}

function _activateSlip(deck: DeckId, anchor: SlipAnchor): void {
	const { st, rt } = _requireLoaded(deck, 'activate SLIP');
	if (!shouldActivateSlip(st.playing, st.slip_enabled)) {
		throw new Error('SLIP can only activate on a playing deck with SLIP enabled');
	}
	rt.slipAnchor = createSlipAnchor(anchor);
	st.slip_active = true;
	st.slip_position_ms = slipHiddenPositionSec(rt.slipAnchor, _ctx?.currentTime ?? anchor.startContextTime) * 1000;
}

function _updateSlipPosition(deck: DeckId): void {
	const st = deckStates[deck];
	const anchor = _rt[deck].slipAnchor;
	if (!st.slip_active || anchor === null) return;
	if (!st.playing) {
		_clearSlip(deck);
		return;
	}
	if (_ctx === null) throw new Error('SLIP active without an AudioContext');
	st.slip_position_ms = slipHiddenPositionSec(anchor, _ctx.currentTime) * 1000;
}

/** Resume an active SLIP loop through the same acknowledged processor and
 * revision timeline used by ordinary transport mutations. */
async function _resumeSlip(deck: DeckId): Promise<void> {
	const { st, rt } = _requireLoaded(deck, 'resume SLIP');
	const anchor = rt.slipAnchor;
	if (!st.slip_active || anchor === null) throw new Error('resume SLIP requires an active hidden playhead');
	if (!st.playing) throw new Error('resume SLIP requires a playing deck');
	if (_ctx === null) throw new Error('resume SLIP: audio graph not initialised');
	await _scheduleDeck(
		deck,
		_futureScheduleTime(deck),
		(effectiveWhen) => slipHiddenPositionSec(anchor, effectiveWhen),
		true,
		undefined,
		undefined,
		null
	);
	_clearSlip(deck);
}

function _positionForSegment(segment: _ClockSegment, at: number, durationSec: number): number {
	if (!segment.active) return segment.startPositionSec;
	const elapsed = Math.max(0, at - segment.startContextTime);
	const linear = segment.startPositionSec + elapsed * segment.tempoRatio;
	const loop = segment.loop;
	if (loop !== null && loop.engaged) {
		const loopStart = loop.in_ms / 1000;
		const loopEnd = loop.out_ms / 1000;
		if (linear >= loopEnd) return loopStart + ((linear - loopStart) % (loopEnd - loopStart));
	}
	return Math.min(linear, durationSec);
}

function _pendingClockSegment(pending: _PendingSegment): _ClockSegment {
	return {
		active: pending.active,
		loop: pending.loop,
		startContextTime: pending.startContextTime,
		startPositionSec: pending.startPositionSec,
		tempoRatio: pending.tempoRatio,
		masterTempoEnabled: pending.masterTempoEnabled ?? true,
		keyShiftSemitones: pending.keyShiftSemitones ?? 0
	};
}

function _controlSegmentAt(rt: _DeckRuntime, contextTime: number): _ClockSegment {
	let selected: _ClockSegment = {
		active: rt.controlActive,
		loop: rt.controlLoop,
		startContextTime: rt.startCtxTime,
		startPositionSec: rt.startOffsetSec,
		tempoRatio: rt.controlTempoRatio,
		masterTempoEnabled: rt.controlMasterTempoEnabled,
		keyShiftSemitones: rt.controlKeyShiftSemitones
	};
	for (const pending of rt.pending) {
		if (pending.startContextTime > contextTime) break;
		selected = _pendingClockSegment(pending);
	}
	return selected;
}

function _commitPendingIfDue(deck: DeckId): void {
	if (_ctx === null) return;
	const rt = _rt[deck];
	while (
		rt.pending.length > 0 &&
		_ctx.currentTime >= rt.pending[0].startContextTime
	) {
		const pending = rt.pending.shift();
		if (pending === undefined) throw new Error(`deck ${deck} pending schedule queue underflow`);
		rt.startCtxTime = pending.startContextTime;
		rt.startOffsetSec = pending.startPositionSec;
		rt.controlActive = pending.active;
		rt.controlLoop = pending.loop === null ? null : { ...pending.loop };
		rt.controlTempoRatio = pending.tempoRatio;
		rt.controlMasterTempoEnabled = pending.masterTempoEnabled ?? true;
		rt.controlKeyShiftSemitones = pending.keyShiftSemitones ?? 0;
	}
}

function _projectPositionAt(deck: DeckId, when: number): number {
	if (_ctx === null) throw new Error('_projectPositionAt: audio graph not initialised');
	const rt = _rt[deck];
	_commitPendingIfDue(deck);
	return _positionForSegment(_controlSegmentAt(rt, when), when, rt.durationSec);
}

function _futureScheduleTime(deck: DeckId): number {
	if (_ctx === null) throw new Error('_futureScheduleTime: audio graph not initialised');
	const rt = _rt[deck];
	const minimumSafeWhen = safeTransportScheduleTime(_ctx.currentTime, rt.latencySec);
	const latestPending = rt.pending[rt.pending.length - 1] ?? null;
	return supersedingScheduleTime(
		minimumSafeWhen,
		latestPending?.startContextTime ?? null,
		minimumSafeWhen
	);
}

function _tempoAt(deck: DeckId, contextTime: number): number {
	return _controlSegmentAt(_rt[deck], contextTime).tempoRatio;
}

/** Render/control-clock position in seconds, with manual loop wrap. This is
 * never published directly to DeckState while audio is active. */
function _currentPosSec(deck: DeckId): number {
	const st = deckStates[deck];
	const rt = _rt[deck];
	if (_ctx === null) return st.position_ms / 1000;
	_commitPendingIfDue(deck);
	return _positionForSegment(
		_controlSegmentAt(rt, _ctx.currentTime),
		_ctx.currentTime,
		rt.durationSec
	);
}

function _readOutputTimestamp(context: AudioContext): {
	contextTime: number;
	performanceTime: number;
} {
	if (typeof context.getOutputTimestamp !== 'function') {
		throw new Error('AudioContext.getOutputTimestamp is required for presented transport');
	}
	const { contextTime, performanceTime } = context.getOutputTimestamp();
	if (contextTime === undefined || performanceTime === undefined) {
		throw new Error('AudioContext.getOutputTimestamp returned an incomplete timestamp');
	}
	return { contextTime, performanceTime };
}

function _publishPresentedTransport(
	deck: DeckId,
	outputTimestamp: { contextTime: number; performanceTime: number }
): PresentedTransportObservation | null {
	const rt = _rt[deck];
	if (rt.processor === null || rt.durationSec <= 0) return null;
	const observation = observePresentedTransportTimeline(
		rt.presentation,
		outputTimestamp,
		rt.durationSec
	);
	if (!observation.accepted) return observation;
	const st = deckStates[deck];
	const wasAudible = st.audible;
	st.position_ms = observation.position_sec * 1000;
	st.audible = observation.audible;
	st.transport_pending = observation.transport_pending;
	const presentedKeyShift = presentedKeyShiftSemitonesAt(
		rt.presentation,
		outputTimestamp.contextTime
	);
	if (presentedKeyShift !== null) st.key_shift_semitones = presentedKeyShift;
	if (wasAudible !== observation.audible) {
		_handleAudibleTransition(deck, wasAudible, observation.audible);
	}
	if (naturalEndNeedsRevisionedStop(st.playing, observation, rt.durationSec, rt.scheduleIntentCount)) {
		if (_ctx === null) throw new Error('natural-end cleanup requires an AudioContext');
		void _scheduleDeck(
			deck,
			safeTransportScheduleTime(_ctx.currentTime, rt.latencySec),
			rt.durationSec,
			false
		)
			.then(() => {
				if (_masterDeck === deck && !st.audible) _electPlayingMaster();
			})
			.catch((error: unknown) => {
				if (rt.processor !== null) _recordProcessorFailure(deck, error);
			});
	}
	return observation;
}

function _tick(): void {
	_rafId = null;
	if (_ctx === null) return;
	const outputTimestamp = _readOutputTimestamp(_ctx);
	let anyTransport = false;
	for (const deck of DECK_IDS) {
		_commitPendingIfDue(deck);
		const observation = _publishPresentedTransport(deck, outputTimestamp);
		_updateSlipPosition(deck);
		if (observation?.audible || observation?.transport_pending) anyTransport = true;
	}
	if (anyTransport) _rafId = requestAnimationFrame(_tick);
}

function _ensureRaf(): void {
	if (_rafId === null) _rafId = requestAnimationFrame(_tick);
}

function _hotCuesFrom(cues: AnlzCue[]): HotCue[] {
	return cues
		.filter((c): c is AnlzCue & { slot: NonNullable<AnlzCue['slot']> } => c.slot !== null)
		.map((c) => ({
			slot: c.slot,
			in_ms: c.in_ms,
			out_ms: c.out_ms,
			is_loop: c.is_loop,
			color_table_index: c.color_table_index,
			comment: c.comment
		}))
		.sort((a, b) => a.slot.localeCompare(b.slot));
}

/** Display-only stored loop (COMPONENT-MAP 1.3: loop chips are display at
 * v1): the rekordbox active loop when one exists, engaged: false. */
function _displayLoopFrom(cues: AnlzCue[]): LoopState | null {
	const active = cues.find((c) => c.active_loop && c.out_ms !== null);
	if (active === undefined || active.out_ms === null) return null;
	return {
		in_ms: active.in_ms,
		out_ms: active.out_ms,
		engaged: false,
		beat_length: active.beat_loop_size
	};
}

function _clearLoadedTrackState(st: DeckState): void {
	st.stable_id = null;
	st.title = null;
	st.artist = null;
	st.bpm = null;
	st.key = null;
	st.key_shift_semitones = 0;
	st.duration_ms = null;
	st.position_ms = 0;
	st.playing = false;
	st.audible = false;
	st.transport_pending = false;
	st.cue_ms = null;
	st.pitch = 1;
	st.stems = unavailableStemDeckState();
	st.slip_enabled = false;
	st.slip_active = false;
	st.slip_position_ms = null;
	st.loop = null;
	_rt[st.deck_id].slipAnchor = null;
	st.hot_cues = [];
	st.anlz = null;
	st.anlz_error = null;
}

function _tempoBounds(deck: DeckId): { min: number; max: number } {
	const range = pitchRanges[deck] / 100;
	return { min: Math.max(0.01, 1 - range), max: 1 + range };
}

async function _resumeContext(): Promise<AudioContext> {
	const ctx = _ensureGraph();
	if (ctx.state === 'suspended') await ctx.resume();
	if (ctx.state !== 'running') {
		throw new Error(`AudioContext did not enter running state; current state is ${ctx.state}`);
	}
	return ctx;
}

export interface ContextTimeSource {
	readonly currentTime: number;
	readonly state: string;
}

export async function waitForAdvancingContextTime(
	ctx: ContextTimeSource,
	targetContextTime: number,
	stillCurrent: () => boolean = () => true,
	stallTimeoutMs: number = CONTEXT_WAIT_STALL_TIMEOUT_MS
): Promise<void> {
	if (!Number.isFinite(targetContextTime) || targetContextTime < 0) {
		throw new RangeError(
			`targetContextTime must be finite and non-negative, got ${targetContextTime}`
		);
	}
	if (!Number.isFinite(stallTimeoutMs) || stallTimeoutMs <= 0) {
		throw new RangeError(`stallTimeoutMs must be finite and positive, got ${stallTimeoutMs}`);
	}
	const initialContextTime = ctx.currentTime;
	if (!Number.isFinite(initialContextTime) || initialContextTime < 0) {
		throw new RangeError(
			`AudioContext time must be finite and non-negative, got ${initialContextTime}`
		);
	}
	let lastContextTime = initialContextTime;
	let lastProgressAtMs = Date.now();
	while (ctx.currentTime < targetContextTime) {
		if (!stillCurrent()) throw new Error('context-time wait state changed before target');
		if (ctx.state !== 'running') {
			throw new Error(`AudioContext is not running during context-time wait; got ${ctx.state}`);
		}
		const contextTime = ctx.currentTime;
		if (!Number.isFinite(contextTime) || contextTime < lastContextTime) {
			throw new Error(
				`AudioContext time must be finite and monotonic, got ${contextTime} after ${lastContextTime}`
			);
		}
		if (contextTime > lastContextTime) {
			lastContextTime = contextTime;
			lastProgressAtMs = Date.now();
		}
		const stallRemainingMs = stallTimeoutMs - (Date.now() - lastProgressAtMs);
		if (stallRemainingMs <= 0) {
			throw new Error(
				`AudioContext time stalled before target ${targetContextTime} at ${contextTime}`
			);
		}
		const contextRemainingMs = (targetContextTime - contextTime) * 1000;
		const delayMs = Math.max(
			1,
			Math.ceil(Math.min(CONTEXT_WAIT_POLL_MS, contextRemainingMs, stallRemainingMs))
		);
		await new Promise<void>((resolve) => setTimeout(resolve, delayMs));
	}
}

interface _MasterSyncSchedule {
	tempoRatio: number;
	masterTempoEnabled: boolean;
}

interface _SyncOptions {
	followerAnchorSec?: Partial<Record<DeckId, number>>;
	masterSchedule?: _MasterSyncSchedule;
}

async function _synchronizeFollowers(
	master: DeckId,
	followers: readonly DeckId[],
	options: _SyncOptions = {}
): Promise<void> {
	if (followers.length === 0 && options.masterSchedule === undefined) return;
	try {
		const ctx = await _resumeContext();
		_commitPendingIfDue(master);
		for (const deck of followers) _commitPendingIfDue(deck);
		const masterState = deckStates[master];
		const masterRuntime = _requireLoaded(master, 'Beat Sync master').rt;
		if (!masterRuntime.desiredActive) {
			throw new Error(`Beat Sync master deck ${master} is neither audible nor scheduled to play`);
		}
		const masterGrid = _requireBeatGrid(masterState, 'Beat Sync');
		const now = ctx.currentTime;
		const maxLatency = Math.max(
			masterRuntime.latencySec,
			...followers.map((deck) => _requireLoaded(deck, 'Beat Sync follower').rt.latencySec)
		);
		const projectionContextTime = Math.max(
			now,
			masterRuntime.pending[masterRuntime.pending.length - 1]?.startContextTime ??
				masterRuntime.startCtxTime
		);
		const requestedSyncAt = safeSyncScheduleTime(
			now,
			maxLatency,
			projectionContextTime
		);
		const supersededDecks =
			options.masterSchedule === undefined ? followers : [master, ...followers];
		const pendingWaitTarget = pendingSyncWaitTarget(
			requestedSyncAt,
			supersededDecks.flatMap((deck) =>
				_rt[deck].pending.map((pending) => pending.startContextTime)
			)
		);
		if (pendingWaitTarget !== null) {
			const waitState = supersededDecks.map((deck) => ({
				deck,
				loadToken: _rt[deck].loadToken,
				desiredRevision: _rt[deck].presentation.desired_revision,
				desiredActive: _rt[deck].desiredActive,
				beatSyncEnabled: deckStates[deck].beat_sync_enabled,
				syncMode: deckStates[deck].sync_mode
			}));
			await waitForAdvancingContextTime(ctx, pendingWaitTarget, () =>
				waitState.every(
					(snapshot) =>
						_rt[snapshot.deck].loadToken === snapshot.loadToken &&
						_rt[snapshot.deck].presentation.desired_revision === snapshot.desiredRevision &&
						_rt[snapshot.deck].desiredActive === snapshot.desiredActive &&
						deckStates[snapshot.deck].beat_sync_enabled === snapshot.beatSyncEnabled &&
						deckStates[snapshot.deck].sync_mode === snapshot.syncMode
				)
			);
			for (const deck of supersededDecks) _commitPendingIfDue(deck);
			return _synchronizeFollowers(master, followers, options);
		}
		const syncAt = requestedSyncAt;
		const masterPositionSec = _projectPositionAt(master, syncAt);
		const masterTempoRatio = options.masterSchedule?.tempoRatio ?? _tempoAt(master, syncAt);
		const planned = followers.map((deck) => {
			const { st } = _requireLoaded(deck, 'Beat Sync follower');
			const followerGrid = _requireBeatGrid(st, 'Beat Sync');
			const bounds = _tempoBounds(deck);
			const rawFollowerPositionSec = _currentPosSec(deck);
			const requestedAnchorSec = options.followerAnchorSec?.[deck];
			const followerPositionSec =
				requestedAnchorSec ??
				(_rt[deck].controlActive
					? _projectPositionAt(deck, syncAt)
					: quantizeToNearestBeat(followerGrid, rawFollowerPositionSec));
			const plan = computeFollowerSyncPlan({
				masterGrid,
				followerGrid,
				masterPositionAtSyncSec: masterPositionSec,
				masterTempoRatio,
				followerPositionSec,
				currentContextTimeSec: now,
				syncAtContextTimeSec: syncAt,
				minFollowerTempoRatio: bounds.min,
				maxFollowerTempoRatio: bounds.max,
				mode: st.sync_mode
			});
			return { deck, st, plan };
		});
		const schedules = [
			...(options.masterSchedule === undefined
				? []
				: [
						{
							deck: master,
							st: masterState,
							inputSec: masterPositionSec,
							tempoRatio: options.masterSchedule.tempoRatio,
							masterTempoEnabled: options.masterSchedule.masterTempoEnabled
						}
					]),
			...planned.map((item) => ({
				deck: item.deck,
				st: item.st,
				inputSec: item.plan.followerPositionSec,
				tempoRatio: item.plan.followerTempoRatio,
				masterTempoEnabled: item.st.master_tempo_enabled
			}))
		];
		const scheduleTimes = commonSyncScheduleTimes(syncAt, schedules.length);
		const outcomes = await Promise.allSettled(
			schedules.map((item, index) =>
				_scheduleDeck(
					item.deck,
					scheduleTimes[index],
					item.inputSec,
					true,
					item.tempoRatio,
					item.masterTempoEnabled
				)
			)
		);
		const failedDecks = outcomes.flatMap((outcome, index) =>
			outcome.status === 'rejected' ? [schedules[index].deck] : []
		);
		if (failedDecks.length > 0) {
			const succeededDecks = schedules
				.map((item) => item.deck)
				.filter((deck) => !failedDecks.includes(deck));
			const message =
				`Beat Sync partial failure: succeeded [${succeededDecks.join(',')}], ` +
				`failed [${failedDecks.join(',')}]`;
			for (const item of schedules) item.st.sync_error = message;
			throw new Error(message, {
				cause: outcomes.find((outcome) => outcome.status === 'rejected')
			});
		}
		for (const item of schedules) item.st.sync_error = null;
	} catch (error) {
		for (const deck of followers) deckStates[deck].sync_error = String(error);
		if (options.masterSchedule !== undefined) deckStates[master].sync_error = String(error);
		throw error;
	}
}

async function _withDeckSwap<T>(rt: _DeckRuntime, swap: () => Promise<T>): Promise<T> {
	const predecessor = rt.swapTail;
	let release!: () => void;
	rt.swapTail = new Promise<void>((resolve) => {
		release = resolve;
	});
	await predecessor;
	try {
		return await swap();
	} finally {
		release();
	}
}

// ------------------------------------------------------------- the engine

/** Singleton Web Audio engine implementing the AudioEngine contract, plus
 * the extras the topbar/deck units need (setMaster, pressCue, beat loops,
 * pitch ranges). All methods fail fast: invalid input or a missing backing
 * track throws; backend 404s reject with their explicit code. */
class RbAudioEngine implements AudioEngine {
	/**
	 * Tear down every resource owned by the /performance route. In-flight
	 * loads are invalidated before state is reset, so a stale candidate cannot
	 * publish after navigation.
	 */
	async dispose(): Promise<void> {
		const processors: _DeckProcessor[] = [];
		const nodes: AudioNode[] = [];
		for (const deck of DECK_IDS) {
			const rt = _rt[deck];
			rt.loadToken += 1;
			const processor = detachProcessorForDisposal(rt);
			if (processor !== null) processors.push(processor);
			if (rt.nodes !== null) nodes.push(...Object.values(rt.nodes));
		}
		_headphoneGeneration += 1;
		_disposeHeadphoneGraph();
		const closing = disposeAudioResources({
			rafId: _rafId,
			processors,
			nodes,
			masterGain: _masterGain,
			context: _ctx
		});

		_rafId = null;
		_masterGain = null;
		_ctx = null;
		_masterDeck = null;
		for (const deck of DECK_IDS) {
			_rt[deck] = _emptyRuntime();
			deckStates[deck] = _emptyDeckState(deck);
			deckLoadErrors[deck] = null;
			pitchRanges[deck] = 16;
			mixerState.channels[deck] = _defaultChannel(deck);
		}
		mixerState.crossfader = 0.5;
		mixerState.master = 1;
		mixerState.headphones = _defaultHeadphones();
		await closing;
	}

	async load(deck: DeckId, stable_id: string): Promise<void> {
		if (stable_id.length === 0) throw new Error('load: stable_id must be non-empty');
		const st = deckStates[deck];
		const rt = _rt[deck];
		_assertCurrentDeckReplacementAllowed(deck);
		const token = ++rt.loadToken;
		deckLoadErrors[deck] = null;
		let track: Track | null = null;
		let buffer: AudioBuffer | null = null;
		let anlz: DeckState['anlz'] = null;
		let latencySec = 0;
		let processor: _DeckProcessor | null = null;
		let candidateStemState: StemDeckState = unavailableStemDeckState();
		try {
			const [trackRes, audioBytes, requiredAnlz] = await Promise.all([
				getTrack(stable_id),
				fetchAudioArrayBuffer(stable_id),
				fetchAnlz(stable_id)
			]);
			const stemProbe = await probeStemArtifact(stable_id);
			const ctx = _ensureGraph();
			track = trackRes.track;
			anlz = requiredAnlz;
			buffer = await ctx.decodeAudioData(audioBytes);
			const processorOptions = {
				onProcessorError: (error: unknown) => {
					if (_rt[deck].processor === processor) _recordProcessorFailure(deck, error);
				}
			};
			if (stemProbe.status === 'ready') {
				const encodedParts = await fetchStemAudioArrayBuffers(stable_id);
				const decodedEntries = await Promise.all(
					DEMUCS_PARTS.map(async (part) => [part, await ctx.decodeAudioData(encodedParts[part])] as const)
				);
				const stemBuffers = Object.fromEntries(decodedEntries) as Record<
					DemucsStemPart,
					AudioBuffer
				> as StemBuffers;
				const created = await AlignedStemDeckProcessor.create(ctx, stemBuffers, processorOptions);
				if (
					created.alignment.sample_rate_hz !== buffer.sampleRate ||
					created.alignment.frame_count !== buffer.length ||
					Math.abs(created.alignment.duration_ms - buffer.duration * 1000) >
						1000 / buffer.sampleRate
				) {
					created.processor.disconnect();
					throw new Error(
						`stem/source alignment mismatch: source ${buffer.sampleRate}Hz, ${buffer.length} ` +
							`frames, ${buffer.duration}s; stems ${created.alignment.sample_rate_hz}Hz, ` +
							`${created.alignment.frame_count} frames, ${created.alignment.duration_ms / 1000}s`
					);
				}
				processor = created.processor;
				candidateStemState = readyStemDeckState(
					{ source: stemProbe.manifest.source, model: stemProbe.manifest.model },
					created.alignment
				);
			} else {
				const mixProcessor = await StretchDeckProcessor.create(ctx, processorOptions);
				await mixProcessor.load(buffer);
				processor = mixProcessor;
				candidateStemState = unavailableStemDeckState(stemProbe.error);
			}
			latencySec = await processor.latencySec();
		} catch (exc) {
			if (processor !== null) processor.disconnect();
			if (token !== rt.loadToken) throw exc;
			assertDeckLoadConsistency(st.stable_id, rt.durationSec, rt.processor !== null);
			const msg =
				exc instanceof RbApiError ? `${exc.code}: ${exc.message}` : String(exc);
			deckLoadErrors[deck] = msg;
			pushToast(`Deck ${deck} load failed - ${msg}`, 'error');
			throw exc;
		}
		if (processor === null || track === null || buffer === null || anlz === null) {
			throw new Error('load: candidate deck transaction is incomplete');
		}
		const candidateProcessor = processor;
		const candidateTrack = track;
		const candidateBuffer = buffer;
		const candidateAnlz = anlz;
		await _withDeckSwap(rt, async () => {
			// Winner check through state publication is one synchronous JS turn.
			// Do not insert an await before rt.processor receives the candidate.
			if (!loadCandidateCanPublish(token, rt.loadToken)) {
				candidateProcessor.disconnect();
				return;
			}
			try {
				_assertCurrentDeckReplacementAllowed(deck);
			} catch (error) {
				candidateProcessor.disconnect();
				throw error;
			}
			if (rt.nodes === null || _ctx === null) {
				candidateProcessor.disconnect();
				throw new Error(`load: deck ${deck} audio graph is missing`);
			}
			const context = _ctx;
			const replacingMaster = _masterDeck === deck;
			const incumbentProcessor = rt.processor;
			try {
				candidateProcessor.connect(rt.nodes.analyser);
			} catch (error) {
				candidateProcessor.disconnect();
				if (loadCandidateCanPublish(token, rt.loadToken)) {
					const message = String(error);
					deckLoadErrors[deck] = message;
					pushToast(`Deck ${deck} load failed - ${message}`, 'error');
				}
				throw error;
			}
			_clearLoadedTrackState(st);
			rt.processor = candidateProcessor;
			rt.durationSec = candidateBuffer.duration;
			rt.latencySec = latencySec;
			rt.controlActive = false;
			rt.controlLoop = null;
			rt.controlTempoRatio = 1;
			rt.controlMasterTempoEnabled = true;
			rt.controlKeyShiftSemitones = 0;
			rt.startCtxTime = 0;
			rt.startOffsetSec = 0;
			rt.pending = [];
			rt.presentation = createPresentedTransportTimeline(0);
			rt.nextScheduleRevision = 0;
			rt.desiredActive = false;
			st.stable_id = stable_id;
			st.title = candidateTrack.title;
			st.artist = candidateTrack.artist;
			st.bpm = candidateTrack.bpm;
			st.key = candidateTrack.key;
			// The decoded buffer is the audio actually scheduled. Metadata can
			// differ, so it must not define waveform bounds or transport truth.
			st.duration_ms = decodedTransportDurationMs(candidateBuffer.duration);
			st.anlz = candidateAnlz;
			st.anlz_error = null;
			st.processor_error = null;
			st.sync_error = null;
			st.stems = candidateStemState;
			st.hot_cues = _hotCuesFrom(candidateAnlz.cues);
			st.loop = _displayLoopFrom(candidateAnlz.cues);
			if (replacingMaster) _electPlayingMaster();
			assertDeckLoadConsistency(st.stable_id, rt.durationSec, rt.processor !== null);
			if (incumbentProcessor !== null) {
				incumbentProcessor.disconnect();
				try {
					await incumbentProcessor.stop(context.currentTime);
				} catch (error) {
					const message = error instanceof Error ? error.message : String(error);
					pushToast(`Deck ${deck} retired processor cleanup failed - ${message}`, 'error');
				}
			}
		});
	}

	async play(deck: DeckId): Promise<void> {
		const { st, rt } = _requireLoaded(deck, 'play');
		if (rt.desiredActive) return; // transport already running is a valid state
		if (st.beat_sync_enabled) _requireBeatGrid(st, 'play Beat Sync');
		const needsScheduledMutation = transportNeedsScheduledMutation({
			playing: st.playing,
			audible: st.audible,
			controlActive: rt.controlActive,
			pendingScheduleCount: rt.pending.length,
			scheduleIntentCount: rt.scheduleIntentCount
		});
		if (!needsScheduledMutation) _setPausedPosition(deck, st.position_ms);
		const ctx = await _resumeContext();
		const startSec: number | ((effectiveWhen: number) => number) = needsScheduledMutation
			? (effectiveWhen) => _projectPositionAt(deck, effectiveWhen)
			: st.position_ms / 1000;
		const activeMaster = _syncMaster();
		if (activeMaster === null) {
			const when = ctx.currentTime + _rt[deck].latencySec + SYNC_SCHEDULE_SAFETY_S;
			try {
				await _scheduleDeck(deck, when, startSec, true);
				_assignMaster(deck);
				st.sync_error = null;
			} catch (error) {
				st.sync_error = String(error);
				throw error;
			}
		} else if (activeMaster === deck || !st.beat_sync_enabled) {
			const when = ctx.currentTime + _rt[deck].latencySec + SYNC_SCHEDULE_SAFETY_S;
			await _scheduleDeck(deck, when, startSec, true);
			st.sync_error = null;
		} else {
			await _synchronizeFollowers(activeMaster, [deck]);
		}
	}

	async pause(deck: DeckId): Promise<void> {
		const { st, rt } = _requireLoaded(deck, 'pause');
		if (!rt.desiredActive) return; // already paused is a valid state
		if (_ctx === null) throw new Error('pause: audio graph not initialised');
		const pauseBeats = st.quantize_enabled ? _requireBeatGrid(st, 'pause cue') : null;
		const when = _futureScheduleTime(deck);
		const positionSec = await _scheduleDeck(
			deck,
			when,
			(effectiveWhen) => _projectPositionAt(deck, effectiveWhen),
			false
		);
		const cueMs = pauseBeats !== null
			? quantizedPositionMs(pauseBeats, positionSec * 1000, true)
			: positionSec * 1000;
		st.cue_ms = cueMs;
		if (st.slip_active) _clearSlip(deck);
	}

	async cueJump(deck: DeckId, ms: number): Promise<void> {
		await this.quantizedSeek(deck, ms);
	}

	async quantizedSeek(deck: DeckId, ms: number): Promise<void> {
		const { st, rt } = _requireLoaded(deck, 'cueJump');
		const durMs = _durationSec(deck) * 1000;
		if (!Number.isFinite(ms) || ms < 0 || ms > durMs) {
			throw new RangeError(`cueJump: ms must be within 0..${Math.round(durMs)}, got ${ms}`);
		}
		const targetMs = st.quantize_enabled
			? quantizedPositionMs(_requireBeatGrid(st, 'cueJump quantize'), ms, true)
			: ms;
		if (targetMs > durMs) {
			throw new RangeError(`cueJump: quantized target ${targetMs} exceeds duration ${durMs}`);
		}
		const needsScheduledMutation = transportNeedsScheduledMutation({
			playing: rt.desiredActive,
			audible: st.audible,
			controlActive: rt.controlActive,
			pendingScheduleCount: rt.pending.length,
			scheduleIntentCount: rt.scheduleIntentCount
		});
		if (needsScheduledMutation) {
			const master = seekSyncMaster(
				deck,
				rt.desiredActive,
				st.beat_sync_enabled,
				_syncMaster()
			);
			if (master !== null) {
				await _synchronizeFollowers(master, [deck], {
					followerAnchorSec: { [deck]: targetMs / 1000 }
				});
			} else {
				if (_ctx === null) throw new Error('cueJump: audio graph not initialised');
				await _scheduleDeck(
					deck,
					_futureScheduleTime(deck),
					targetMs / 1000,
					rt.desiredActive
				);
			}
		} else {
			_setPausedPosition(deck, targetMs);
		}
	}

	/** The physical CUE button. Playing: return to the cue point and pause.
	 * Paused with a cue set: jump the playhead to it. Paused with no cue:
	 * set the cue at the current position. */
	async pressCue(deck: DeckId): Promise<void> {
		const { st } = _requireLoaded(deck, 'pressCue');
		if (st.playing) {
			const target = st.cue_ms ?? 0;
			if (_ctx === null) throw new Error('pressCue: audio graph not initialised');
			await _scheduleDeck(deck, _futureScheduleTime(deck), target / 1000, false);
			return;
		}
		if (st.cue_ms === null) {
			st.cue_ms = st.quantize_enabled
				? quantizedPositionMs(_requireBeatGrid(st, 'pressCue quantize'), st.position_ms, true)
				: st.position_ms;
		} else {
			await this.quantizedSeek(deck, st.cue_ms);
		}
	}

	async setPitch(deck: DeckId, ratio: number): Promise<void> {
		await this.setTempoRatio(deck, ratio);
	}

	async setTempoRatio(deck: DeckId, ratio: number): Promise<void> {
		const { st, rt } = _requireLoaded(deck, 'setTempoRatio');
		if (!Number.isFinite(ratio) || ratio <= 0) {
			throw new RangeError(`setTempoRatio: ratio must be > 0, got ${ratio}`);
		}
		const rangePct = pitchRanges[deck];
		if (Math.abs(ratio - 1) * 100 > rangePct + 1e-9) {
			throw new RangeError(
				`setTempoRatio: ratio ${ratio} outside the selected +-${rangePct}% range on deck ${deck}`
			);
		}
		const activeMaster = _syncMaster();
		if (st.playing && st.beat_sync_enabled && activeMaster !== null && activeMaster !== deck) {
			throw new Error(`setTempoRatio: disable Beat Sync before changing follower deck ${deck}`);
		}
		if (st.playing) {
			if (_ctx === null) throw new Error('setTempoRatio: audio graph not initialised');
			const followers =
				_masterDeck === deck
					? DECK_IDS.filter(
							(candidate) =>
								candidate !== deck &&
								deckStates[candidate].playing &&
								deckStates[candidate].beat_sync_enabled
						)
					: [];
			if (followers.length > 0) {
				await _synchronizeFollowers(deck, followers, {
					masterSchedule: {
						tempoRatio: ratio,
						masterTempoEnabled: st.master_tempo_enabled
					}
				});
			} else {
				const when = _futureScheduleTime(deck);
				await _scheduleDeck(
					deck,
					when,
					(effectiveWhen) => _projectPositionAt(deck, effectiveWhen),
					true,
					ratio
				);
			}
		} else {
			_applyPausedDeckControlSettings(st, rt, { tempoRatio: ratio });
		}
	}

	/** Select the pitch fader range. Throws when the current pitch no longer
	 * fits - reset pitch toward 1.0 first (explicit, never a silent clamp). */
	setPitchRange(deck: DeckId, range: PitchRange): void {
		if (!PITCH_RANGES.includes(range)) {
			throw new RangeError(`setPitchRange: invalid range ${range}`);
		}
		const st = deckStates[deck];
		if (Math.abs(st.pitch - 1) * 100 > range + 1e-9) {
			throw new RangeError(
				`setPitchRange: current pitch ${st.pitch} exceeds +-${range}% - reset pitch first`
			);
		}
		pitchRanges[deck] = range;
	}

	async setLoop(deck: DeckId, loop: { in_ms: number; out_ms: number } | null): Promise<void> {
		const { st } = _requireLoaded(deck, 'setLoop');
		const wasPlaying = st.playing;
		const scheduleAt = wasPlaying ? _futureScheduleTime(deck) : 0;
		if (loop === null) {
			if (st.slip_active) {
				await _resumeSlip(deck);
				return;
			}
			if (wasPlaying) {
				if (_ctx === null) throw new Error('setLoop: audio graph not initialised');
				await _scheduleDeck(
					deck,
					scheduleAt,
					(effectiveWhen) => _projectPositionAt(deck, effectiveWhen),
					true,
					undefined,
					undefined,
					null
				);
			} else {
				st.loop = null;
			}
			return;
		}
		const durMs = _durationSec(deck) * 1000;
		const snapped = st.quantize_enabled
			? quantizedLoopEndpointsMs(_requireBeatGrid(st, 'setLoop quantize'), loop, true)
			: quantizedLoopEndpointsMs([], loop, false);
		const bounded = loopEndpointsWithinDurationMs(snapped, durMs);
		const nextLoop: LoopState = { ...bounded, engaged: true, beat_length: null };
		if (wasPlaying) {
			if (_ctx === null) throw new Error('setLoop: audio graph not initialised');
			const activateSlip = shouldActivateSlip(st.playing, st.slip_enabled) && !st.slip_active;
			let slipAnchor: SlipAnchor | null = null;
			await _scheduleDeck(
				deck,
				scheduleAt,
				(effectiveWhen) => {
					const positionSec = _projectPositionAt(deck, effectiveWhen);
					if (activateSlip) {
						slipAnchor = createSlipAnchor({
							startContextTime: effectiveWhen,
							startPositionSec: positionSec,
							tempoRatio: _tempoAt(deck, effectiveWhen),
							durationSec: _durationSec(deck)
						});
					}
					return positionSec;
				},
				true,
				undefined,
				undefined,
				nextLoop
			);
			if (activateSlip) {
				if (slipAnchor === null) throw new Error('SLIP loop schedule did not produce an anchor');
				_activateSlip(deck, slipAnchor);
			}
		} else {
			st.loop = nextLoop;
		}
	}

	/** Engage a beat loop using exact consecutive PQTZ timestamps. */
	async engageBeatLoop(deck: DeckId, beats: number, startMs?: number): Promise<void> {
		const { st } = _requireLoaded(deck, 'engageBeatLoop');
		const grid = _requireBeatGrid(st, 'engageBeatLoop');
		const currentMs = st.playing ? _currentPosSec(deck) * 1000 : st.position_ms;
		const range = exactBeatLoopRangeMs(grid, currentMs, beats, startMs);
		await this.setLoop(deck, range);
		if (st.loop !== null) st.loop.beat_length = beats;
		const pending = _rt[deck].pending;
		const pendingLoop = pending[pending.length - 1]?.loop;
		if (pendingLoop !== null && pendingLoop !== undefined) {
			pendingLoop.beat_length = beats;
		}
	}

	setQuantize(deck: DeckId, enabled: boolean): void {
		if (typeof enabled !== 'boolean') throw new TypeError('setQuantize: enabled must be boolean');
		deckStates[deck].quantize_enabled = enabled;
	}

	setBeatSync(deck: DeckId, enabled: boolean): Promise<void> {
		if (typeof enabled !== 'boolean') throw new TypeError('setBeatSync: enabled must be boolean');
		const st = deckStates[deck];
		st.beat_sync_enabled = enabled;
		if (!enabled) {
			st.sync_error = null;
			return Promise.resolve();
		}
		if (!_rt[deck].desiredActive) return Promise.resolve();
		const master = _syncMaster();
		if (master === null) {
			_assignMaster(deck);
			return Promise.resolve();
		}
		return syncChangeRequiresReschedule(deck, true, enabled, master)
			? _synchronizeFollowers(master, [deck])
			: Promise.resolve();
	}

	async setMasterTempo(deck: DeckId, enabled: boolean): Promise<void> {
		if (typeof enabled !== 'boolean') throw new TypeError('setMasterTempo: enabled must be boolean');
		const st = deckStates[deck];
		if (!st.playing) {
			_applyPausedDeckControlSettings(st, _rt[deck], { masterTempoEnabled: enabled });
			return;
		}
		if (_ctx === null) throw new Error('setMasterTempo: audio graph not initialised');
		const when = _futureScheduleTime(deck);
		await _scheduleDeck(
			deck,
			when,
			(effectiveWhen) => _projectPositionAt(deck, effectiveWhen),
			true,
			undefined,
			enabled
		);
	}

	async setSlip(deck: DeckId, enabled: boolean): Promise<void> {
		if (typeof enabled !== 'boolean') throw new TypeError('setSlip: enabled must be boolean');
		const st = deckStates[deck];
		if (enabled) {
			// Arming SLIP is state-only. It never schedules or publishes a transport change.
			st.slip_enabled = true;
			return;
		}
		if (st.slip_active) await _resumeSlip(deck);
		st.slip_enabled = false;
	}

	async nudgeKey(deck: DeckId, semitones: -1 | 1): Promise<void> {
		if (semitones !== -1 && semitones !== 1) {
			throw new RangeError(`nudgeKey: semitones must be -1 or 1, got ${semitones}`);
		}
		_requireLoaded(deck, 'nudgeKey');
		await _setDeckKeyShift(deck, _desiredKeyShiftSemitones(deck) + semitones);
	}

	async syncKey(deck: DeckId): Promise<void> {
		const { st } = _requireLoaded(deck, 'syncKey');
		const masterDeck = _masterDeck;
		if (masterDeck === null) throw new Error('KEY SYNC requires an elected loaded master deck');
		if (masterDeck === deck) throw new Error('KEY SYNC cannot be applied to the selected master deck');
		const { st: master } = _requireLoaded(masterDeck, 'KEY SYNC master');
		const deckManualShiftSemitones = _desiredKeyShiftSemitones(deck);
		const nudge = deriveKeySyncNudge(
			st.key,
			master.key,
			_effectiveAudibleSemitones(deck),
			_effectiveAudibleSemitones(masterDeck),
			deckManualShiftSemitones
		);
		await _setDeckKeyShift(deck, deckManualShiftSemitones + nudge);
	}

	setSyncMode(deck: DeckId, mode: SyncMode): Promise<void> {
		if (mode !== 'beat' && mode !== 'bar') {
			const _exhaustive: never = mode;
			throw new TypeError(`setSyncMode: invalid sync mode ${String(_exhaustive)}`);
		}
		const st = deckStates[deck];
		st.sync_mode = mode;
		const master = _syncMaster();
		if (
			!syncChangeRequiresReschedule(
				deck,
				_rt[deck].desiredActive,
				st.beat_sync_enabled,
				master
			)
		) {
			return Promise.resolve();
		}
		if (master === null) {
			throw new Error('setSyncMode: reschedule invariant requires a selected master deck');
		}
		return _synchronizeFollowers(master, [deck]);
	}

	async setDeckMaster(deck: DeckId): Promise<void> {
		const { st } = _requireLoaded(deck, 'setDeckMaster');
		if (!st.audible) {
			const blockers = pausedMasterSelectionBlockers(deck, deckStates);
			assertPausedMasterSelectionAllowed(deck, st.audible, blockers);
			_assignMaster(deck);
			return;
		}
		const followers = masterSwitchFollowers(deck, deckStates);
		await _synchronizeFollowers(deck, followers);
		_assignMaster(deck);
	}

	setStemMute(deck: DeckId, stem: StemControl, muted: boolean): void {
		if (typeof muted !== 'boolean') throw new TypeError('setStemMute: muted must be boolean');
		this._setStemControl(deck, stem, 'muted', muted);
	}

	setStemSolo(deck: DeckId, stem: StemControl, solo: boolean): void {
		if (typeof solo !== 'boolean') throw new TypeError('setStemSolo: solo must be boolean');
		this._setStemControl(deck, stem, 'solo', solo);
	}

	private _setStemControl(
		deck: DeckId,
		stem: StemControl,
		field: 'muted' | 'solo',
		value: boolean
	): void {
		if (!STEM_CONTROLS.includes(stem)) {
			throw new TypeError(`stem must be vocal, instrumental, or drums; got ${String(stem)}`);
		}
		const { st, rt } = _requireLoaded(deck, `setStem${field === 'muted' ? 'Mute' : 'Solo'}`);
		if (st.stems.status !== 'ready' || !(rt.processor instanceof AlignedStemDeckProcessor)) {
			throw new Error(
				`deck ${deck} stems are ${st.stems.status}: ${st.stems.error ?? 'no aligned artifact'}`
			);
		}
		const controls = {
			vocal: { ...st.stems.controls.vocal },
			instrumental: { ...st.stems.controls.instrumental },
			drums: { ...st.stems.controls.drums }
		};
		controls[stem][field] = value;
		rt.processor.setControls(controls);
		st.stems.controls = controls;
	}

	captureDeckAudio(deck: DeckId): DeckAudioSnapshot {
		const st = deckStates[deck];
		if (st.processor_error !== null) {
			throw new Error(`captureDeckAudio: processor failed: ${st.processor_error}`);
		}
		const { rt } = _requireLoaded(deck, 'captureDeckAudio');
		if (_ctx === null || rt.nodes === null) {
			throw new Error('captureDeckAudio: audio graph not initialised');
		}
		_publishPresentedTransport(deck, _readOutputTimestamp(_ctx));
		if (_ctx.state !== 'running' || !st.audible) {
			throw new Error('captureDeckAudio: deck must be audible and AudioContext must be running');
		}
		const presentationContextTime = rt.presentation.last_presentation_context_time_s;
		if (presentationContextTime === null) {
			throw new Error('captureDeckAudio: audio output presentation clock has not started');
		}
		const analyser = rt.nodes.analyser;
		const frequencyDb = new Float32Array(analyser.frequencyBinCount);
		const timeDomain = new Float32Array(analyser.fftSize);
		analyser.getFloatFrequencyData(frequencyDb);
		analyser.getFloatTimeDomainData(timeDomain);
		const finiteFrequencyDb = Array.from(frequencyDb, (value) =>
			Number.isFinite(value) ? value : analyser.minDecibels
		);
		const finiteTimeDomain = Array.from(timeDomain);
		if (finiteTimeDomain.some((value) => !Number.isFinite(value))) {
			throw new Error('captureDeckAudio: analyser returned non-finite time-domain samples');
		}
		const snapshot: DeckAudioSnapshot = {
			context_time_s: presentationContextTime,
			sample_rate_hz: _ctx.sampleRate,
			fft_size: analyser.fftSize,
			frequency_db: finiteFrequencyDb,
			time_domain: finiteTimeDomain
		};
		const scalarValues = [
			snapshot.context_time_s,
			snapshot.sample_rate_hz,
			snapshot.fft_size
		];
		if (scalarValues.some((value) => !Number.isFinite(value))) {
			throw new Error('captureDeckAudio: analyser metadata contains non-finite numbers');
		}
		return snapshot;
	}

	setTrim(deck: DeckId, value: number): void {
		_assertUnit('setTrim value', value);
		mixerState.channels[deck].trim = value;
		const nodes = _rt[deck].nodes;
		if (nodes !== null) _setParam(nodes.trim.gain, value * TRIM_MAX_GAIN);
	}

	setEq(deck: DeckId, band: EqBand, value: number): void {
		_assertUnit('setEq value', value);
		const ch = mixerState.channels[deck];
		const nodes = _rt[deck].nodes;
		const db = _eqDbFromKnob(value);
		if (band === 'low') {
			ch.eq_low = value;
			if (nodes !== null) _setParam(nodes.low.gain, db);
		} else if (band === 'mid') {
			ch.eq_mid = value;
			if (nodes !== null) _setParam(nodes.mid.gain, db);
		} else if (band === 'high') {
			ch.eq_high = value;
			if (nodes !== null) _setParam(nodes.high.gain, db);
		} else {
			const _exhaustive: never = band;
			throw new Error(`Unhandled EQ band: ${_exhaustive}`);
		}
	}

	setFader(deck: DeckId, value: number): void {
		_assertUnit('setFader value', value);
		mixerState.channels[deck].fader = value;
		const nodes = _rt[deck].nodes;
		if (nodes !== null) _setParam(nodes.fader.gain, value);
	}

	setCrossfader(value: number): void {
		_assertUnit('setCrossfader value', value);
		mixerState.crossfader = value;
		if (_ctx !== null) _applyCrossfader();
	}

	assignChannel(deck: DeckId, assign: CrossfaderAssign): void {
		if (assign !== 'A' && assign !== 'B' && assign !== 'THRU') {
			const _exhaustive: never = assign;
			throw new Error(`Unhandled crossfader assign: ${_exhaustive}`);
		}
		mixerState.channels[deck].assign = assign;
		if (_ctx !== null) _applyCrossfader();
	}

	setChannelCue(deck: DeckId, enabled: boolean): void {
		if (typeof enabled !== 'boolean') throw new TypeError('channel cue enabled must be boolean');
		mixerState.channels[deck].cue_enabled = enabled;
		const nodes = _rt[deck].nodes;
		if (nodes !== null) _setParam(nodes.cue.gain, enabled ? 1 : 0);
	}

	setHeadphoneMix(value: number): void {
		_assertUnit('setHeadphoneMix value', value);
		mixerState.headphones.mix = value;
		if (_headphoneNodes !== null) _applyHeadphoneMix();
	}

	setHeadphoneLevel(value: number): void {
		_assertUnit('setHeadphoneLevel value', value);
		mixerState.headphones.level = value;
		if (_headphoneNodes !== null) _applyHeadphoneMix();
	}

	async refreshHeadphoneOutputs(): Promise<void> {
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
		mixerState.headphones.error = null;
	}

	async selectHeadphoneOutput(deviceId: string): Promise<void> {
		const generation = _headphoneGeneration;
		let nodes: _HeadphoneNodes | null = null;
		let candidate: HTMLAudioElement | null = null;
		try {
			_requireHeadphoneDeviceApi();
			assertHeadphoneOutputSelection(deviceId, mixerState.headphones.outputs);
			const context = _ensureGraph();
			if (_masterGain === null) throw new Error('headphone monitor master gain is missing');
			nodes = _ensureHeadphoneGraph(context, _masterGain);
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
			mixerState.headphones.error = null;
			_detachHeadphoneElement(previous);
			candidate = null;
		} catch (error) {
			if (candidate !== null) _detachHeadphoneElement(candidate);
			_assertCurrentHeadphoneOperation(generation, nodes);
			throw _headphoneError('headphone output selection failed', error);
		}
	}

	/** Topbar master-volume slider -> master GainNode (COMPONENT-MAP 1.1). */
	setMaster(value: number): void {
		_assertUnit('setMaster value', value);
		mixerState.master = value;
		if (_masterGain !== null) _setParam(_masterGain.gain, value);
	}
}

/** The singleton engine every /performance unit imports. */
export const engine: RbAudioEngine = new RbAudioEngine();
