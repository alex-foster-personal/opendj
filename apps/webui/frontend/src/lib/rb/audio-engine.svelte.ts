/**
 * Client-side Web Audio engine for the /performance rekordbox-parity build
 * (build unit: audio-engine). Implements the AudioEngine contract from
 * $lib/rb/audio-engine-types and owns the per-deck DeckState rune stores.
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
 *     deckEffectiveBpm() = PQTZ grid BPM * pitch (tag BPM only if no grid).
 *     [if] play() then 1s elapses [then] position_ms ~= 1000 * pitch
 *     [if] play() while paused at end-of-track [then] transport restarts at 0
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
import { noteAudioPresentationTick } from '$lib/rb/audio-health.svelte';
import { copyPrefetchedAudio } from '$lib/rb/audio-prefetch-cache.svelte';
import { disposeAudioResources } from '$lib/rb/audio-resource-disposal';
import { reportDeckLoadFailure } from '$lib/rb/deck-load-failure-context';
import { recordDeckLoadTiming, recordPerfEvent, recordPerfTiming } from '$lib/rb/perf-event-log';
import { noteMasterSilence, resetMasterSilenceWatch } from '$lib/rb/master-silence-report';
import {
	notePresentationClock,
	notePresentationTickFailure,
	readOutputTimestamp as _readOutputTimestamp
} from '$lib/rb/presentation-clock-report';
import {
	armAudioContextWatchdog,
	armXrunSentinel,
	disarmContextInstrumentation,
	stampContextDeviceFloors
} from '$lib/rb/audio-context-instrumentation';
import { measurePressToScheduleMs } from '$lib/rb/press-stamp';
import {
	fetchAnlz,
	fetchAnlzBypassingHttpCache,
	fetchAudioArrayBuffer,
	fetchHotCueSlots,
	fetchStemAudioArrayBuffers,
	STEM_LAYOUT_PART_NAMES,
	getTrack,
	probeStemArtifact,
	RbApiError
} from '$lib/rb/api-rb';
import type { AnlzWithVocals, DemucsStemPart, HotCueSlotState, Track } from '$lib/rb/api-rb';
import {
	fetchAnlzForDeckLoad,
	getAnlzEntry,
	invalidateAnlzCacheEntry,
	isAnlzEntryUsable,
	refreshAnlzCacheEntry
} from '$lib/components/rb/wave/anlz-cache.svelte';
import {
	beatJumpTargetMs,
	beatJumpTargetWithinDurationMs,
	computeFollowerSyncPlan,
	planTempoRatioRamp,
	playbackBpm,
	quantizeToNearestBeat,
	validateBeatGrid
} from '$lib/rb/beat-sync-math';
import type { TempoRampStep } from '$lib/rb/beat-sync-math';
import {
	effectiveBeatSync,
	effectiveQuantize,
	GRID_FEATURE_TIP,
	gridFeaturesInert,
	hasRealBeatGrid
} from '$lib/player/grid-features';
import { beatFourLeadInSec, syncSeekBlendDurationSec } from '$lib/rb/sync-seek-blend';
import { uiPrefs } from '$lib/rb/prefs.svelte';
import {
	StretchDeckProcessor,
	type StretchScheduleChange
} from '$lib/rb/stretch-adapter';
import {
	AlignedStemDeckProcessor,
	DEMUCS_PARTS,
	loadingStemDeckState,
	readyStemDeckState,
	STEM_CONTROLS,
	unavailableStemDeckState,
	type StemBuffers
} from '$lib/rb/stem-graph';
import type { AnlzBeat, AnlzCue } from '$lib/rb/anlz-types';
import type { AudioEngine } from '$lib/rb/audio-engine-types';
import { parseExternalRouting, type DeckId } from '$lib/rb/deck-slots';
import type { DeckAudioSnapshot, DeckState, LoopState, SyncMode } from '$lib/rb/deck-state-types';
import type { HotCue, HotCueSlot } from '$lib/rb/hot-cue-types';
import { hotCuesFromAnlz } from '$lib/rb/hot-cue-from-anlz';
import type { CrossfaderAssign, EqBand, MixerChannelState, MixerState } from '$lib/rb/mixer-types';
import type { StemControl, StemDeckState } from '$lib/rb/stem-types';
import {
	ANALYSER_FFT_SIZE,
	AUDIO_CONTEXT_OPTIONS,
	CONTEXT_WAIT_POLL_MS,
	CONTEXT_WAIT_STALL_TIMEOUT_MS,
	DECK_IDS,
	EQ_FREQ_HIGH_HZ,
	EQ_FREQ_LOW_HZ,
	EQ_FREQ_MID_HZ,
	EQ_MAX_DB,
	EQ_MID_Q,
	EQ_MIN_DB,
	FILTER_DEADZONE_FRAC,
	FILTER_HP_CEILING_HZ,
	FILTER_HP_FLOOR_HZ,
	FILTER_LP_CEILING_HZ,
	FILTER_LP_FLOOR_HZ,
	FILTER_Q,
	PARAM_SMOOTH_S,
	PITCH_RANGES,
	TRIM_MAX_GAIN
} from '$lib/player/constants';
import type { PitchRange } from '$lib/player/constants';
import {
	_defaultChannel,
	_defaultHeadphones,
	_emptyDeckState,
	_hotCueRevisionsFrom,
	deckEffectiveBpm,
	deckLoadErrors,
	deckRemainingMs,
	deckStates,
	getDeckState,
	mixerState,
	pitchRanges
} from '$lib/player/state.svelte';
import {
	attachMasterMuteNode,
	isMasterMuted,
	setMasterMuted
} from '$lib/player/master-mute.svelte';
import {
	acquireHeadphoneOutput as acquireMonitorOutput,
	applyHeadphoneMix,
	disposeHeadphoneMonitor,
	ensureHeadphoneGraph,
	refreshHeadphoneOutputs as refreshMonitorOutputs,
	selectHeadphoneOutput as selectMonitorOutput
} from '$lib/player/headphones';
import {
	_assertKeyShift,
	camelotKeysAreCompatible,
	composeStretchSemitones,
	deriveKeySyncNudge,
	deriveKeySyncSemitones,
	deriveKeySyncTargetManualShift,
	effectiveCamelotKey,
	masterTempoSemitones,
	parseCamelotKey
} from '$lib/player/key/camelot';
import type { CamelotKey } from '$lib/player/key/camelot';
import {
	exactBeatLoopRangeMs,
	loopEndpointsWithinDurationMs,
	quantizedLoopEndpointsMs
} from '$lib/player/transport/loops';
import {
	_positionForSegment,
	pausedSeekClock,
	commonSyncScheduleTimes,
	deckReachedEnd,
	normalizeEngagedLoopPositionSec,
	normalizeScheduledTransportEntrySec,
	pendingSyncWaitTarget,
	playResumePositionSec,
	processorOnsetLeadSec,
	projectedLoopAwareTransportPosition,
	projectedTransportPosition,
	safeSyncScheduleTime,
	safeTransportScheduleTime,
	scheduleOffsetStages,
	supersedingScheduleTime
} from '$lib/player/transport/schedule-math';
import type { _ClockSegment } from '$lib/player/transport/schedule-math';
import {
	_effectivePresentedScheduleAt,
	acknowledgePresentedTransportSchedule,
	createPresentedTransportTimeline,
	observePresentedTransportTimeline,
	setPausedTransportTimelineCursor
} from '$lib/player/transport/presentation';
import type {
	PresentedTransportObservation,
	PresentedTransportSchedule,
	PresentedTransportTimeline
} from '$lib/player/transport/presentation';

// ---------------------------------------------------- extracted re-exports
//
// T4 S1. The DSP constants moved to player/constants.ts and the Camelot
// algebra to player/key/camelot.ts. Everything they used to export from here
// is re-exported below, so every existing importer of
// $lib/rb/audio-engine.svelte keeps working unchanged.

export { DECK_IDS, PITCH_RANGES };
export type { PitchRange };
export {
	camelotKeysAreCompatible,
	composeStretchSemitones,
	deriveKeySyncNudge,
	deriveKeySyncSemitones,
	deriveKeySyncTargetManualShift,
	effectiveCamelotKey,
	masterTempoSemitones,
	parseCamelotKey
};
export type { CamelotKey };
export { exactBeatLoopRangeMs, loopEndpointsWithinDurationMs, quantizedLoopEndpointsMs };
export {
	commonSyncScheduleTimes,
	deckReachedEnd,
	normalizeEngagedLoopPositionSec,
	normalizeScheduledTransportEntrySec,
	pendingSyncWaitTarget,
	playResumePositionSec,
	processorOnsetLeadSec,
	projectedLoopAwareTransportPosition,
	projectedTransportPosition,
	safeSyncScheduleTime,
	safeTransportScheduleTime,
	scheduleOffsetStages,
	supersedingScheduleTime
};
export { pausedSeekClock };
// The headphone / cue monitor moved WHOLE to player/headphones.ts -- its state,
// its device boundary and its algebra. Deliberately NOT re-exported here: no
// module in src ever reached its pure surface through this barrel, and a
// pass-through export would leave the coupling the extraction just removed.
export {
	acknowledgePresentedTransportSchedule,
	createPresentedTransportTimeline,
	observePresentedTransportTimeline,
	setPausedTransportTimelineCursor
};
export type {
	PresentedTransportObservation,
	PresentedTransportSchedule,
	PresentedTransportTimeline
};

// ------------------------------------------------------------ rune stores
//
// T4 S4: PerformanceState now lives in player/state.svelte.ts. Re-exported
// below at the same names so every UI consumer keeps importing it from here.
// _hotCuesFromSlots stays until S11: it maps through hotCuesFromAnlz, which is
// deck-load's ANLZ cue mapper, and state must not import deck/load.

export {
	deckEffectiveBpm,
	deckLoadErrors,
	deckRemainingMs,
	deckStates,
	getDeckState,
	mixerState,
	pitchRanges
};

// Opt-in startup master mute (`?muted=1`), re-exported at the engine barrel so
// the topbar toggle and any test agent reach it on the same import as the rest
// of the player surface. See player/master-mute.svelte.ts.
export { isMasterMuted, setMasterMuted };

function _hotCuesFromSlots(slots: HotCueSlotState[]): HotCue[] {
	return hotCuesFromAnlz(slots.flatMap((slot) => slot.cue === null ? [] : [slot.cue]));
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
	filterLp: BiquadFilterNode;
	filterHp: BiquadFilterNode;
	cue: GainNode;
	fader: GainNode;
	xf: GainNode;
	extsplit: ChannelSplitterNode | null;
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

/** Acknowledged future rate change for SLIP's hidden, non-looping timeline. */
export interface SlipTempoBoundary {
	startContextTime: number;
	tempoRatio: number;
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
	slipTempoBoundaries: SlipTempoBoundary[];
	/** Manual key-shift baseline captured when KEY SYNC latches on; restored
	 * on disable so the Camelot offset cannot drift away from the latch. */
	keySyncBaselineSemitones: number | null;
	/** Decoded mix buffer retained for short sync-seek crossfades. */
	audioBuffer: AudioBuffer | null;
	/**
	 * True from the moment a re-anchor tempo ramp begins until its last step
	 * is registered. Each ramp step's own revision briefly becomes "presented"
	 * as soon as real playback reaches it, well before later steps finish
	 * their own worklet round-trip - without this override, transport_pending
	 * would flicker false mid-ramp (revision temporarily settled) and let a
	 * caller read/measure position while the rate is still transitioning. See
	 * `_scheduleReanchoredFollower` / `_continueTempoRamp`.
	 */
	reanchorRampActive: boolean;
	/**
	 * LAZY-STEMS. A fully built AlignedStemDeckProcessor waiting for the deck to
	 * be replaceable, held here because the engine forbids swapping a deck's
	 * processor while it is playing or audible (assertDeckReplacementAllowed).
	 * Set by _upgradeDeckStems when the stems finish decoding mid-playback;
	 * drained by _drainPendingStemUpgrade on the next stop. Null the rest of the
	 * time. `token` pins it to the load that produced it so a track swap during
	 * the fetch cannot graft one track's stems onto another's mix.
	 */
	pendingStemUpgrade: {
		token: number;
		processor: AlignedStemDeckProcessor;
		state: StemDeckState;
	} | null;
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
		slipAnchor: null,
		slipTempoBoundaries: [],
		keySyncBaselineSemitones: null,
		audioBuffer: null,
		reanchorRampActive: false,
		pendingStemUpgrade: null
	};
}

let _ctx: AudioContext | null = null;
let _masterGain: GainNode | null = null;
let _masterAnalyser: AnalyserNode | null = null;
/** Opt-in startup mute, last node before the destination. Never bypassed. */
let _masterMuteGain: GainNode | null = null;
let _externalMerger: ChannelMergerNode | null = null;
let _rafId: number | null = null;
let _masterDeck: DeckId | null = null;
const _rt: Record<DeckId, _DeckRuntime> = {
	1: _emptyRuntime(),
	2: _emptyRuntime(),
	3: _emptyRuntime(),
	4: _emptyRuntime()
};

/** Instantaneous post-DSP RMS meter 0..1 for a channel strip VU pulse.
 * Returns 0 when the deck graph is missing or silent - real silence, not a mock. */
const _meterScratch: Record<DeckId, Float32Array | null> = { 1: null, 2: null, 3: null, 4: null };

export function peekDeckMeter(deck: DeckId): number {
	const nodes = _rt[deck].nodes;
	if (nodes === null) return 0;
	let buf = _meterScratch[deck];
	if (buf === null || buf.length !== nodes.analyser.fftSize) {
		buf = new Float32Array(new ArrayBuffer(nodes.analyser.fftSize * 4));
		_meterScratch[deck] = buf;
	}
	nodes.analyser.getFloatTimeDomainData(buf as Float32Array<ArrayBuffer>);
	let sum = 0;
	for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
	const rms = Math.sqrt(sum / buf.length);
	if (!Number.isFinite(rms)) return 0;
	// Typical music RMS sits well below 1.0; scale for a readable thin pulse.
	return Math.min(1, rms * 5.5);
}

export function deckTransportClock(deck: DeckId): DeckTransportClock {
	const presentation = _rt[deck].presentation;
	return {
		source: presentation.presented_active ? 'audio_output' : 'paused_cursor',
		presentation_context_time_s: presentation.last_presentation_context_time_s,
		desired_revision: presentation.desired_revision,
		presented_revision: presentation.presented_revision
	};
}

/** Estimated decoded PCM retained for memory tracking.
 * Mix buffer always; when stems are ready, add 4 aligned part buffers
 * (AlignedStemDeckProcessor keeps vocals/drums/bass/other at the same geometry). */
export function deckPcmEstimatedBytes(): number {
	let total = 0;
	for (const deck of DECK_IDS) {
		const buffer = _rt[deck].audioBuffer;
		if (buffer === null) continue;
		const mixBytes = buffer.length * buffer.numberOfChannels * 4;
		total += mixBytes;
		if (deckStates[deck].stems.status === 'ready') total += mixBytes * 4;
	}
	return total;
}

interface AudioDisconnectable {
	disconnect(): void;
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

/**
 * FILTER knob -> {lpHz, hpHz} corner frequencies for the two always-in-chain
 * biquads (see FILTER_* constants). `colour` is bipolar travel away from the
 * 0.5 detent; below FILTER_DEADZONE_FRAC of it is a true dry bypass (both
 * filters left fully open). Sweeps are exponential in Hz, i.e. linear in
 * octaves (L2 in docs/research/filter-taper-laws.md), so equal knob motion
 * covers equal perceived distance anywhere in the travel.
 */
function _filterFreqsFromKnob(value: number): { lpHz: number; hpHz: number } {
	const colour = (value - 0.5) * 2; // -1 (full CCW) .. 1 (full CW)
	if (Math.abs(colour) < FILTER_DEADZONE_FRAC) {
		return { lpHz: FILTER_LP_CEILING_HZ, hpHz: FILTER_HP_FLOOR_HZ };
	}
	const u = (Math.abs(colour) - FILTER_DEADZONE_FRAC) / (1 - FILTER_DEADZONE_FRAC);
	if (colour < 0) {
		const lpHz = FILTER_LP_CEILING_HZ * (FILTER_LP_FLOOR_HZ / FILTER_LP_CEILING_HZ) ** u;
		return { lpHz, hpHz: FILTER_HP_FLOOR_HZ };
	}
	const hpHz = FILTER_HP_FLOOR_HZ * (FILTER_HP_CEILING_HZ / FILTER_HP_FLOOR_HZ) ** u;
	return { lpHz: FILTER_LP_CEILING_HZ, hpHz };
}

function _setParam(param: AudioParam, value: number): void {
	if (_ctx === null) throw new Error('audio graph not initialised');
	param.setTargetAtTime(value, _ctx.currentTime, PARAM_SMOOTH_S);
}

// -------------------------------------------- external mixer routing
// Opt-in via URL query `?extroute=1:1,2:7` -- comma-separated `deck:usbLeft`
// pairs, where usbLeft is the 1-based LEFT channel of a stereo pair on the
// selected multichannel output device (e.g. DJM mixer over USB: 1 -> USB 1/2
// -> mixer CH1, 7 -> USB 7/8 -> mixer CH4). Routed decks keep trim/EQ/fader
// but bypass the in-app crossfader and master bus: summing and crossfading
// belong to the hardware mixer. Unrouted decks stay on the internal master,
// which in this mode feeds ONLY the headphone monitor.

function _ensureGraph(): AudioContext {
	if (typeof window === 'undefined') {
		throw new Error('AudioEngine requires a browser AudioContext (no SSR usage)');
	}
	if (_ctx !== null) return _ctx;
	// Construction options travel through ONE named constant so a future
	// user-facing buffer/latency setting has a single place to write to.
	_ctx = new AudioContext(AUDIO_CONTEXT_OPTIONS);
	stampContextDeviceFloors(_ctx);
	// A context that is allowed to start running immediately never fires
	// statechange, so the build stamp above already caught it; one that starts
	// suspended is re-stamped the moment it runs, whichever path resumed it.
	// The watchdog owns that re-stamp AND every non-running state: suspended,
	// interrupted and closed used to fall through in silence, which is how
	// Wed 2 Sep 2026 cost ~24 minutes of audio with nothing on screen.
	armAudioContextWatchdog(_ctx, () => DECK_IDS.some((deck) => deckStates[deck].playing));
	_masterGain = _ctx.createGain();
	_masterGain.gain.value = mixerState.master;
	// Silence watchdog tap: an AnalyserNode with nothing downstream is a pure
	// observer, and it sits BEFORE _masterMuteGain so `?muted=1` is not a dropout.
	_masterAnalyser = _ctx.createAnalyser();
	_masterGain.connect(_masterAnalyser);
	resetMasterSilenceWatch();
	// Silence belt for headless test agents (`?muted=1`): the LAST node before
	// the destination, so a mute is one gain value and every node upstream --
	// decks, EQ, crossfader, analysers, headphone monitor -- keeps running
	// identically. See player/master-mute.svelte.ts.
	_masterMuteGain = _ctx.createGain();
	attachMasterMuteNode(_masterMuteGain);
	const routing = parseExternalRouting();
	if (routing === null) {
		_masterMuteGain.connect(_ctx.destination);
		_masterGain.connect(_masterMuteGain);
	} else {
		const highestUsbChannel = Math.max(...[...routing.values()].map((left) => left + 1));
		const dest = _ctx.destination;
		if (dest.maxChannelCount < highestUsbChannel) {
			throw new Error(
				`extroute needs ${highestUsbChannel} output channels but the current output device exposes ` +
					`${dest.maxChannelCount} - select the multichannel interface as the system output device and reload`
			);
		}
		dest.channelCount = dest.maxChannelCount;
		dest.channelInterpretation = 'discrete';
		// The mute node inherits the discrete multichannel contract, otherwise
		// the default speakers interpretation would downmix the per-deck USB
		// pairs on their way through it.
		_masterMuteGain.channelCount = dest.channelCount;
		_masterMuteGain.channelCountMode = 'explicit';
		_masterMuteGain.channelInterpretation = 'discrete';
		_masterMuteGain.connect(dest);
		_externalMerger = _ctx.createChannelMerger(dest.channelCount);
		_externalMerger.channelInterpretation = 'discrete';
		_externalMerger.connect(_masterMuteGain);
	}
	const headphones = ensureHeadphoneGraph(_ctx, _masterGain);
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
		const { lpHz, hpHz } = _filterFreqsFromKnob(ch.filter);
		const filterLp = _ctx.createBiquadFilter();
		filterLp.type = 'lowpass';
		filterLp.Q.value = FILTER_Q;
		filterLp.frequency.value = lpHz;
		const filterHp = _ctx.createBiquadFilter();
		filterHp.type = 'highpass';
		filterHp.Q.value = FILTER_Q;
		filterHp.frequency.value = hpHz;
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
		high.connect(filterLp);
		filterLp.connect(filterHp);
		filterHp.connect(cue);
		cue.connect(headphones.cueSum);
		filterHp.connect(fader);
		const usbLeft = routing?.get(deck) ?? null;
		let extsplit: ChannelSplitterNode | null = null;
		if (usbLeft !== null && _externalMerger !== null) {
			extsplit = _ctx.createChannelSplitter(2);
			fader.connect(extsplit);
			extsplit.connect(_externalMerger, 0, usbLeft - 1);
			extsplit.connect(_externalMerger, 1, usbLeft);
		} else {
			fader.connect(xf);
			xf.connect(_masterGain);
		}
		_rt[deck].nodes = { analyser, trim, low, mid, high, filterLp, filterHp, cue, fader, xf, extsplit };
	}
	armXrunSentinel(_ctx);
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

export function decodedTransportDurationMs(decodedDurationSec: number): number {
	if (!Number.isFinite(decodedDurationSec) || decodedDurationSec <= 0) {
		throw new RangeError(
			`decoded audio duration must be finite and positive, got ${decodedDurationSec}`
		);
	}
	return decodedDurationSec * 1000;
}

export function seekSyncMaster(
	deck: DeckId,
	playing: boolean,
	beatSyncEnabled: boolean,
	master: DeckId | null
): DeckId | null {
	return playing && beatSyncEnabled && master !== null && master !== deck ? master : null;
}

/** BeatSyncMax master relocate: return transport-active beat-synced followers
 * that must phase-lock when the master itself seeks. Empty = free seek. */
export function beatSyncMaxFollowers(
	deck: DeckId,
	beatSyncMax: boolean,
	master: DeckId | null,
	candidates: readonly { id: DeckId; playing: boolean; beatSyncEnabled: boolean }[]
): DeckId[] {
	if (!beatSyncMax || master === null || master !== deck) return [];
	return candidates
		.filter((c) => c.id !== deck && c.playing && c.beatSyncEnabled)
		.map((c) => c.id);
}

/** Decide whether a playing relocate keeps BAR/beat phase lock.
 * Loop exit must not force a free seek - clear the loop, then still sync. */
export type SeekSyncPlan =
	| { kind: 'follower'; master: DeckId }
	| { kind: 'master-max'; followers: DeckId[] }
	| { kind: 'free' };

export function planSeekSync(args: {
	deck: DeckId;
	transportActive: boolean;
	beatSyncEnabled: boolean;
	activeMaster: DeckId | null;
	beatSyncMax: boolean;
	candidates: readonly { id: DeckId; playing: boolean; beatSyncEnabled: boolean }[];
}): SeekSyncPlan {
	const master = seekSyncMaster(
		args.deck,
		args.transportActive,
		args.beatSyncEnabled,
		args.activeMaster
	);
	if (master !== null) return { kind: 'follower', master };
	const followers = beatSyncMaxFollowers(
		args.deck,
		args.beatSyncMax,
		args.activeMaster,
		args.candidates
	);
	if (followers.length > 0) return { kind: 'master-max', followers };
	return { kind: 'free' };
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

/** The manual baseline for a live command must match the output-presented
 * effective pitch baseline, never a newer desired control schedule. */
export function keySyncManualShiftBaseline(source: KeySyncEffectiveOffsetSource): number {
	const quiescent =
		!source.audible &&
		!source.transportPending &&
		!source.pendingMutation &&
		!source.presentation.presented_active &&
		source.presentation.desired_revision === source.presentation.presented_revision;
	if (quiescent) {
		_assertKeyShift(source.control.keyShiftSemitones);
		return source.control.keyShiftSemitones;
	}
	const presentedAt = source.presentation.last_presentation_context_time_s;
	if (presentedAt === null) {
		throw new Error('KEY SYNC requires output presentation truth before deriving its manual baseline');
	}
	const baseline = presentedKeyShiftSemitonesAt(source.presentation, presentedAt);
	if (baseline === null) {
		throw new Error('KEY SYNC requires an output-presented schedule before deriving its manual baseline');
	}
	_assertKeyShift(baseline);
	return baseline;
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

/** Re-anchor hidden SLIP transport at an acknowledged rate boundary. */
export function rebaseSlipAnchor(
	anchor: SlipAnchor,
	effectiveWhen: number,
	tempoRatio: number
): SlipAnchor {
	if (!Number.isFinite(effectiveWhen) || effectiveWhen < 0) {
		throw new RangeError(`SLIP rebase time must be finite and non-negative, got ${effectiveWhen}`);
	}
	return createSlipAnchor({
		startContextTime: effectiveWhen,
		startPositionSec: slipHiddenPositionSec(anchor, effectiveWhen),
		tempoRatio,
		durationSec: anchor.durationSec
	});
}

/** Integrate hidden SLIP time through its accepted presentation-rate boundaries. */
export function slipHiddenPositionWithTempoBoundaries(
	anchor: SlipAnchor,
	boundaries: readonly SlipTempoBoundary[],
	contextTime: number
): number {
	if (!Number.isFinite(contextTime) || contextTime < 0) {
		throw new RangeError(`SLIP context time must be finite and non-negative, got ${contextTime}`);
	}
	let segment = createSlipAnchor(anchor);
	for (const boundary of boundaries) {
		if (!Number.isFinite(boundary.startContextTime) || boundary.startContextTime < segment.startContextTime) {
			throw new RangeError('SLIP tempo boundaries must be ordered after the anchor');
		}
		if (!Number.isFinite(boundary.tempoRatio) || boundary.tempoRatio <= 0) {
			throw new RangeError('SLIP tempo boundary ratio must be finite and positive');
		}
		if (boundary.startContextTime > contextTime) break;
		segment = rebaseSlipAnchor(segment, boundary.startContextTime, boundary.tempoRatio);
	}
	return slipHiddenPositionSec(segment, contextTime);
}

/** Retain accepted, effective future presentation schedules after a SLIP anchor. */
export function slipTempoBoundariesAfterAnchor(
	timeline: PresentedTransportTimeline,
	anchor: SlipAnchor
): SlipTempoBoundary[] {
	const validAnchor = createSlipAnchor(anchor);
	const candidates = timeline.schedules
		.filter(
			(schedule) =>
				schedule.supersededByRevision === null &&
				schedule.active &&
				schedule.startContextTime > validAnchor.startContextTime
		)
		.sort(
			(left, right) =>
				left.startContextTime - right.startContextTime || left.revision - right.revision
		);
	const boundaries: SlipTempoBoundary[] = [];
	for (const schedule of candidates) {
		const previous = boundaries[boundaries.length - 1];
		if (previous?.startContextTime === schedule.startContextTime) {
			previous.tempoRatio = schedule.tempoRatio;
		} else {
			boundaries.push({ startContextTime: schedule.startContextTime, tempoRatio: schedule.tempoRatio });
		}
	}
	return boundaries;
}

/** Create a hidden SLIP anchor from the listener-facing engaged loop only. */
export function presentedSlipAnchor(
	timeline: PresentedTransportTimeline,
	durationSec: number
): SlipAnchor {
	if (!Number.isFinite(durationSec) || durationSec <= 0) {
		throw new RangeError(`SLIP duration must be positive and finite, got ${durationSec}`);
	}
	const presentedAt = timeline.last_presentation_context_time_s;
	if (presentedAt === null) throw new Error('SLIP requires output presentation truth before activation');
	const schedule = _effectivePresentedScheduleAt(timeline, presentedAt);
	if (schedule === null || !schedule.active || schedule.loop?.engaged !== true) {
		throw new Error('SLIP requires an output-presented engaged loop before activation');
	}
	return createSlipAnchor({
		startContextTime: presentedAt,
		startPositionSec: _positionForSegment(schedule, presentedAt, durationSec),
		tempoRatio: schedule.tempoRatio,
		durationSec
	});
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
	/** Presentation clock lag: desired_revision !== presented_revision.
	 * The control clock (`pendingScheduleCount`) drains off `ctx.currentTime`,
	 * but the presentation clock only advances inside the rAF tick. When rAF
	 * stops - a hidden tab, or a natural end that left nothing audible to
	 * animate - the control clock reaches idle while presentation still lags.
	 * Without this flag a seek takes the paused-cursor branch, which then
	 * throws in setPausedTransportTimelineCursor and wedges the deck. */
	presentationPending: boolean;
}

/** True while the rAF-driven presentation clock has not caught up to the last
 * acknowledged schedule. Read this, never `st.transport_pending`, when gating a
 * transport mutation: the state flag also folds in reanchor ramps. */
function _presentationPending(rt: _DeckRuntime): boolean {
	return rt.presentation.desired_revision !== rt.presentation.presented_revision;
}

export function transportNeedsScheduledMutation(
	activity: TransportMutationActivity
): boolean {
	for (const [name, value] of Object.entries({
		playing: activity.playing,
		audible: activity.audible,
		controlActive: activity.controlActive,
		presentationPending: activity.presentationPending
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
		activity.presentationPending ||
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

/**
 * The grid a GRID-DEPENDENT operation cannot proceed without.
 *
 * Reserved for operations that are meaningless with no grid: engaging Beat
 * Sync, and beat loops. Transport must never call this - see _quantizeGrid.
 */
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

/**
 * The grid to snap to, or null when quantize is off or this deck has no grid.
 * Never throws.
 *
 * This is the transport path's only route to the beat grid. Transport is never
 * gated by a grid-dependent feature: on a track with no real PQTZ grid,
 * quantize simply has no effect and play, pause and cue use exact playhead
 * times. pause() in particular must be unrefusable - a deck the DJ cannot stop
 * is worse than one that starts unquantized.
 *
 * effectiveQuantize is the authority on WHETHER to snap; hasRealBeatGrid is
 * re-asked only to narrow readonly AnlzBeat[] | undefined for the caller.
 */
function _quantizeGrid(st: DeckState): readonly AnlzBeat[] | null {
	const beats = st.anlz?.beatgrid.beats;
	return effectiveQuantize(st) && hasRealBeatGrid(beats) ? beats : null;
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

/** Disconnect for synchronous silence, then let the worklet transfer its
 * retained PCM back and mark itself finished (see StretchDeckProcessor.dispose's
 * docstring). Every path that retires a processor must call this, not just
 * `.disconnect()`: `.disconnect()` alone leaves the retired worklet and its
 * PCM alive until the whole AudioContext is torn down. */
function _retireProcessor(processor: _DeckProcessor): void {
	processor.disconnect();
	void processor.dispose().catch(() => {
		// Retirement already proceeds regardless of outcome; dispose() still
		// closes the MessagePort in its finally block even on rejection.
	});
}

function _recordProcessorFailure(deck: DeckId, error: unknown): void {
	const st = deckStates[deck];
	const rt = _rt[deck];
	const message = error instanceof Error ? error.message : String(error);
	const failedProcessor = rt.processor;
	rt.processor = null;
	if (failedProcessor !== null) _retireProcessor(failedProcessor);
	rt.audioBuffer = null;
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

/** Q1: `_scheduleDeck` from a press path - the stamp rides, nothing else moves.
 * `keep` = the tempo/loop/key arguments a press never overrides, so each one
 * stays the deck's standing intent. */
function _schedulePress(
	deck: DeckId,
	when: number,
	inputSec: number | ((effectiveWhen: number) => number),
	active: boolean,
	pressT0Ms: number | undefined
): Promise<number> {
	const keep = undefined;
	return _scheduleDeck(deck, when, inputSec, active, keep, keep, keep, keep, pressT0Ms);
}

async function _scheduleDeck(
	deck: DeckId,
	when: number,
	inputSec: number | ((effectiveWhen: number) => number),
	active: boolean,
	tempoRatio?: number,
	masterTempoEnabled?: boolean,
	loop?: LoopState | null,
	keyShiftSemitones?: number,
	pressT0Ms?: number
): Promise<number> {
	const rt = _rt[deck];
	const expectedLoadToken = rt.loadToken;
	const expectedProcessor = rt.processor;
	const predecessor = rt.scheduleTail;
	let release!: () => void;
	rt.desiredActive = active;
	// LATENCY-01 visual feedback (<=16ms, one frame): the play glyph reads
	// st.playing, and st.playing used to be written only AFTER the AudioWorklet
	// MessagePort round trip acknowledged the schedule. That made the button's
	// appearance causally downstream of the audio thread - fast today only
	// because that RPC happens to be ~2ms, and not independently fast at all: a
	// worklet stall, a stem deck's four parallel schedules or a busy sync scope
	// drags the glyph along with it. Write the INTENT here, in the same
	// synchronous turn as the input, before any await. The post-ack write in
	// _scheduleDeckSerial stays as the reconcile-to-truth.
	deckStates[deck].playing = active;
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
			keyShiftSemitones,
			pressT0Ms
		);
	} catch (error) {
		// Reconcile the optimistic write to the STANDING intent, not to the value
		// it had before: a newer command may already have superseded this one, and
		// rolling back to a stale value would clobber it. A processor failure and
		// a mid-queue reload both reset desiredActive to false first, so this
		// lands on "not playing" for a genuinely failed start.
		deckStates[deck].playing = rt.desiredActive;
		throw error;
	} finally {
		rt.scheduleIntentCount -= 1;
		release();
	}
}

/**
 * Re-read the processor's OWN self-reported latency and heal our snapshot.
 *
 * `rt.latencySec` is read once, at load. Signalsmith's `configure()` ends by
 * re-reading `_inputLatency()` / `_outputLatency()` from WASM and `latency()`
 * returns their live sum (vendored 1.3.2, SignalsmithStretch.js:215-216), so
 * the library tracks its own reconfiguration and our copy does not. A stale
 * copy would corrupt two things at once: the schedule floor computed from it,
 * and the `processor_latency_ms` term the logged decomposition is read against.
 *
 * Never awaited by the transport path - the schedule is already posted when
 * this runs, so it adds no latency to the thing it measures. A move is recorded
 * loudly rather than absorbed: schedules taken before it used the old value.
 */
async function _observeLiveProcessorLatency(
	deck: DeckId,
	processor: _DeckProcessor
): Promise<void> {
	let liveLatencySec: number;
	try {
		liveLatencySec = await processor.latencySec();
	} catch (error) {
		// The deck was unloaded or the processor torn down while a diagnostic
		// query was in flight. Recorded, never swallowed - a read that keeps
		// failing means the instrument has gone blind.
		recordPerfEvent('processor-latency-read-failed', String(error), deck);
		return;
	}
	const rt = _rt[deck];
	if (rt.processor !== processor) return; // deck moved on; this reading is not its
	if (Math.abs(liveLatencySec - rt.latencySec) <= 1e-9) return;
	recordPerfEvent(
		'processor-latency-drift',
		`deck ${deck} processor latency moved ${rt.latencySec}s -> ${liveLatencySec}s; ` +
			'schedules and logged offsets before this point used the stale value',
		deck
	);
	rt.latencySec = liveLatencySec;
}

async function _scheduleDeckSerial(
	deck: DeckId,
	when: number,
	inputSec: number | ((effectiveWhen: number) => number),
	active: boolean,
	tempoRatio: number | undefined,
	masterTempoEnabled: boolean | undefined,
	loop: LoopState | null | undefined,
	keyShiftSemitones: number | undefined,
	pressT0Ms: number | undefined
): Promise<number> {
	const { st, rt } = _requireLoaded(deck, '_scheduleDeck');
	_commitPendingIfDue(deck);
	const processor = rt.processor;
	if (processor === null) throw new Error(`_scheduleDeck: deck ${deck} processor is missing`);
	if (_ctx === null) throw new Error('_scheduleDeck: audio graph not initialised');
	const scheduleContextTime = _ctx.currentTime;
	// Q1: the SAME clock read the row reports, so both halves of
	// input_to_audible_ms meet at one instant instead of overlapping.
	const pressToScheduleMs = measurePressToScheduleMs(pressT0Ms, deck);
	const processorLeadSec = _transportLeadSec(deck);
	const minimumSafeWhen = safeTransportScheduleTime(scheduleContextTime, processorLeadSec);
	const safeRequestedWhen = Math.max(when, minimumSafeWhen);
	const latestPending = rt.pending[rt.pending.length - 1] ?? null;
	const effectiveWhen = supersedingScheduleTime(
		safeRequestedWhen,
		latestPending?.startContextTime ?? null,
		minimumSafeWhen
	);
	// LATENCY-03: scheduled_offset_ms is THE number the Class A budget turns on,
	// and it is a value this function never returns to a caller. Captured HERE,
	// downstream of both clamps (the minimumSafeWhen floor and the superseding
	// pending boundary), from the same const handed to processor.schedule below.
	// Emitted after the ack, so the log write never sits on the path it measures.
	const scheduleStages = scheduleOffsetStages({
		contextTimeSec: scheduleContextTime,
		requestedWhenSec: when,
		effectiveWhenSec: effectiveWhen,
		processorLeadSec,
		processorLatencySec: rt.latencySec,
		baseLatencySec: _ctx.baseLatency,
		outputLatencySec: _ctx.outputLatency,
		active,
		...(pressToScheduleMs === undefined ? {} : { pressToScheduleMs })
	});
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
	recordPerfTiming('transport-schedule', scheduleStages, deck);
	// LATENCY-03: rt.latencySec is a snapshot taken once at load, but Signalsmith
	// re-reads both latency terms from WASM at the end of every configure() and
	// latency() returns their live sum - so the library self-tracks and our copy
	// does not. Nothing reconfigures today; the point is that when something
	// does, the instrument must not keep quoting a dead number. Fire-and-forget
	// so it costs the transport path nothing: the row above carries the value
	// this schedule actually used, and the check that follows heals the snapshot
	// for the next one and shouts if it moved.
	void _observeLiveProcessorLatency(deck, processor);
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
	_recordSlipTempoBoundary(rt, effectiveWhen, scheduledTempoRatio);
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
		rt.presentation.presented_revision !== rt.presentation.desired_revision ||
		rt.reanchorRampActive;
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
			presentationPending: _presentationPending(rt),
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
	return keySyncEffectiveAudibleSemitones(_keySyncSource(deck));
}

function _keySyncManualShiftBaseline(deck: DeckId): number {
	return keySyncManualShiftBaseline(_keySyncSource(deck));
}

function _keySyncSource(deck: DeckId): KeySyncEffectiveOffsetSource {
	const st = deckStates[deck];
	const rt = _rt[deck];
	return {
		audible: st.audible,
		transportPending: st.transport_pending,
		pendingMutation: rt.pending.length > 0 || rt.scheduleIntentCount > 0,
		control: {
			tempoRatio: rt.controlTempoRatio,
			masterTempoEnabled: rt.controlMasterTempoEnabled,
			keyShiftSemitones: rt.controlKeyShiftSemitones
		},
		presentation: rt.presentation
	};
}

function _clearSlip(deck: DeckId): void {
	const st = deckStates[deck];
	st.slip_active = false;
	st.slip_position_ms = null;
	_rt[deck].slipAnchor = null;
	_rt[deck].slipTempoBoundaries = [];
}

function _slipHiddenPositionAt(rt: _DeckRuntime, contextTime: number): number {
	if (rt.slipAnchor === null) throw new Error('SLIP hidden position requires an active anchor');
	return slipHiddenPositionWithTempoBoundaries(rt.slipAnchor, rt.slipTempoBoundaries, contextTime);
}

function _recordSlipTempoBoundary(rt: _DeckRuntime, effectiveWhen: number, tempoRatio: number): void {
	if (rt.slipAnchor === null) return;
	rt.slipTempoBoundaries = rt.slipTempoBoundaries.filter(
		(boundary) => boundary.startContextTime < effectiveWhen
	);
	rt.slipTempoBoundaries.push({ startContextTime: effectiveWhen, tempoRatio });
}

function _activateSlip(
	deck: DeckId,
	anchor: SlipAnchor,
	boundaries: readonly SlipTempoBoundary[] = []
): void {
	const { st, rt } = _requireLoaded(deck, 'activate SLIP');
	if (!shouldActivateSlip(st.playing, st.slip_enabled)) {
		throw new Error('SLIP can only activate on a playing deck with SLIP enabled');
	}
	rt.slipAnchor = createSlipAnchor(anchor);
	rt.slipTempoBoundaries = boundaries.map((boundary) => ({ ...boundary }));
	st.slip_active = true;
	st.slip_position_ms = rt.slipAnchor.startPositionSec * 1000;
}

function _updateSlipPosition(deck: DeckId): void {
	const st = deckStates[deck];
	const anchor = _rt[deck].slipAnchor;
	if (!st.slip_active || anchor === null) return;
	if (!st.playing) {
		_clearSlip(deck);
		return;
	}
	const presentedAt = _rt[deck].presentation.last_presentation_context_time_s;
	if (presentedAt === null) return;
	st.slip_position_ms = _slipHiddenPositionAt(_rt[deck], presentedAt) * 1000;
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
		(effectiveWhen) => _slipHiddenPositionAt(rt, effectiveWhen),
		true,
		undefined,
		undefined,
		null
	);
	_clearSlip(deck);
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

/**
 * The lead an IMMEDIATE (Class A) transport mutation must give deck `deck`'s
 * processor, derived from the measured ONSET RAMP rather than the processor's
 * `latency()` self-report (round 2; see `processorOnsetLeadSec`).
 *
 * Every plain-transport schedule floor on this engine goes through here, so
 * play, pause, cue, seek, loop entry, key shift, the natural-end stop and the
 * safety loop all get the same term from one place and cannot drift apart.
 *
 * BEAT SYNC deliberately does NOT use it: a group launch is planned from
 * `maxLatency` (the raw self-report) plus `SYNC_SCHEDULE_SAFETY_S`, which keeps
 * the shared instant comfortably clear of every participant's ramp.
 */
function _transportLeadSec(deck: DeckId): number {
	return processorOnsetLeadSec(_rt[deck].latencySec);
}

/**
 * UNIFORMITY: every loaded deck must report the same processor latency.
 *
 * For the Signalsmith worklet `latency()` equals the STFT block length exactly,
 * and the block length sets the onset ramp (0.37x, see
 * `PROCESSOR_ONSET_RAMP_FACTOR`). Two decks configured with different blocks
 * therefore have different ramps, and two different ramps launched at ONE
 * shared beat-sync instant do not start together: one deck is still ramping
 * while the other is at level, so the group's first beat smears. That is the
 * constraint the round-2 design names as the price of making the block
 * configurable at all, and it is why STEP 2 must be a global setting rather
 * than a per-deck "high quality mode".
 *
 * Checked at LOAD, before the candidate processor is published, so a mixed
 * fleet fails the load that would have created it rather than going audibly
 * wrong later under sync. Tolerance is one sample: `latency()` quantises the
 * block to whole samples, so exact equality would be brittle across rates.
 */
function _assertUniformProcessorBlock(
	deck: DeckId,
	latencySec: number,
	sampleRateHz: number
): void {
	if (!Number.isFinite(sampleRateHz) || sampleRateHz <= 0) {
		throw new RangeError(`sampleRateHz must be finite and positive, got ${sampleRateHz}`);
	}
	const toleranceSec = 1 / sampleRateHz;
	for (const other of DECK_IDS) {
		if (other === deck) continue;
		const otherRuntime = _rt[other];
		if (otherRuntime.processor === null) continue;
		if (Math.abs(otherRuntime.latencySec - latencySec) <= toleranceSec) continue;
		throw new Error(
			`deck ${deck} processor reports ${latencySec}s latency but loaded deck ${other} ` +
				`reports ${otherRuntime.latencySec}s. latency() equals the STFT block for this ` +
				'processor, so a mixed fleet means mixed onset ramps, and a beat-sync group ' +
				'launched at one shared instant would smear its first beat. Block configuration ' +
				'must be global (see STRETCH_BLOCK_MS), never per deck.'
		);
	}
}

function _futureScheduleTime(deck: DeckId): number {
	if (_ctx === null) throw new Error('_futureScheduleTime: audio graph not initialised');
	const minimumSafeWhen = safeTransportScheduleTime(_ctx.currentTime, _transportLeadSec(deck));
	const rt = _rt[deck];
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

/**
 * Transport position read from the AUDIO CLOCK rather than the rAF-published
 * mirror in `deckStates[deck].position_ms`.
 *
 * That mirror only advances inside `_tick`, which runs on requestAnimationFrame
 * and which the browser stops entirely while the tab is in the background. Any
 * decision that must keep being made while the user is on another tab has to
 * read the clock directly, or it silently stalls: the mirror freezes at
 * whatever it last published, so derived remaining-time never enters its
 * trigger window and nothing ever fires. AutoPlay's end-of-track handoff is
 * exactly that shape, and stopped the set dead at the current track whenever
 * the user tabbed away.
 *
 * This is a READ for decisions only. `_currentPosSec` stays unpublished to
 * DeckState while audio is active, per its own contract: the UI must keep
 * showing the PRESENTED position, which trails this one by output latency.
 * A paused deck has no running clock, so its published cursor is the truth -
 * the same split `_scheduleSeek` already makes.
 */
export function deckAudioClockPositionMs(deck: DeckId): number {
	const st = deckStates[deck];
	if (!st.playing) return st.position_ms;
	return _currentPosSec(deck) * 1000;
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

function _publishPresentedTransport(
	deck: DeckId,
	outputTimestamp: { contextTime: number; performanceTime: number }
): PresentedTransportObservation | null {
	const rt = _rt[deck];
	if (rt.processor === null || rt.durationSec <= 0) return null;
	// The sample clock is handed over so a stalled HAL output position falls back
	// to it rather than freezing the waveform. See
	// .planning/hardening-ledger/decisions/presentation-clock-fallback.md.
	const observation = observePresentedTransportTimeline(
		rt.presentation,
		outputTimestamp,
		rt.durationSec,
		_ctx === null ? undefined : _ctx.currentTime
	);
	notePresentationClock(deck, observation.clock_stalled);
	if (!observation.accepted) return observation;
	const st = deckStates[deck];
	const wasAudible = st.audible;
	st.position_ms = observation.position_sec * 1000;
	st.audible = observation.audible;
	st.transport_pending = observation.transport_pending || rt.reanchorRampActive;
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
		const safety = st.safety_loop;
		if (
			safety !== null &&
			safety.armed &&
			(st.loop === null || !st.loop.engaged) &&
			safety.out_ms > safety.in_ms
		) {
			const nextLoop: LoopState = {
				in_ms: safety.in_ms,
				out_ms: safety.out_ms,
				engaged: true,
				beat_length: safety.beat_length
			};
			void _scheduleDeck(
				deck,
				safeTransportScheduleTime(_ctx.currentTime, _transportLeadSec(deck)),
				safety.in_ms / 1000,
				true,
				undefined,
				undefined,
				nextLoop
			).catch((error: unknown) => {
				if (rt.processor !== null) _recordProcessorFailure(deck, error);
			});
			return observation;
		}
		void _scheduleDeck(
			deck,
			safeTransportScheduleTime(_ctx.currentTime, _transportLeadSec(deck)),
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

/**
 * One frame of presentation, guarded. `_rafId` is nulled FIRST, so before this
 * guard any throw left no pending frame and no handler, and the only re-arm was
 * `_ensureRaf()` in `_scheduleDeck` - which free-running playback never calls.
 * One throw killed the waveform for the session while audio played on. A
 * failing frame is RE-ARMED rather than abandoned, because a frozen playhead is
 * the defect being fixed; the error row is rate-limited by perf-event-log.
 */
function _tick(): void {
	_rafId = null;
	if (_ctx === null) return;
	let anyTransport = false;
	try {
		const outputTimestamp = _readOutputTimestamp(_ctx);
		for (const deck of DECK_IDS) {
			_commitPendingIfDue(deck);
			const observation = _publishPresentedTransport(deck, outputTimestamp);
			_updateSlipPosition(deck);
			if (observation?.audible || observation?.transport_pending) anyTransport = true;
		}
		// Feed TopBar audio-Hz meter (presentation publish rate ~= game FPS).
		noteMasterSilence(_masterAnalyser, anyTransport, Date.now());
		if (anyTransport) noteAudioPresentationTick();
	} catch (error: unknown) {
		notePresentationTickFailure(error);
		// Unconditional, and through the guarded entry point: the throw may have
		// happened before anyTransport was known, and a deck that IS playing must
		// not lose its clock to one bad frame.
		_ensureRaf();
		return;
	}
	if (anyTransport) _rafId = requestAnimationFrame(_tick);
}

function _ensureRaf(): void {
	if (_rafId === null) _rafId = requestAnimationFrame(_tick);
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
	st.key_sync_enabled = false;
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
	_rt[st.deck_id].slipTempoBoundaries = [];
	_rt[st.deck_id].keySyncBaselineSemitones = null;
	_rt[st.deck_id].reanchorRampActive = false;
	st.hot_cues = [];
	st.has_rb_mapping = true;
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
	// Belt for the statechange listener: whichever fires first, the authoritative
	// device-floor row is emitted exactly once (the helper is idempotent).
	stampContextDeviceFloors(ctx);
	return ctx;
}

export interface ContextTimeSource {
	readonly currentTime: number;
	readonly state: string;
}

/**
 * The two time primitives the context-time wait below is built on: the
 * millisecond reading it measures stall progress against, and the sleep it
 * parks on between polls. They are injectable for one reason - the wait's
 * contract is "give up within `stallTimeoutMs` of the last observed
 * progress", and that is a statement about scheduling arithmetic, not about
 * how punctually a loaded machine delivers a timer callback. A test that
 * drives a virtual clock checks the arithmetic; a test that times real
 * `setTimeout` calls checks the host's spare CPU.
 */
export interface ContextWaitClock {
	nowMs(): number;
	sleep(ms: number): Promise<void>;
}

export const REAL_CONTEXT_WAIT_CLOCK: ContextWaitClock = {
	nowMs: () => Date.now(),
	sleep: (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms))
};

export async function waitForAdvancingContextTime(
	ctx: ContextTimeSource,
	targetContextTime: number,
	stillCurrent: () => boolean = () => true,
	stallTimeoutMs: number = CONTEXT_WAIT_STALL_TIMEOUT_MS,
	clock: ContextWaitClock = REAL_CONTEXT_WAIT_CLOCK
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
	let lastProgressAtMs = clock.nowMs();
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
			lastProgressAtMs = clock.nowMs();
		}
		const stallRemainingMs = stallTimeoutMs - (clock.nowMs() - lastProgressAtMs);
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
		await clock.sleep(delayMs);
	}
}

interface _MasterSyncSchedule {
	tempoRatio: number;
	masterTempoEnabled: boolean;
	/** When set, relocate master to this track position as part of the sync. */
	positionSec?: number;
}

interface _SyncOptions {
	followerAnchorSec?: Partial<Record<DeckId, number>>;
	masterSchedule?: _MasterSyncSchedule;
	/**
	 * Followers that were already playing and already beat-synced before
	 * this call - a re-anchor of an ongoing lock, not a fresh join or the
	 * first Beat Sync engage. Each caller below already restricts itself to
	 * exactly that precondition (see seekSyncMaster, beatSyncMaxFollowers,
	 * masterSwitchFollowers, syncChangeRequiresReschedule), so it passes the
	 * same followers through here unchanged. Their recomputed
	 * followerTempoRatio approaches its target through
	 * `_scheduleReanchoredFollower` instead of stepping - see that function.
	 */
	reanchorDecks?: ReadonlySet<DeckId>;
}

/**
 * When a synced waveform seek snaps backwards, crossfade a little from the
 * current bar into beat 4 of the target bar so the landing at `landingSec`
 * is less of a hard cut. Falls back to a plain schedule when no buffer /
 * lead-in is available.
 */
async function _scheduleFollowerBackwardBlend(
	deck: DeckId,
	syncAt: number,
	landingSec: number,
	tempoRatio: number,
	masterTempoEnabled: boolean,
	currentSec: number
): Promise<number> {
	const { st, rt } = _requireLoaded(deck, 'sync seek blend');
	const ctx = _ctx;
	const buffer = rt.audioBuffer;
	const nodes = rt.nodes;
	const processor = rt.processor;
	if (
		ctx === null ||
		buffer === null ||
		nodes === null ||
		processor === null ||
		landingSec >= currentSec - 0.08
	) {
		return _scheduleDeck(deck, syncAt, landingSec, true, tempoRatio, masterTempoEnabled);
	}

	const beats = st.anlz?.beatgrid.beats ?? null;
	const beat4 =
		beats !== null && beats.length >= 2 ? beatFourLeadInSec(beats, landingSec) : null;
	const incomingSec =
		beat4 !== null && beat4 < landingSec - 0.05 && landingSec - beat4 <= 2.2
			? beat4
			: landingSec;
	const blendDur = syncSeekBlendDurationSec(incomingSec, landingSec, tempoRatio);
	const t0 = Math.max(ctx.currentTime + 0.02, syncAt - blendDur);
	const tEnd = t0 + blendDur;

	processor.disconnect();
	const mainGain = ctx.createGain();
	const outGain = ctx.createGain();
	processor.connect(mainGain);
	mainGain.connect(nodes.analyser);
	outGain.connect(nodes.analyser);

	const outSrc = ctx.createBufferSource();
	outSrc.buffer = buffer;
	outSrc.playbackRate.value = tempoRatio;
	outSrc.connect(outGain);

	mainGain.gain.setValueAtTime(0.0001, t0);
	mainGain.gain.linearRampToValueAtTime(1, tEnd);
	outGain.gain.setValueAtTime(1, t0);
	outGain.gain.linearRampToValueAtTime(0.0001, tEnd);

	const startOffset = Math.min(Math.max(0, currentSec), Math.max(0, buffer.duration - 0.01));
	try {
		outSrc.start(t0, startOffset);
	} catch {
		try {
			mainGain.disconnect();
			outGain.disconnect();
		} catch {
			/* ignore */
		}
		try {
			processor.connect(nodes.analyser);
		} catch {
			/* ignore */
		}
		return _scheduleDeck(deck, syncAt, landingSec, true, tempoRatio, masterTempoEnabled);
	}

	const scheduled = await _scheduleDeck(
		deck,
		t0,
		incomingSec,
		true,
		tempoRatio,
		masterTempoEnabled
	);

	const token = rt.loadToken;
	const delayMs = Math.max(0, (tEnd - ctx.currentTime) * 1000) + 50;
	window.setTimeout(() => {
		try {
			outSrc.stop();
		} catch {
			/* already ended */
		}
		try {
			outSrc.disconnect();
			outGain.disconnect();
			mainGain.disconnect();
		} catch {
			/* ignore */
		}
		if (rt.loadToken !== token || rt.processor !== processor || rt.nodes === null) return;
		try {
			processor.disconnect();
			processor.connect(rt.nodes.analyser);
		} catch {
			/* ignore */
		}
	}, delayMs);

	return scheduled;
}

/**
 * Re-anchoring an already-playing, already-synced follower can recompute a
 * different followerTempoRatio purely from grid noise near the new anchor
 * (`_windowedIntervalBpm`'s window shifts by a few beats). Landing on it in
 * one step turns that noise into an audible tempo jump. Position and phase
 * still lock exactly at `syncAt` in this same call, unchanged from the
 * non-ramped path - only the RATE eases toward its target afterward, via
 * `planTempoRatioRamp`'s small steps scheduled on the real AudioContext
 * clock (never a JS timer racing the audio graph).
 *
 * The first step is awaited so this resolves with the same phase-lock
 * timing callers already depend on; the remaining steps continue on this
 * deck's own serialized schedule queue (`_scheduleDeck`'s `rt.scheduleTail`)
 * without being awaited here, so they cannot extend how long the shared
 * `sync` command scope stays claimed - only this deck's own scope, exactly
 * like an ordinary follow-up mutation on that deck.
 *
 * `rt.reanchorRampActive` holds `transport_pending` true for this deck across
 * the whole ramp. Each step's own schedule revision becomes "presented" as
 * soon as real playback reaches it - which can happen before the *next*
 * step's own worklet round-trip finishes registering - so without this
 * override a caller polling `!transport_pending` could catch that gap and
 * read/measure position while the rate is still mid-transition.
 */
async function _scheduleReanchoredFollower(
	deck: DeckId,
	syncAt: number,
	inputSec: number,
	toTempoRatio: number,
	masterTempoEnabled: boolean
): Promise<number> {
	const fromTempoRatio = _tempoAt(deck, syncAt);
	const ramp = planTempoRatioRamp(fromTempoRatio, toTempoRatio);
	const rt = _rt[deck];
	rt.reanchorRampActive = true;
	let scheduledInputSec: number;
	try {
		scheduledInputSec = await _scheduleDeck(
			deck,
			syncAt,
			inputSec,
			true,
			ramp[0].tempoRatio,
			masterTempoEnabled
		);
	} catch (error) {
		rt.reanchorRampActive = false;
		throw error;
	}
	void _continueTempoRamp(deck, syncAt, ramp.slice(1), masterTempoEnabled);
	return scheduledInputSec;
}

/** Background tail of a re-anchor ramp. Position is deliberately left to
 * `_projectPositionAt` (the same projection every other tempo-only mutation
 * here uses) rather than re-stated from the plan, since only the rate is
 * changing at each step. Always clears `rt.reanchorRampActive` on the way
 * out, success or not - a superseded/abandoned ramp must not leave this
 * deck's `transport_pending` stuck true forever. */
async function _continueTempoRamp(
	deck: DeckId,
	syncAt: number,
	remainingSteps: readonly TempoRampStep[],
	masterTempoEnabled: boolean
): Promise<void> {
	const rt = _rt[deck];
	try {
		for (const step of remainingSteps) {
			try {
				await _scheduleDeck(
					deck,
					syncAt + step.offsetSec,
					(effectiveWhen) => _projectPositionAt(deck, effectiveWhen),
					true,
					step.tempoRatio,
					masterTempoEnabled
				);
			} catch {
				// The deck moved on (reload/unload/a newer command) mid-ramp;
				// abandon the rest rather than fight whatever superseded it.
				return;
			}
		}
	} finally {
		rt.reanchorRampActive = false;
	}
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
		// Deliberately the RAW self-report, not the round-2 onset lead. A group
		// launch has a constraint a single deck does not: the shared instant must
		// clear the WORST participant's onset ramp, or one deck is still ramping
		// while another is at level and the group's first beat smears. Keeping
		// maxLatency here leaves the sync lead at latency + 100ms, i.e. ~2.7x the
		// ramp, so that constraint holds with margin and holds automatically at
		// any block size. Shrinking it is a separate, gated change.
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
		const projectedMasterSec = _projectPositionAt(master, syncAt);
		const masterPositionSec = options.masterSchedule?.positionSec ?? projectedMasterSec;
		const masterTempoRatio = options.masterSchedule?.tempoRatio ?? _tempoAt(master, syncAt);
		// Plan per follower independently. One unsyncable deck (e.g. BAR tempo
		// out of range) must not abort BeatSyncMax for the rest - that left
		// CH1/CH3 desynced when CH2 could not lock.
		type _PlannedFollower = {
			deck: DeckId;
			st: DeckState;
			plan: ReturnType<typeof computeFollowerSyncPlan>;
			rawFollowerPositionSec: number;
		};
		const planned: _PlannedFollower[] = [];
		const planFailed: { deck: DeckId; message: string }[] = [];
		for (const deck of followers) {
			try {
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
				planned.push({ deck, st, plan, rawFollowerPositionSec });
			} catch (error) {
				const message = error instanceof Error ? error.message : String(error);
				planFailed.push({ deck, message });
				deckStates[deck].sync_error = message;
			}
		}
		if (planned.length === 0 && options.masterSchedule === undefined) {
			const detail =
				planFailed.length > 0
					? planFailed.map((f) => `deck ${f.deck}: ${f.message}`).join('; ')
					: 'no followers';
			throw new Error(`Beat Sync: no followers could phase-lock (${detail})`);
		}
		const schedules = [
			...(options.masterSchedule === undefined
				? []
				: [
						{
							deck: master,
							st: masterState,
							inputSec: masterPositionSec,
							tempoRatio: options.masterSchedule.tempoRatio,
							masterTempoEnabled: options.masterSchedule.masterTempoEnabled,
							blendFromSec:
								options.masterSchedule.positionSec !== undefined &&
								masterPositionSec < projectedMasterSec - 0.08
									? projectedMasterSec
									: (null as number | null)
						}
					]),
			...planned.map((item) => ({
				deck: item.deck,
				st: item.st,
				inputSec: item.plan.followerPositionSec,
				tempoRatio: item.plan.followerTempoRatio,
				masterTempoEnabled: item.st.master_tempo_enabled,
				blendFromSec:
					options.followerAnchorSec?.[item.deck] !== undefined &&
					item.plan.followerPositionSec < item.rawFollowerPositionSec - 0.08
						? item.rawFollowerPositionSec
						: null
			}))
		];
		const scheduleTimes = commonSyncScheduleTimes(syncAt, schedules.length);
		const outcomes = await Promise.allSettled(
			schedules.map((item, index) => {
				if (item.blendFromSec !== null) {
					return _scheduleFollowerBackwardBlend(
						item.deck,
						scheduleTimes[index],
						item.inputSec,
						item.tempoRatio,
						item.masterTempoEnabled,
						item.blendFromSec
					);
				}
				if (options.reanchorDecks?.has(item.deck) === true) {
					return _scheduleReanchoredFollower(
						item.deck,
						scheduleTimes[index],
						item.inputSec,
						item.tempoRatio,
						item.masterTempoEnabled
					);
				}
				return _scheduleDeck(
					item.deck,
					scheduleTimes[index],
					item.inputSec,
					true,
					item.tempoRatio,
					item.masterTempoEnabled
				);
			})
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
			for (const item of schedules) {
				if (failedDecks.includes(item.deck)) item.st.sync_error = message;
			}
			throw new Error(message, {
				cause: outcomes.find((outcome) => outcome.status === 'rejected')
			});
		}
		for (const item of planned) item.st.sync_error = null;
		if (planFailed.length > 0) {
			const skipped = planFailed.map((f) => f.deck).join(',');
			pushToast(
				`Beat Sync skipped deck(s) [${skipped}] (tempo/phase cannot lock) - others stayed locked`,
				'error'
			);
			for (const f of planFailed) {
				recordPerfEvent('beat-sync-skip', f.message, f.deck);
			}
		}
	} catch (error) {
		// Only stamp followers that do not already carry a more specific plan error.
		for (const deck of followers) {
			if (deckStates[deck].sync_error === null) {
				deckStates[deck].sync_error = String(error);
			}
		}
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

/** LAZY-STEMS. True when the deck can have its processor replaced right now.
 * Same predicate the engine has always enforced for load/unload, asked as a
 * question instead of thrown as an assertion, because a background upgrade
 * arriving mid-set is an EXPECTED state to wait out, not a fault. */
function _deckIsReplaceable(deck: DeckId): boolean {
	const st = deckStates[deck];
	const rt = _rt[deck];
	try {
		assertDeckReplacementAllowed(deck, {
			playing: st.playing,
			audible: st.audible,
			transportPending: st.transport_pending,
			controlActive: rt.controlActive,
			pendingScheduleCount: rt.pending.length,
			scheduleIntentCount: rt.scheduleIntentCount
		});
		return true;
	} catch {
		return false;
	}
}

/**
 * LAZY-STEMS. Put a prepared AlignedStemDeckProcessor in place of the deck's
 * mix processor. The caller MUST have established that the deck is replaceable.
 *
 * Playhead safety: a stopped deck's position lives in `st.position_ms` (what
 * `play` resumes from) and `rt.presentation`. This function touches NEITHER, so
 * a deck cued to 1:30 is still at 1:30 after the swap. That is the whole reason
 * it is not written in terms of the load-time swap, which deliberately zeroes
 * both because it is publishing a DIFFERENT track.
 */
function _adoptStemProcessor(
	deck: DeckId,
	upgrade: { processor: AlignedStemDeckProcessor; state: StemDeckState }
): void {
	const st = deckStates[deck];
	const rt = _rt[deck];
	if (rt.nodes === null) throw new Error(`stem upgrade: deck ${deck} audio graph is missing`);
	const retired = rt.processor;
	upgrade.processor.connect(rt.nodes.analyser);
	rt.processor = upgrade.processor;
	st.stems = upgrade.state;
	if (retired !== null) _retireProcessor(retired);
}

/** LAZY-STEMS. Detach and silence a held stem upgrade that will never land
 * (deck unloaded, engine disposed, track swapped). Without this the prepared
 * worklet nodes stay connected to a context nobody owns any more: the exact
 * "PCM should drop to 0 on unload" property the perf log treats as the leak
 * signature. Safe to call when nothing is pending. */
function _releasePendingStemUpgrade(rt: _DeckRuntime): void {
	const pending = rt.pendingStemUpgrade;
	if (pending === null) return;
	rt.pendingStemUpgrade = null;
	_retireProcessor(pending.processor);
}

/** LAZY-STEMS. Called wherever a deck comes to rest, to land a stem upgrade
 * that finished while the deck was playing. No-op when nothing is pending. */
function _drainPendingStemUpgrade(deck: DeckId): void {
	const rt = _rt[deck];
	const pending = rt.pendingStemUpgrade;
	if (pending === null) return;
	if (pending.token !== rt.loadToken) {
		// The deck moved on to another track while these stems were decoding.
		rt.pendingStemUpgrade = null;
		_retireProcessor(pending.processor);
		return;
	}
	if (!_deckIsReplaceable(deck)) return;
	rt.pendingStemUpgrade = null;
	_adoptStemProcessor(deck, pending);
}

/**
 * LAZY-STEMS. The whole secondary load: probe, fetch, decode, build, swap.
 * Runs AFTER the deck is playable and is never awaited by `load`.
 *
 * Fail-fast: this never leaves the deck claiming a capability it does not have.
 * Every exit either settles `st.stems` to `ready` (processor actually in the
 * graph), `unavailable` (no bundle - a settled answer), or `error` (the bundle
 * exists but could not be used, with the reason). A deck that ends up anything
 * other than `ready` still plays; only the stem CONTROLS are absent, and
 * `_setStemControl` already refuses loudly for any non-`ready` status.
 */
async function _upgradeDeckStems(
	deck: DeckId,
	stableId: string,
	token: number,
	ctx: AudioContext,
	mixBuffer: AudioBuffer
): Promise<void> {
	const rt = _rt[deck];
	const st = deckStates[deck];
	const t0 = performance.now();
	const stages: Record<string, number> = {};
	const time = async <T>(name: string, work: Promise<T>): Promise<T> => {
		const started = performance.now();
		try {
			return await work;
		} finally {
			stages[name] = Math.round(performance.now() - started);
		}
	};
	const stale = (): boolean => token !== rt.loadToken;
	let built: AlignedStemDeckProcessor | null = null;
	try {
		const probe = await time('probeStem', probeStemArtifact(stableId));
		if (stale()) return;
		if (probe.status !== 'ready') {
			// A settled "this track has no bundle". Not an error, and not a
			// spinner: the deck is finished loading.
			st.stems = unavailableStemDeckState(probe.error);
			stages.total = Math.round(performance.now() - t0);
			recordPerfTiming(`deck-stems-none sid=${stableId.slice(0, 12)}`, stages, deck);
			return;
		}
		const layout = probe.manifest.layout;
		const layoutParts = STEM_LAYOUT_PART_NAMES[layout];
		const encodedParts = await time(
			'fetchStems',
			fetchStemAudioArrayBuffers(stableId, layout)
		);
		if (stale()) return;
		const decodedEntries = await time(
			'decodeStems',
			Promise.all(
				layoutParts.map(
					async (part) =>
						[part, await ctx.decodeAudioData(encodedParts[part] as ArrayBuffer)] as const
				)
			)
		);
		if (stale()) return;
		const stemBuffers = Object.fromEntries(decodedEntries) as StemBuffers;
		const created = await time(
			'stemProcessorCreate',
			AlignedStemDeckProcessor.create(ctx, stemBuffers, {
				onProcessorError: (error: unknown) => {
					if (_rt[deck].processor === built) _recordProcessorFailure(deck, error);
				}
			})
		);
		built = created.processor;
		if (stale()) {
			_retireProcessor(built);
			return;
		}
		if (
			created.alignment.sample_rate_hz !== mixBuffer.sampleRate ||
			created.alignment.frame_count !== mixBuffer.length ||
			Math.abs(created.alignment.duration_ms - mixBuffer.duration * 1000) >
				1000 / mixBuffer.sampleRate
		) {
			_retireProcessor(built);
			throw new Error(
				`stem/source alignment mismatch: source ${mixBuffer.sampleRate}Hz, ` +
					`${mixBuffer.length} frames, ${mixBuffer.duration}s; stems ` +
					`${created.alignment.sample_rate_hz}Hz, ${created.alignment.frame_count} ` +
					`frames, ${created.alignment.duration_ms / 1000}s`
			);
		}
		const readyState = readyStemDeckState(
			{ source: probe.manifest.source, model: probe.manifest.model, layout },
			created.alignment
		);
		await _withDeckSwap(rt, async () => {
			if (stale()) {
				if (built !== null) _retireProcessor(built);
				built = null;
				return;
			}
			if (!_deckIsReplaceable(deck)) {
				// Playing. The engine forbids replacing a live processor, so hold
				// the finished bundle and land it on the next stop rather than
				// glitching the output mid-phrase.
				rt.pendingStemUpgrade = {
					token,
					processor: created.processor,
					state: readyState
				};
				built = null;
				stages.deferredToStop = 1;
				return;
			}
			_adoptStemProcessor(deck, { processor: created.processor, state: readyState });
			built = null;
		});
		stages.total = Math.round(performance.now() - t0);
		recordPerfTiming(`deck-stems sid=${stableId.slice(0, 12)}`, stages, deck);
	} catch (error) {
		if (built !== null) _retireProcessor(built);
		if (stale()) return;
		const message = error instanceof Error ? error.message : String(error);
		st.stems = { ...unavailableStemDeckState(message), status: 'error' };
		stages.failedAt = Math.round(performance.now() - t0);
		recordPerfTiming(`deck-stems-fail sid=${stableId.slice(0, 12)}`, stages, deck);
		pushToast(`Deck ${deck} stems unavailable - ${message}`, 'error');
	}
}

/**
 * The monitor's tap into the engine graph, handed to the headphone module as a
 * thunk. Resolved lazily on purpose: a headphone selection must still build the
 * graph at the point INSIDE its try block where it always did, so a graph-build
 * failure is still reported as `headphone output selection failed`.
 */
function _monitorSource(): { context: AudioContext; masterGain: GainNode } {
	const context = _ensureGraph();
	if (_masterGain === null) throw new Error('headphone monitor master gain is missing');
	return { context, masterGain: _masterGain };
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
			_releasePendingStemUpgrade(rt);
			const processor = detachProcessorForDisposal(rt);
			if (processor !== null) processors.push(processor);
			if (rt.nodes !== null) {
				nodes.push(...Object.values(rt.nodes).filter((node): node is AudioNode => node !== null));
			}
		}
		disposeHeadphoneMonitor();
		disarmContextInstrumentation();
		if (_masterMuteGain !== null) nodes.push(_masterMuteGain);
		const closing = disposeAudioResources({
			rafId: _rafId,
			processors,
			nodes,
			masterGain: _masterGain,
			context: _ctx
		});

		_rafId = null;
		_masterGain = null;
		// The mute VALUE survives teardown on purpose: a route remount must not
		// hand a headless agent its audio back. Only the node is released.
		attachMasterMuteNode(null);
		_masterMuteGain = null;
		_externalMerger = null;
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
		let hotCueSlots: HotCueSlotState[] | null = null;
		let latencySec = 0;
		let processor: _DeckProcessor | null = null;
		let candidateStemState: StemDeckState = unavailableStemDeckState();
		// Hoisted so the deferred stem upgrade decodes into the SAME context the
		// mix decoded into; re-resolving it after the swap could hand the stems a
		// rebuilt graph and a silent sample-rate mismatch.
		let loadCtx: AudioContext | null = null;
		// Always-on stage timings -> recordPerfTiming / DevTools filter `[perf]`.
		const perfT0 = performance.now();
		const perfMs = (): number => Math.round(performance.now() - perfT0);
		const stages: Record<string, number> = {};
		const time = async <T>(name: string, work: Promise<T>): Promise<T> => {
			const t0 = performance.now();
			try {
				return await work;
			} finally {
				stages[name] = Math.round(performance.now() - t0);
			}
		};
		try {
			// SPIKE-PERF: reuse a ready FE anlz cache entry (select prefetch / prior load).
			const cachedAnlz = getAnlzEntry(stable_id);
			const anlzCached = isAnlzEntryUsable(cachedAnlz);
			// A direct (uncached) fetch never blocks the load out waiting on a
			// momentarily saturated decoder (Codex finding, issue #735 follow-up,
			// discussion_r3907610439): `anlz` below must be non-null for this
			// load to publish at all, so accepting even a retryable reject here
			// is required to finish the load. WaveRow's own effect keeps asking
			// for a fetch while the published anlz is still the retryable class
			// (deckAnlzNeedsFetch), and resolveDisplayedAnlz picks up the
			// eventual terminal answer from the shared cache once it lands.
			const anlzPromise: Promise<AnlzWithVocals> = anlzCached
				? Promise.resolve(cachedAnlz.data as AnlzWithVocals)
				: (fetchAnlzForDeckLoad(stable_id) as Promise<AnlzWithVocals>);
			// Prefetch hit: copyPrefetchedAudio (slice) so decode cannot detach cache.
			const prefetchedAudio = copyPrefetchedAudio(stable_id);
			const audioPromise =
				prefetchedAudio !== null
					? Promise.resolve(prefetchedAudio)
					: fetchAudioArrayBuffer(stable_id);
			// LAZY-STEMS: the critical path fetches ONLY what first playback needs.
			// `probeStem` used to ride here as a fifth request and, being last in
			// the list behind a multi-MB audio download on a single-worker engine,
			// it held the fetch wall on its own (PR #601 measured 47% of it for a
			// 1-9ms endpoint). It now runs after the swap, in _upgradeDeckStems.
			const [trackRes, audioBytes, requiredAnlz, requiredHotCueSlots] =
				await Promise.all([
					time('getTrack', getTrack(stable_id)),
					time(prefetchedAudio !== null ? 'fetchAudioCacheHit' : 'fetchAudio', audioPromise),
					time(anlzCached ? 'anlzCacheHit' : 'fetchAnlz', anlzPromise),
					time('fetchHotCues', fetchHotCueSlots(stable_id))
				]);
			stages.fetchWall = perfMs();
			stages.audioBytes = audioBytes.byteLength;
			stages.audioPrefetchHit = prefetchedAudio !== null ? 1 : 0;
			const ctx = _ensureGraph();
			loadCtx = ctx;
			track = trackRes.track;
			anlz = requiredAnlz;
			hotCueSlots = requiredHotCueSlots;
			const processorOptions = {
				onProcessorError: (error: unknown) => {
					if (_rt[deck].processor === processor) _recordProcessorFailure(deck, error);
				}
			};
			// LAZY-STEMS: EVERY load now takes the mix path. The mix buffer is
			// what first playback actually needs -- a stemmed deck kept the mix
			// buffer anyway (rt.audioBuffer, for the sync blend), so nothing
			// audible is being deferred here, only the per-stem gain branches.
			// SPIKE-PERF: overlap decode with worklet create.
			const [decodedMix, mixProcessor] = await Promise.all([
				time('decodeMix', ctx.decodeAudioData(audioBytes)),
				time('stretchCreate', StretchDeckProcessor.create(ctx, processorOptions))
			]);
			buffer = decodedMix;
			await time('stretchLoad', mixProcessor.load(buffer));
			processor = mixProcessor;
			// `loading`, not `unavailable`: the probe has not run yet, so claiming
			// "no stems" here would be a guess. _upgradeDeckStems settles it.
			candidateStemState = loadingStemDeckState();
			latencySec = await time('processorLatency', processor.latencySec());
			_assertUniformProcessorBlock(deck, latencySec, ctx.sampleRate);
			stages.totalBeforeSwap = perfMs();
		} catch (exc) {
			stages.failedAt = perfMs();
			st.last_load_stages = { ...stages };
			recordDeckLoadTiming('deck-load-fail', stages, deck, candidateStemState);
			if (processor !== null) {
				try {
					await processor.dispose();
				} catch {
					// Preserve the load failure; dispose() closes the port in finally.
				}
			}
			if (token !== rt.loadToken) throw exc;
			assertDeckLoadConsistency(st.stable_id, rt.durationSec, rt.processor !== null);
			const msg =
				exc instanceof RbApiError ? `${exc.code}: ${exc.message}` : String(exc);
			deckLoadErrors[deck] = msg;
			reportDeckLoadFailure(deck, msg, exc, stages);
			throw exc;
		}
		if (
			processor === null || track === null || buffer === null || anlz === null ||
			hotCueSlots === null
		) {
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
				await candidateProcessor.dispose();
				return;
			}
			try {
				_assertCurrentDeckReplacementAllowed(deck);
			} catch (error) {
				await candidateProcessor.dispose();
				throw error;
			}
			if (rt.nodes === null || _ctx === null) {
				await candidateProcessor.dispose();
				throw new Error(`load: deck ${deck} audio graph is missing`);
			}
			const context = _ctx;
			const replacingMaster = _masterDeck === deck;
			const incumbentProcessor = rt.processor;
			try {
				candidateProcessor.connect(rt.nodes.analyser);
			} catch (error) {
				await candidateProcessor.dispose();
				if (loadCandidateCanPublish(token, rt.loadToken)) {
					const message = String(error);
					deckLoadErrors[deck] = message;
					pushToast(`Deck ${deck} load failed - ${message}`, 'error');
				}
				throw error;
			}
			// LAZY-STEMS: the outgoing track may have had a stem bundle waiting for
			// a stop that will now never come for it.
			_releasePendingStemUpgrade(rt);
			_clearLoadedTrackState(st);
			rt.processor = candidateProcessor;
			rt.durationSec = candidateBuffer.duration;
			rt.audioBuffer = candidateBuffer;
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
			// TrackOut spells every nullable field optional (a pydantic default
			// becomes a not-required property), so absent and null both land as
			// the deck's "unknown" null.
			st.title = candidateTrack.title ?? null;
			st.artist = candidateTrack.artist ?? null;
			st.bpm = candidateTrack.bpm ?? null;
			st.key = candidateTrack.key ?? null;
			// The decoded buffer is the audio actually scheduled. Metadata can
			// differ, so it must not define waveform bounds or transport truth.
			st.duration_ms = decodedTransportDurationMs(candidateBuffer.duration);
			st.anlz = candidateAnlz;
			st.anlz_error = null;
			st.processor_error = null;
			st.sync_error = null;
			st.stems = candidateStemState;
			st.hot_cues = _hotCuesFromSlots(hotCueSlots);
			st.hot_cue_revisions = _hotCueRevisionsFrom(hotCueSlots);
			st.has_rb_mapping = candidateTrack.has_rb_mapping;
			st.loop = _displayLoopFrom(candidateAnlz.cues);
			if (replacingMaster) _electPlayingMaster();
			assertDeckLoadConsistency(st.stable_id, rt.durationSec, rt.processor !== null);
			if (incumbentProcessor !== null) {
				incumbentProcessor.disconnect();
				try {
					await incumbentProcessor.stop(context.currentTime);
					await incumbentProcessor.dispose();
				} catch (error) {
					try {
						await incumbentProcessor.dispose();
					} catch {
						// The original cleanup error remains the useful diagnostic.
					}
					const message = error instanceof Error ? error.message : String(error);
					pushToast(`Deck ${deck} retired processor cleanup failed - ${message}`, 'error');
				}
			}
		});
		stages.total = perfMs();
		st.last_load_latency_ms = stages.total;
		st.last_load_stages = { ...stages };
		recordDeckLoadTiming(`deck-load sid=${stable_id.slice(0, 12)}`, stages, deck, candidateStemState);
		// LAZY-STEMS: deliberately NOT awaited. `load` resolves as soon as the
		// deck can play; the stem bundle lands afterwards and moves st.stems off
		// `loading` on its own. Errors are handled inside, so no rejection can
		// escape into an unhandled promise.
		if (loadCtx === null) throw new Error('load: audio context was never resolved');
		void _upgradeDeckStems(deck, stable_id, token, loadCtx, candidateBuffer);
	}

	/** Re-read hot cues + display loop after a SAVE/CLEAR, without touching
	 * the audio graph/buffer/transport a full load() would disturb. Forces
	 * past the /anlz 1h HTTP cache and syncs the shared anlz-cache module
	 * fetchAnlz bypasses, so a later load() sees the mutation (#877) - on a
	 * partial failure that shared entry is evicted, not left stale. */
	async refreshHotCues(deck: DeckId): Promise<void> {
		const { st } = _requireLoaded(deck, 'refreshHotCues');
		const stableId = st.stable_id;
		if (stableId === null) throw new Error('refreshHotCues: deck has no stable_id');
		const [fresh, slots] = await Promise.all([
			fetchAnlzBypassingHttpCache(stableId),
			fetchHotCueSlots(stableId)
		]).catch((err: unknown) => {
			invalidateAnlzCacheEntry(stableId);
			throw err;
		});
		if (st.stable_id !== stableId) return; // deck was swapped mid-request
		refreshAnlzCacheEntry(stableId, fresh);
		st.anlz = fresh;
		st.hot_cues = _hotCuesFromSlots(slots);
		st.hot_cue_revisions = _hotCueRevisionsFrom(slots);
		st.loop = _displayLoopFrom(fresh.cues);
	}

	/** Q1: `pressT0Ms` is the operator's input stamp - see `$lib/rb/press-stamp`. */
	async play(deck: DeckId, pressT0Ms?: number): Promise<void> {
		const { st, rt } = _requireLoaded(deck, 'play');
		if (rt.desiredActive) return; // transport already running is a valid state
		// NOT st.beat_sync_enabled: the flag defaults ON, and a track with no
		// real grid has nothing to phase-lock with. Reading the effective flag
		// routes such a deck down the plain-transport branch instead of
		// refusing to start it at all.
		const syncActive = effectiveBeatSync(st);
		const needsScheduledMutation = transportNeedsScheduledMutation({
			playing: st.playing,
			audible: st.audible,
			controlActive: rt.controlActive,
			presentationPending: _presentationPending(rt),
			pendingScheduleCount: rt.pending.length,
			scheduleIntentCount: rt.scheduleIntentCount
		});
		// Finished tracks restart from 0 via the paused-seek cursor (not an
		// optimistic playing position); schedule then publishes presentation.
		const resumeSec = playResumePositionSec(
			st.position_ms / 1000,
			rt.durationSec,
			st.loop
		);
		if (!needsScheduledMutation) _setPausedPosition(deck, resumeSec * 1000);
		const ctx = await _resumeContext();
		const startSec: number | ((effectiveWhen: number) => number) = needsScheduledMutation
			? (effectiveWhen) =>
					playResumePositionSec(
						_projectPositionAt(deck, effectiveWhen),
						rt.durationSec,
						st.loop
					)
			: resumeSec;
		const activeMaster = _syncMaster();
		if (activeMaster === null) {
			// Nothing is playing, so this deck is about to BECOME master: there is
			// no other transport to align with and no reason to pay the beat-sync
			// margin. Immediate-transport safety (LATENCY-01), led by the onset
			// ramp rather than the processor's self-report (LATENCY round 2).
			const when = safeTransportScheduleTime(ctx.currentTime, _transportLeadSec(deck));
			try {
				await _schedulePress(deck, when, startSec, true, pressT0Ms);
				_assignMaster(deck);
				st.sync_error = null;
			} catch (error) {
				st.sync_error = String(error);
				throw error;
			}
		} else if (activeMaster === deck || !syncActive) {
			// Either this deck IS the master, or Beat Sync is not in effect on
			// it (switched off, or lit but with no grid to lock to). Sync is not
			// in play, so this is plain transport (LATENCY-01), led by the onset
			// ramp (LATENCY round 2).
			const when = safeTransportScheduleTime(ctx.currentTime, _transportLeadSec(deck));
			await _schedulePress(deck, when, startSec, true, pressT0Ms);
			st.sync_error = null;
		} else {
			// This deck is joining from silence (guarded by the desiredActive
			// check above) - no audible tempo to protect yet, so no
			// reanchorDecks here; the initial lock applies immediately.
			await _synchronizeFollowers(activeMaster, [deck]);
		}
	}

	/** Q1: see `play` for the `pressT0Ms` contract. */
	async pause(deck: DeckId, pressT0Ms?: number): Promise<void> {
		const { st, rt } = _requireLoaded(deck, 'pause');
		if (!rt.desiredActive) return; // already paused is a valid state
		if (_ctx === null) throw new Error('pause: audio graph not initialised');
		// Unrefusable by construction: with no grid the memory cue lands on the
		// exact pause point instead of a snapped one. A deck that cannot be
		// stopped is the worst failure this transport has.
		const pauseBeats = _quantizeGrid(st);
		const when = _futureScheduleTime(deck);
		const positionSec = await _schedulePress(
			deck,
			when,
			(effectiveWhen) => _projectPositionAt(deck, effectiveWhen),
			false,
			pressT0Ms
		);
		const cueMs = pauseBeats !== null
			? quantizedPositionMs(pauseBeats, positionSec * 1000, true)
			: positionSec * 1000;
		st.cue_ms = cueMs;
		if (st.slip_active) _clearSlip(deck);
		// LAZY-STEMS: the deck has just come to rest, so a stem bundle that
		// finished decoding mid-play can land now without touching live audio.
		_drainPendingStemUpgrade(deck);
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
		const seekBeats = _quantizeGrid(st);
		const targetMs = seekBeats !== null ? quantizedPositionMs(seekBeats, ms, true) : ms;
		if (targetMs > durMs) {
			throw new RangeError(`cueJump: quantized target ${targetMs} exceeds duration ${durMs}`);
		}
		// Rekordbox: seeking outside an engaged loop exits the loop and plays
		// from the clicked point. Keep modulo wrap only for in-loop transport.
		// Clear the loop BEFORE phase sync so the shared schedule path does not
		// wrap the target back into the old loop (BeatSyncMax / follower sync).
		const exitLoop =
			st.loop !== null &&
			st.loop.engaged &&
			(targetMs < st.loop.in_ms || targetMs >= st.loop.out_ms);
		if (exitLoop) st.loop = null;
		const scheduleLoop: LoopState | null | undefined = exitLoop ? null : undefined;
		const needsScheduledMutation = transportNeedsScheduledMutation({
			playing: rt.desiredActive,
			audible: st.audible,
			controlActive: rt.controlActive,
			presentationPending: _presentationPending(rt),
			pendingScheduleCount: rt.pending.length,
			scheduleIntentCount: rt.scheduleIntentCount
		});
		if (needsScheduledMutation) {
			const activeMaster = _syncMaster();
			const syncPlan = planSeekSync({
				deck,
				transportActive: rt.desiredActive,
				beatSyncEnabled: effectiveBeatSync(st),
				activeMaster,
				beatSyncMax: uiPrefs.beat_sync_max,
				candidates: DECK_IDS.map((id) => ({
					id,
					// desiredActive covers the schedule-ack window where playing
					// lags; otherwise BeatSyncMax can miss a just-started follower.
					playing: deckStates[id].playing || _rt[id].desiredActive,
					// Effective, not raw: a gridless deck dragged into a
					// BeatSyncMax re-anchor would fail the plan and turn one
					// operator's seek into another deck's sync error.
					beatSyncEnabled: effectiveBeatSync(deckStates[id])
				}))
			});
			if (syncPlan.kind === 'follower') {
				// seekSyncMaster only returns this kind for a deck already
				// playing and already beat-synced - a re-anchor, not a join.
				await _synchronizeFollowers(syncPlan.master, [deck], {
					followerAnchorSec: { [deck]: targetMs / 1000 },
					reanchorDecks: new Set([deck])
				});
			} else if (syncPlan.kind === 'master-max') {
				// beatSyncMaxFollowers only returns already playing,
				// already beat-synced followers - same re-anchor contract.
				await _synchronizeFollowers(deck, syncPlan.followers, {
					masterSchedule: {
						tempoRatio: st.pitch,
						masterTempoEnabled: st.master_tempo_enabled,
						positionSec: targetMs / 1000
					},
					reanchorDecks: new Set(syncPlan.followers)
				});
			} else {
				if (_ctx === null) throw new Error('cueJump: audio graph not initialised');
				await _scheduleDeck(
					deck,
					_futureScheduleTime(deck),
					targetMs / 1000,
					rt.desiredActive,
					undefined,
					undefined,
					scheduleLoop
				);
			}
		} else {
			_setPausedPosition(deck, targetMs);
		}
	}

	/** The physical CUE button. Playing: return to the cue point and pause.
	 * Paused with a cue set: jump the playhead to it. Paused with no cue:
	 * set the cue at the current position.
	 *
	 * Q1: see `play` for the stamp. Only the playing branch schedules; the paused
	 * branches are pure state writes, and the seek branch is Q1's follow-up. */
	async pressCue(deck: DeckId, pressT0Ms?: number): Promise<void> {
		const { st } = _requireLoaded(deck, 'pressCue');
		if (st.playing) {
			const target = st.cue_ms ?? 0;
			if (_ctx === null) throw new Error('pressCue: audio graph not initialised');
			await _schedulePress(deck, _futureScheduleTime(deck), target / 1000, false, pressT0Ms);
			return;
		}
		if (st.cue_ms === null) {
			const cueBeats = _quantizeGrid(st);
			st.cue_ms =
				cueBeats !== null
					? quantizedPositionMs(cueBeats, st.position_ms, true)
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
		// Effective, not raw: refusing a pitch move because a lit-but-inert
		// BEAT SYNC "owns" the tempo would strand a gridless deck's fader.
		if (st.playing && effectiveBeatSync(st) && activeMaster !== null && activeMaster !== deck) {
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
								effectiveBeatSync(deckStates[candidate])
						)
					: [];
			if (followers.length > 0) {
				// followers above is already filtered to playing +
				// beat_sync_enabled decks - a re-anchor, not a fresh join.
				await _synchronizeFollowers(deck, followers, {
					masterSchedule: {
						tempoRatio: ratio,
						masterTempoEnabled: st.master_tempo_enabled
					},
					reanchorDecks: new Set(followers)
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
		// Same rule as the rest of transport: quantize with a grid, exact
		// endpoints without one. A manual in/out loop is not grid-dependent.
		const loopBeats = _quantizeGrid(st);
		const snapped =
			loopBeats !== null
				? quantizedLoopEndpointsMs(loopBeats, loop, true)
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

	/** Jump whole PQTZ beats, anchored on the playhead projected to the next
	 * safe schedule time so back-to-back jumps compound, including when a
	 * pending mutation (e.g. a not-yet-presented pause) has not landed yet;
	 * math + duration clamp live in beat-sync-math.ts. */
	async beatJump(deck: DeckId, beats: number): Promise<void> {
		const { st } = _requireLoaded(deck, 'beatJump');
		const grid = _requireBeatGrid(st, 'beatJump');
		const anchorMs = _projectPositionAt(deck, _futureScheduleTime(deck)) * 1000;
		const rawTargetMs = beatJumpTargetMs(grid, anchorMs, beats);
		const targetMs = beatJumpTargetWithinDurationMs(grid, rawTargetMs, _durationSec(deck) * 1000);
		await this.quantizedSeek(deck, targetMs);
	}

	/** Capture the current engaged loop as the one-slot safety loop (armed). */
	saveSafetyLoop(deck: DeckId): void {
		const { st } = _requireLoaded(deck, 'saveSafetyLoop');
		if (st.loop === null || !st.loop.engaged) {
			throw new Error(`saveSafetyLoop: deck ${deck} has no engaged loop to save`);
		}
		st.safety_loop = {
			in_ms: st.loop.in_ms,
			out_ms: st.loop.out_ms,
			beat_length: st.loop.beat_length,
			armed: true
		};
	}

	setSafetyLoopArmed(deck: DeckId, armed: boolean): void {
		if (typeof armed !== 'boolean') {
			throw new TypeError('setSafetyLoopArmed: armed must be boolean');
		}
		const { st } = _requireLoaded(deck, 'setSafetyLoopArmed');
		if (st.safety_loop === null) {
			throw new Error(`setSafetyLoopArmed: deck ${deck} has no saved safety loop`);
		}
		st.safety_loop = { ...st.safety_loop, armed };
	}

	clearSafetyLoop(deck: DeckId): void {
		const { st } = _requireLoaded(deck, 'clearSafetyLoop');
		st.safety_loop = null;
	}

	setQuantize(deck: DeckId, enabled: boolean): void {
		if (typeof enabled !== 'boolean') throw new TypeError('setQuantize: enabled must be boolean');
		const st = deckStates[deck];
		st.quantize_enabled = enabled;
		// Inert, never refused. The UI already renders Q disabled with this
		// exact sentence; the toast is for the IPC and CLI paths, where there
		// is no hovered button to read and a silent no-op would be a lie.
		if (enabled && gridFeaturesInert(st)) {
			pushToast(`Deck ${deck} QUANTIZE ${GRID_FEATURE_TIP}`, 'info');
		}
	}

	setBeatSync(deck: DeckId, enabled: boolean): Promise<void> {
		if (typeof enabled !== 'boolean') throw new TypeError('setBeatSync: enabled must be boolean');
		const st = deckStates[deck];
		st.beat_sync_enabled = enabled;
		if (!enabled) {
			st.sync_error = null;
			return Promise.resolve();
		}
		// Same contract as setQuantize: the flag keeps the value the DJ chose,
		// the deck has nothing to lock to, and that is said out loud rather
		// than thrown into command_error.
		if (gridFeaturesInert(st)) {
			pushToast(`Deck ${deck} BEAT SYNC ${GRID_FEATURE_TIP}`, 'info');
			return Promise.resolve();
		}
		if (!_rt[deck].desiredActive) return Promise.resolve();
		const master = _syncMaster();
		if (master === null) {
			_assignMaster(deck);
			return Promise.resolve();
		}
		if (!syncChangeRequiresReschedule(deck, true, enabled, master)) {
			return Promise.resolve();
		}
		// Hard-reject impossible phase lock; never leave BEAT SYNC lit without a plan.
		// This is the FIRST lock for this deck (beat_sync_enabled just flipped
		// true above) - nothing audible was synced before, so no
		// reanchorDecks; engaging Beat Sync applies immediately.
		return _synchronizeFollowers(master, [deck]).catch((error: unknown) => {
			st.beat_sync_enabled = false;
			const bounds = _tempoBounds(deck);
			const detail = error instanceof Error ? error.message : String(error);
			throw new Error(
				`cannot phase-lock within pitch [${bounds.min}, ${bounds.max}] (BAR): ${detail}`,
				{ cause: error instanceof Error ? error : undefined }
			);
		});
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
			if (st.playing && !st.slip_active) {
				const { rt } = _requireLoaded(deck, 'setSlip');
				const presentedAt = rt.presentation.last_presentation_context_time_s;
				if (presentedAt === null) {
					throw new Error('SLIP requires output presentation truth before live activation');
				}
				const schedule = _effectivePresentedScheduleAt(rt.presentation, presentedAt);
				if (schedule?.loop?.engaged === true) {
					const anchor = presentedSlipAnchor(rt.presentation, rt.durationSec);
					st.slip_enabled = true;
					_activateSlip(deck, anchor, slipTempoBoundariesAfterAnchor(rt.presentation, anchor));
					return;
				}
			}
			// Arming SLIP outside a presented engaged loop is state-only.
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
		const deckManualShiftSemitones = _keySyncManualShiftBaseline(deck);
		const targetManualShiftSemitones = deriveKeySyncTargetManualShift(
			st.key,
			master.key,
			_effectiveAudibleSemitones(deck),
			_effectiveAudibleSemitones(masterDeck),
			deckManualShiftSemitones
		);
		await _setDeckKeyShift(deck, targetManualShiftSemitones);
	}

	async setKeySync(deck: DeckId, enabled: boolean): Promise<void> {
		if (typeof enabled !== 'boolean') throw new TypeError('setKeySync: enabled must be boolean');
		const st = deckStates[deck];
		const rt = _rt[deck];
		if (!enabled) {
			st.key_sync_enabled = false;
			const baseline = rt.keySyncBaselineSemitones;
			rt.keySyncBaselineSemitones = null;
			if (baseline !== null && st.stable_id !== null) {
				await _setDeckKeyShift(deck, baseline);
			}
			return;
		}
		_requireLoaded(deck, 'setKeySync');
		rt.keySyncBaselineSemitones = _keySyncManualShiftBaseline(deck);
		st.key_sync_enabled = true;
		await this.syncKey(deck);
	}

	async unload(deck: DeckId): Promise<void> {
		const st = deckStates[deck];
		const rt = _rt[deck];
		if (st.stable_id === null && rt.processor === null) return;
		if (rt.desiredActive) await this.pause(deck);
		const deadline = performance.now() + 2000;
		while (performance.now() < deadline) {
			try {
				assertDeckReplacementAllowed(deck, {
					playing: st.playing,
					audible: st.audible,
					transportPending: st.transport_pending,
					controlActive: rt.controlActive,
					pendingScheduleCount: rt.pending.length,
					scheduleIntentCount: rt.scheduleIntentCount
				});
				break;
			} catch {
				await new Promise<void>((resolve) => {
					requestAnimationFrame(() => resolve());
				});
			}
		}
		rt.loadToken += 1;
		_releasePendingStemUpgrade(rt);
		const processor = detachProcessorForDisposal(rt);
		if (processor !== null) {
			processor.disconnect();
			if (_ctx !== null) {
				try {
					await processor.stop(_ctx.currentTime);
				} catch (error: unknown) {
					const message = error instanceof Error ? error.message : String(error);
					pushToast(`Deck ${deck} unload cleanup failed - ${message}`, 'error');
				}
			}
			try {
				await processor.dispose();
			} catch (error: unknown) {
				const message = error instanceof Error ? error.message : String(error);
				pushToast(`Deck ${deck} processor disposal failed - ${message}`, 'error');
			}
		}
		const wasMaster = _masterDeck === deck;
		_rt[deck] = {
			..._emptyRuntime(),
			loadToken: rt.loadToken,
			nodes: rt.nodes,
			scheduleTail: Promise.resolve(),
			swapTail: Promise.resolve()
		};
		deckStates[deck] = _emptyDeckState(deck);
		deckLoadErrors[deck] = null;
		if (wasMaster) _electPlayingMaster();
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
				// Effective: a lit-but-inert BEAT SYNC has no schedule to
				// re-anchor, and asking for one would throw on the missing grid.
				effectiveBeatSync(st),
				master
			)
		) {
			return Promise.resolve();
		}
		if (master === null) {
			throw new Error('setSyncMode: reschedule invariant requires a selected master deck');
		}
		// syncChangeRequiresReschedule already required desiredActive and
		// beat_sync_enabled - this deck was already locked, now re-anchoring.
		return _synchronizeFollowers(master, [deck], { reanchorDecks: new Set([deck]) });
	}

	async setDeckMaster(deck: DeckId): Promise<void> {
		const { st } = _requireLoaded(deck, 'setDeckMaster');
		if (!st.audible) {
			const blockers = pausedMasterSelectionBlockers(deck, deckStates);
			assertPausedMasterSelectionAllowed(deck, st.audible, blockers);
			_assignMaster(deck);
			return;
		}
		// masterSwitchFollowers only returns already playing, already
		// beat-synced decks - re-anchoring them to the new master. A deck whose
		// BEAT SYNC is lit but inert (no real grid) is not one of them: it never
		// locked, so there is nothing to re-anchor and _requireBeatGrid would
		// turn one operator's MASTER press into that deck's sync error.
		const followers = masterSwitchFollowers(deck, deckStates).filter((candidate) =>
			effectiveBeatSync(deckStates[candidate])
		);
		await _synchronizeFollowers(deck, followers, { reanchorDecks: new Set(followers) });
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
		if (
			st.stems.status === 'ready' &&
			!st.stems.available_controls.includes(stem)
		) {
			// e.g. DRUMS on a roformer2 bundle: the signal is inside
			// `instrumental`, so there is nothing to gain to zero. Refuse loudly
			// rather than accept a control change that can never be heard.
			throw new Error(
				`deck ${deck} stem layout ${String(st.stems.layout)} has no ${stem} control; ` +
					`available: ${st.stems.available_controls.join(', ')}`
			);
		}
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

	setFilter(deck: DeckId, value: number): void {
		_assertUnit('setFilter value', value);
		mixerState.channels[deck].filter = value;
		const nodes = _rt[deck].nodes;
		if (nodes !== null) {
			const { lpHz, hpHz } = _filterFreqsFromKnob(value);
			_setParam(nodes.filterLp.frequency, lpHz);
			_setParam(nodes.filterHp.frequency, hpHz);
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
		applyHeadphoneMix();
	}

	setHeadphoneLevel(value: number): void {
		_assertUnit('setHeadphoneLevel value', value);
		mixerState.headphones.level = value;
		applyHeadphoneMix();
	}

	async refreshHeadphoneOutputs(): Promise<void> {
		return refreshMonitorOutputs();
	}

	/** Must be called from a visible user gesture so the browser can open its
	 * output chooser. This never requests microphone capture. */
	async acquireHeadphoneOutput(): Promise<void> {
		return acquireMonitorOutput(_monitorSource);
	}

	async selectHeadphoneOutput(deviceId: string): Promise<void> {
		return selectMonitorOutput(deviceId, _monitorSource);
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
