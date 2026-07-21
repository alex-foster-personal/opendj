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
 *   ✔︎ load(): fetch audio via api-rb -> decodeAudioData -> per-deck chain;
 *     backend 404 (AUDIO_FILE_MISSING etc) -> pushToast + deckLoadErrors set
 *     + reject. Never a silent fallback.
 *     [if] load() of a stable_id whose file is missing [then] toast shown,
 *       deckLoadErrors[deck] holds the code, promise rejects ⛔️
 *     [if] load() succeeds [then] DeckState title/bpm/key/duration populated
 *     [if] a second load() starts before the first resolves [then] the stale
 *       result never clobbers the newer one ⛔️
 *   ✔︎ Reactive transport: position_ms advances via rAF clock math while
 *     playing; remaining time derivable from duration_ms - position_ms;
 *     deckEffectiveBpm() = bpm * pitch.
 *     [if] play() then 1s elapses [then] position_ms ~= 1000 * pitch
 *   ✔︎ CUE semantics: pause() stores the cue at the pause position;
 *     pressCue() while playing returns-to-cue and pauses; while paused it
 *     jumps the playhead to the cue; play() resumes from there.
 *   ✔︎ Loop: engageBeatLoop(beats) converts beats -> seconds from track bpm;
 *     seamless audio via buffer-source loopStart/loopEnd; UI clock wraps the
 *     position manually with the same bounds.
 *     [if] loop engaged and linear clock passes out point [then] position_ms
 *       wraps to in point, audio does not glitch
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
 *
 * Rune module: this file MUST stay .svelte.ts (rune_outside_svelte
 * otherwise - RECON-FRONTEND 10.1, same bug class as stores.svelte.ts fix).
 */

import { pushToast } from '$lib/stores.svelte';
import { fetchAnlz, fetchAudioArrayBuffer, getTrack, RbApiError } from '$lib/rb/api-rb';
import type { Track } from '$lib/rb/api-rb';
import {
	computeFollowerSyncPlan,
	quantizeToNearestBeat,
	validateBeatGrid
} from '$lib/rb/beat-sync-math';
import {
	StretchDeckProcessor,
	type StretchScheduleChange
} from '$lib/rb/stretch-adapter';
import type {
	AnlzBeat,
	AnlzCue,
	AudioEngine,
	CrossfaderAssign,
	DeckAudioSnapshot,
	DeckId,
	DeckState,
	EqBand,
	HotCue,
	LoopState,
	MixerChannelState,
	MixerState,
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

// ------------------------------------------------------------ rune stores

function _emptyDeckState(deck_id: DeckId): DeckState {
	return {
		deck_id,
		stable_id: null,
		title: null,
		artist: null,
		bpm: null,
		key: null,
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
		sync_mode: 'bar',
		sync_error: null,
		processor_error: null,
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
		assign: deck_id % 2 === 1 ? 'A' : 'B'
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
	master: 1
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
	fader: GainNode;
	xf: GainNode;
}

interface _ClockSegment {
	active: boolean;
	loop: LoopState | null;
	startContextTime: number;
	startPositionSec: number;
	tempoRatio: number;
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
}

interface _DeckRuntime {
	processor: StretchDeckProcessor | null;
	durationSec: number;
	latencySec: number;
	controlActive: boolean;
	controlLoop: LoopState | null;
	controlTempoRatio: number;
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
	scheduleIntentCount: number;
	scheduleTail: Promise<void>;
	swapTail: Promise<void>;
}

function _emptyRuntime(): _DeckRuntime {
	return {
		processor: null,
		durationSec: 0,
		latencySec: 0,
		controlActive: false,
		controlLoop: null,
		controlTempoRatio: 1,
		startCtxTime: 0,
		startOffsetSec: 0,
		loadToken: 0,
		nodes: null,
		pending: [],
		presentation: createPresentedTransportTimeline(0),
		nextScheduleRevision: 0,
		scheduleIntentCount: 0,
		scheduleTail: Promise.resolve(),
		swapTail: Promise.resolve()
	};
}

let _ctx: AudioContext | null = null;
let _masterGain: GainNode | null = null;
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

function _ensureGraph(): AudioContext {
	if (typeof window === 'undefined') {
		throw new Error('AudioEngine requires a browser AudioContext (no SSR usage)');
	}
	if (_ctx !== null) return _ctx;
	_ctx = new AudioContext();
	_masterGain = _ctx.createGain();
	_masterGain.gain.value = mixerState.master;
	_masterGain.connect(_ctx.destination);
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
		const fader = _ctx.createGain();
		fader.gain.value = ch.fader;
		const xf = _ctx.createGain();
		xf.gain.value = _xfGainFor(ch.assign, mixerState.crossfader);
		analyser.connect(trim);
		trim.connect(low);
		low.connect(mid);
		mid.connect(high);
		high.connect(fader);
		fader.connect(xf);
		xf.connect(_masterGain);
		_rt[deck].nodes = { analyser, trim, low, mid, high, fader, xf };
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
	timeline.schedules.push({
		...schedule,
		loop: schedule.loop === null ? null : { ...schedule.loop },
		supersededByRevision
	});
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
	if (contextTime === 0 && performanceTime === 0) {
		return _presentedObservation(
			timeline,
			false,
			timeline.last_presentation_context_time_s !== null
		);
	}
	if (contextTime === 0 || performanceTime === 0) {
		throw new RangeError(
			`zero timestamp must be exactly {contextTime: 0, performanceTime: 0}, got ` +
				`contextTime=${contextTime}, performanceTime=${performanceTime}`
		);
	}
	const previousContextTime = timeline.last_presentation_context_time_s;
	const previousPerformanceTime = timeline.last_presentation_performance_time_ms;
	if (
		(previousContextTime !== null && contextTime < previousContextTime) ||
		(previousPerformanceTime !== null && performanceTime < previousPerformanceTime)
	) {
		return _presentedObservation(timeline, false, true);
	}

	let selected: PresentedTransportSchedule | null = null;
	for (const candidate of timeline.schedules) {
		if (candidate.supersededByRevision !== null || candidate.startContextTime > contextTime) {
			continue;
		}
		if (
			selected === null ||
			candidate.startContextTime > selected.startContextTime ||
			(candidate.startContextTime === selected.startContextTime &&
				candidate.revision > selected.revision)
		) {
			selected = candidate;
		}
	}
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
	timeline.last_presentation_performance_time_ms = performanceTime;
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
	rt.presentation = createPresentedTransportTimeline(0);
	rt.nextScheduleRevision = 0;
	_clearLoadedTrackState(st);
	st.processor_error = message;
	st.sync_error = message;
	pushToast(`Deck ${deck} processor failed - ${message}`, 'error');
	if (_masterDeck === deck) _electPlayingMaster();
}

function _stretchChange(
	inputSec: number,
	active: boolean,
	tempoRatio: number,
	masterTempoEnabled: boolean,
	loop: LoopState | null
): StretchScheduleChange {
	return {
		active,
		input: inputSec,
		rate: tempoRatio,
		semitones: masterTempoSemitones(tempoRatio, masterTempoEnabled),
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
	loop?: LoopState | null
): Promise<number> {
	const rt = _rt[deck];
	const expectedLoadToken = rt.loadToken;
	const expectedProcessor = rt.processor;
	const predecessor = rt.scheduleTail;
	let release!: () => void;
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
			loop
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
	loop: LoopState | null | undefined
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
	const scheduledTempoRatio = tempoRatio ?? st.pitch;
	const scheduledMasterTempoEnabled = masterTempoEnabled ?? st.master_tempo_enabled;
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
			_stretchChange(
				scheduledInputSec,
				active,
				scheduledTempoRatio,
				scheduledMasterTempoEnabled,
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
		tempoRatio: scheduledTempoRatio
	});
	st.pitch = scheduledTempoRatio;
	st.master_tempo_enabled = scheduledMasterTempoEnabled;
	st.loop = scheduledLoop === null ? null : { ...scheduledLoop };
	st.playing = active;
	rt.pending = rt.pending.filter((pending) => pending.startContextTime < effectiveWhen);
	rt.pending.push({
		active,
		loop: scheduledLoop === null ? null : { ...scheduledLoop },
		startContextTime: effectiveWhen,
		startPositionSec: scheduledInputSec,
		tempoRatio: scheduledTempoRatio
	});
	st.transport_pending =
		rt.presentation.presented_revision !== rt.presentation.desired_revision;
	_commitPendingIfDue(deck);
	_ensureRaf();
	return scheduledInputSec;
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
		tempoRatio: pending.tempoRatio
	};
}

function _controlSegmentAt(rt: _DeckRuntime, contextTime: number): _ClockSegment {
	let selected: _ClockSegment = {
		active: rt.controlActive,
		loop: rt.controlLoop,
		startContextTime: rt.startCtxTime,
		startPositionSec: rt.startOffsetSec,
		tempoRatio: rt.controlTempoRatio
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
	st.duration_ms = null;
	st.position_ms = 0;
	st.playing = false;
	st.audible = false;
	st.transport_pending = false;
	st.cue_ms = null;
	st.pitch = 1;
	st.loop = null;
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

async function _waitForContextTime(ctx: AudioContext, targetContextTime: number): Promise<void> {
	while (ctx.currentTime < targetContextTime) {
		const remainingMs = (targetContextTime - ctx.currentTime) * 1000;
		await new Promise<void>((resolve) => setTimeout(resolve, Math.max(1, Math.ceil(remainingMs))));
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
		if (!masterState.playing) {
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
			await _waitForContextTime(ctx, pendingWaitTarget);
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
	async load(deck: DeckId, stable_id: string): Promise<void> {
		if (stable_id.length === 0) throw new Error('load: stable_id must be non-empty');
		const st = deckStates[deck];
		const rt = _rt[deck];
		const token = ++rt.loadToken;
		deckLoadErrors[deck] = null;
		let track: Track | null = null;
		let buffer: AudioBuffer | null = null;
		let anlz: DeckState['anlz'] = null;
		let anlzError: string | null = null;
		let latencySec = 0;
		let processor: StretchDeckProcessor | null = null;
		try {
			const ctx = _ensureGraph();
			const [trackRes, audioBytes] = await Promise.all([
				getTrack(stable_id),
				fetchAudioArrayBuffer(stable_id)
			]);
			track = trackRes.track;
			buffer = await ctx.decodeAudioData(audioBytes);
			processor = await StretchDeckProcessor.create(ctx, {
				onProcessorError: (error) => {
					if (_rt[deck].processor === processor) _recordProcessorFailure(deck, error);
				}
			});
			await processor.load(buffer);
			latencySec = await processor.latencySec();
			try {
				anlz = await fetchAnlz(stable_id);
			} catch (error) {
				if (error instanceof RbApiError) {
					anlzError = error.code;
				} else {
					throw error;
				}
			}
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
		if (processor === null || track === null || buffer === null) {
			throw new Error('load: candidate deck transaction is incomplete');
		}
		const candidateProcessor = processor;
		const candidateTrack = track;
		const candidateBuffer = buffer;
		await _withDeckSwap(rt, async () => {
			// Winner check through state publication is one synchronous JS turn.
			// Do not insert an await before rt.processor receives the candidate.
			if (!loadCandidateCanPublish(token, rt.loadToken)) {
				candidateProcessor.disconnect();
				return;
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
			rt.startCtxTime = 0;
			rt.startOffsetSec = 0;
			rt.pending = [];
			rt.presentation = createPresentedTransportTimeline(0);
			rt.nextScheduleRevision = 0;
			st.stable_id = stable_id;
			st.title = candidateTrack.title;
			st.artist = candidateTrack.artist;
			st.bpm = candidateTrack.bpm;
			st.key = candidateTrack.key;
			// TrackOut carries duration_ms (server models.py); the hand-written
			// Track interface omits it, so read it via a typed extension. When
			// absent, the decoded buffer length is the ground truth.
			const trackDurationMs = (
				candidateTrack as Track & { duration_ms?: number | null }
			).duration_ms;
			st.duration_ms =
				typeof trackDurationMs === 'number'
					? trackDurationMs
					: Math.round(candidateBuffer.duration * 1000);
			st.anlz = anlz;
			st.anlz_error = anlzError;
			st.processor_error = null;
			st.sync_error = null;
			if (anlz !== null) {
				st.hot_cues = _hotCuesFrom(anlz.cues);
				st.loop = _displayLoopFrom(anlz.cues);
			}
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
		const { st } = _requireLoaded(deck, 'play');
		if (st.playing) return; // transport already running is a valid state
		if (st.beat_sync_enabled) _requireBeatGrid(st, 'play Beat Sync');
		_setPausedPosition(deck, st.position_ms);
		const ctx = await _resumeContext();
		const startSec = st.position_ms / 1000;
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
		const { st } = _requireLoaded(deck, 'pause');
		if (!st.playing) return; // already paused is a valid state
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
	}

	async cueJump(deck: DeckId, ms: number): Promise<void> {
		await this.quantizedSeek(deck, ms);
	}

	async quantizedSeek(deck: DeckId, ms: number): Promise<void> {
		const { st } = _requireLoaded(deck, 'cueJump');
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
		if (st.playing) {
			const master = seekSyncMaster(deck, st.playing, st.beat_sync_enabled, _syncMaster());
			if (master !== null) {
				await _synchronizeFollowers(master, [deck], {
					followerAnchorSec: { [deck]: targetMs / 1000 }
				});
			} else {
				if (_ctx === null) throw new Error('cueJump: audio graph not initialised');
				await _scheduleDeck(deck, _futureScheduleTime(deck), targetMs / 1000, true);
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
		const { st } = _requireLoaded(deck, 'setTempoRatio');
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
			st.pitch = ratio;
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
		if (snapped.out_ms > durMs) {
			throw new RangeError(
				`setLoop: out_ms must be <= ${Math.round(durMs)}, got ${snapped.out_ms}`
			);
		}
		const nextLoop: LoopState = { ...snapped, engaged: true, beat_length: null };
		if (wasPlaying) {
			if (_ctx === null) throw new Error('setLoop: audio graph not initialised');
			await _scheduleDeck(
				deck,
				scheduleAt,
				(effectiveWhen) => _projectPositionAt(deck, effectiveWhen),
				true,
				undefined,
				undefined,
				nextLoop
			);
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
		if (!st.audible) return Promise.resolve();
		const master = _syncMaster();
		if (master === null) {
			_assignMaster(deck);
			return Promise.resolve();
		}
		return master === deck ? Promise.resolve() : _synchronizeFollowers(master, [deck]);
	}

	async setMasterTempo(deck: DeckId, enabled: boolean): Promise<void> {
		if (typeof enabled !== 'boolean') throw new TypeError('setMasterTempo: enabled must be boolean');
		const st = deckStates[deck];
		if (!st.playing) {
			st.master_tempo_enabled = enabled;
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

	setSyncMode(deck: DeckId, mode: SyncMode): Promise<void> {
		if (mode !== 'beat' && mode !== 'bar') {
			const _exhaustive: never = mode;
			throw new TypeError(`setSyncMode: invalid sync mode ${String(_exhaustive)}`);
		}
		const st = deckStates[deck];
		st.sync_mode = mode;
		const master = _syncMaster();
		if (!st.audible || !st.beat_sync_enabled || master === null || master === deck) {
			return Promise.resolve();
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

	/** Topbar master-volume slider -> master GainNode (COMPONENT-MAP 1.1). */
	setMaster(value: number): void {
		_assertUnit('setMaster value', value);
		mixerState.master = value;
		if (_masterGain !== null) _setParam(_masterGain.gain, value);
	}
}

/** The singleton engine every /performance unit imports. */
export const engine: RbAudioEngine = new RbAudioEngine();
