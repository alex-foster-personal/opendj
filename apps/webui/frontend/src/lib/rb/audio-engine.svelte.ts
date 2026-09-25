import {
	SILENT_METER_READING,
	createMasterMeterSource,
	masterMeterReading,
	metersUnavailable,
	onMetersUnavailableChange,
	meterClockMs,
	releaseMasterMeterTap,
	readMeterTap,
	type MeterReading,
	type MeterTap,
	type MeterTapSource
} from '$lib/rb/meter-tap';
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
import { cueOnlyMonitoringActive, parseDjOutputProfile, wireAudioOutputTopology, type DjOutputProfile } from '$lib/rb/audio-output-topology';
import { decodeDeckLoadAudio, deckLoadAudio } from '$lib/rb/audio-prefetch-cache.svelte';
import {
	registerAudioContext,
	unregisterAudioContext
} from '$lib/rb/audio-context-registry';
import { detachProcessorForDisposal, disposeAudioResources } from '$lib/rb/audio-resource-disposal';
import {
	beginDeckLoad,
	formatDeckLoadFailureMessage,
	recordDeckLoad,
	reportDeckLoadFailure
} from '$lib/rb/deck-load-context';
import { recordPerfEvent, recordPerfTiming } from '$lib/rb/perf-event-log';
import {
	noteMasterSilence,
	notePresentationClock,
	notePresentationTickFailure,
	readOutputTimestamp as _readOutputTimestamp,
	resetMasterSilenceWatch,
	resetPresentationClockStall
} from '$lib/rb/engine-clock-reports';
import {
	armAudioContextWatchdog,
	armDeckMeters,
	armXrunSentinel,
	disarmContextInstrumentation,
	resumeAudioContextOrReportDead,
	stampContextDeviceFloors
} from '$lib/rb/audio-context-instrumentation';
import {
	notePlayingFallingEdge,
	readPauseOrigin,
	recordUnexpectedPause,
	setPlayingPositionReader,
	withPauseOrigin
} from '$lib/rb/unexpected-pause-report';
import { buildDeckChannelGraph, recreateFromEngineAccess, type DeckChannelNodes as _ChannelNodes } from '$lib/rb/deck-channel-graph';
import { applyEqRamp, logEqApply, logMixerApply, measurePressToScheduleMs, scheduleRowFacts } from '$lib/rb/press-stamp';
import {
	ConflictError,
	fetchAnlz,
	fetchAnlzBypassingHttpCache,
	fetchAudioArrayBuffer,
	fetchHotCueSlots,
	fetchStemAudioArrayBuffers,
	STEM_LAYOUT_PART_NAMES,
	getTrack,
	patchTrack,
	RbApiError
} from '$lib/rb/api-rb';
import { awaitStemArtifact } from '$lib/rb/stem-hydrate-wait';
import type { AnlzWithVocals, DemucsStemPart, HotCueSlotState, Track } from '$lib/rb/api-rb';
import {
	anlzMatchesConfirmedSource,
	currentAnlzFetchGeneration,
	fetchAnlzForDeckLoad,
	fetchAnlzUntilSourceConfirmed,
	getAnlzEntry,
	installAuthoritativeAnlzGridSink,
	installAuthoritativeAnlzErrorSink,
	invalidateAnlzCacheEntry,
	isAnlzEntryUsable,
	revalidateAnlz,
	refreshAnlzCacheEntry,
	upgradeDeckBeatgrid,
	createBeatgridResyncGuards,
	createBeatgridResyncTracking,
	analysisSourceState,
	reconcileLoopForAuthoritativeGrid,
	requireBeatGrid,
	resolvePublishedAnlz,
	type BeatgridResyncPorts
} from '$lib/components/rb/wave/anlz-cache.svelte';
import {
	beatJumpTargetMs,
	beatJumpTargetWithinDurationMs,
	computeFollowerSyncPlan,
	displayLoopFrom,
	computeQuantizedLaunchArm,
	planPhaseCompensatedReanchor,
	playbackBpm,
	quantizeToNearestBeat,
	quantizeToNearestGridBeat,
	QUANTIZED_LAUNCH
} from '$lib/rb/beat-sync-math';
import type { TempoRampStep } from '$lib/rb/beat-sync-math';
import { beatSyncOutcomeNotices } from '$lib/rb/beat-sync-math';
import {
	deckHasRealBeatGrid,
	effectiveBeatSync,
	effectiveQuantize,
	gridFeatureInertTip,
	gridFeaturesInert,
	hasRealBeatGrid,
	hasTrustedBeatGrid
} from '$lib/player/grid-features';
import {
	beatFourLeadInSec,
	beatSyncMaxFollowers,
	planSeekSync,
	seekSyncMaster,
	syncChangeRequiresReschedule,
	syncMayWriteTempo,
	syncModeForBeatSyncMax,
	syncSeekBlendDurationSec,
	type SeekSyncPlan
} from '$lib/rb/sync-seek-blend';
import { uiPrefs } from '$lib/rb/prefs.svelte';
import {
	StretchDeckProcessor,
	ensureStretchWorkletReady,
	type StretchScheduleChange
} from '$lib/rb/stretch-adapter';
import {
	AlignedStemDeckProcessor,
	decodeStemBuffers,
	DEMUCS_PARTS,
	loadingStemDeckState,
	readyStemDeckState, playingStemsIntentionallySilent,
	STEM_CONTROLS,
	unavailableStemDeckState,
	type StemBuffers
} from '$lib/rb/stem-graph';
import { applyStemControl, applyStemEqMode } from '$lib/rb/stem-engine-controls';
import type { AnlzBeat, AnlzData } from '$lib/rb/anlz-types';
import type { AudioEngine, MasterMode, MasterReason } from '$lib/rb/audio-engine-types';
import { parseExternalRouting, type DeckId } from '$lib/rb/deck-slots';
import { buildDeckAudioSnapshot } from '$lib/rb/deck-audio-snapshot';
import type { DeckAudioSnapshot, DeckState, LoopState, QuantizeGrid, SyncMode } from '$lib/rb/deck-state-types';
import type { HotCue, HotCueSlot } from '$lib/rb/hot-cue-types';
import { hotCuesFromAnlz } from '$lib/rb/hot-cue-from-anlz';
import type { CrossfaderAssign, EqBand, MixerChannelState, MixerState } from '$lib/rb/mixer-types';
import type { StemControl, StemDeckState } from '$lib/rb/stem-types';
import {
	assertUnitRange,
	AUDIO_CONTEXT_OPTIONS,
	CONTEXT_WAIT_POLL_MS,
	CONTEXT_WAIT_STALL_TIMEOUT_MS,
	DECK_IDS,
	eqDbFromKnob,
	masterDelaySeconds,
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
import { attachMasterMuteNode, isMasterMuted, setMasterMuted } from '$lib/player/master-mute.svelte';
import {
	acquireHeadphoneOutput as acquireMonitorOutput,
	applyHeadphoneMix,
	createMasterDelayNode,
	disposeHeadphoneMonitor,
	ensureHeadphoneGraph,
	refreshHeadphoneOutputs as refreshMonitorOutputs,
	selectAudioInput as selectMonitorAudioInput,
	selectHeadphoneOutput as selectMonitorOutput,
	selectMasterOutput as selectMonitorMasterOutput,
	setAlignmentMode as setMonitorAlignmentMode,
	setHeadDelayMs as setMonitorHeadDelay,
	setHeadphoneOutputMode as setMonitorOutputMode,
	setMasterDelayMs as setMonitorMasterDelay,
	setMultichannelMonitorActive,
	wirePracticeBlendIntoMasterPath,
	wireSplitCableIntoMasterPath
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
	disarmSafetyLoopOnExplicitExit,
	exactBeatLoopRangeMs,
	loopEndpointsWithinDurationMs,
	phaseLockedSafetyLoop,
	playbackReachedSafetyLoopOut,
	precedingDownbeatMs,
	quantizedLoopEndpointsMs,
	quantizedPositionMs,
	quantizedSeekDecisionMs,
	replaceMatchingSafetyLoopSnapshot,
	resolvedLoopState,
	shiftLiveBeatLoopRangeMs,
	targetWithinShiftedLiveLoopMs
} from '$lib/player/transport/loops';
import {
	_positionForSegment,
	pausedSeekClock,
	commonSyncScheduleTimes,
	deckReachedEnd,
	decodedTransportDurationMs,
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
	createSlipAnchor,
	keySyncEffectiveAudibleSemitones,
	keySyncManualShiftBaseline,
	keySyncPreviewAvailable,
	observePresentedTransportTimeline,
	presentedKeyShiftSemitonesAt,
	presentedSlipAnchor,
	rebaseSlipAnchor,
	setPausedTransportTimelineCursor,
	shouldActivateSlip,
	slipHiddenPositionSec,
	slipHiddenPositionWithTempoBoundaries,
	slipTempoBoundariesAfterAnchor
} from '$lib/player/transport/presentation';
import type {
	PresentedTransportObservation,
	PresentedTransportSchedule,
	PresentedTransportTimeline,
	SlipAnchor,
	SlipTempoBoundary
} from '$lib/player/transport/presentation';

// ---------------------------------------------------- extracted re-exports
//
// T4 S1. The DSP constants moved to player/constants.ts and the Camelot
// algebra to player/key/camelot.ts. Everything they used to export from here
// is re-exported below, so every existing importer of
// $lib/rb/audio-engine.svelte keeps working unchanged.

export { DECK_IDS, PITCH_RANGES };
export { detachProcessorForDisposal };
export {
	beatSyncMaxFollowers,
	planSeekSync,
	seekSyncMaster,
	syncChangeRequiresReschedule,
	syncMayWriteTempo,
	type SeekSyncPlan
};
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
export {
	keySyncEffectiveAudibleSemitones,
	keySyncManualShiftBaseline,
	presentedKeyShiftSemitonesAt
};
export {
	exactBeatLoopRangeMs,
	loopEndpointsWithinDurationMs,
	quantizedLoopEndpointsMs,
	quantizedPositionMs
};
export {
	commonSyncScheduleTimes,
	deckReachedEnd,
	decodedTransportDurationMs,
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
export {
	createSlipAnchor,
	presentedSlipAnchor,
	rebaseSlipAnchor,
	shouldActivateSlip,
	slipHiddenPositionSec,
	slipHiddenPositionWithTempoBoundaries,
	slipTempoBoundariesAfterAnchor
};
export type { SlipAnchor, SlipTempoBoundary };
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
	/** Library-listed track duration; decoded buffer duration lives in deck state. */
	metadataDurationMs: number | null;
	/** Monotonic token; superseding transport/sync commands bump this deck's generation. */
	reanchorOperationGeneration: number;
	/** When set, equals the generation of the ramp that owns `transport_pending`. */
	reanchorRampOwnerGeneration: number | null;
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
		metadataDurationMs: null,
		reanchorOperationGeneration: 0,
		reanchorRampOwnerGeneration: null,
		pendingStemUpgrade: null
	};
}

let _ctx: AudioContext | null = null;
let _masterGain: GainNode | null = null;
let _masterAnalyser: AnalyserNode | null = null;
/** Opt-in startup mute, after every tap and before the room delay. Never bypassed. */
let _masterMuteGain: GainNode | null = null;
/** CUEOUT-14 room delay line: THE last node before the destination, after the
 * mute, so the monitor tap, the meters and the silence belt all sit upstream
 * of it and the phones never pay it. Created by player/headphones.ts, which
 * owns its delayTime; the engine only wires it. */
let _masterDelay: DelayNode | null = null;
let _externalMerger: ChannelMergerNode | null = null, _externalRouteAnalyser: AnalyserNode | null = null; // #1642: taps _externalMerger, which bypasses _masterGain
let _djOutputNodes: AudioNode[] = [];
let _djOutputProfileActive: DjOutputProfile | null = null;
let _rafId: number | null = null;
let _masterDeck: DeckId | null = null;
const _quantizedLaunchAt: Record<DeckId, number | null> = { 1: null, 2: null, 3: null, 4: null };
let _masterMode: MasterMode = 'auto';
let _masterReason: MasterReason = null;
/**
 * #1475 M enforcement: a static gain ceiling, not a limiter. `_ceilingDbfs` is
 * the level captured by the M control at some tap; enabling it attenuates
 * `_masterGain` by a fixed amount so 0 dBFS (full scale) lands at that
 * captured level instead. It never boosts, and it never reads the current
 * signal, so there is no attack/release and nothing here colors the sound -
 * disabling removes the attenuation exactly. See audio-engine-types.ts for
 * the rationale against a look-ahead limiter.
 */
let _ceilingDbfs: number | null = null;
let _ceilingEnabled = false;

function _ceilingGainMultiplier(): number {
	if (!_ceilingEnabled || _ceilingDbfs === null) return 1;
	return Math.min(1, 10 ** (_ceilingDbfs / 20));
}
/** Monotonic engine-session id, bumped by dispose(). Deck loadTokens cannot
 * carry a guard across a route remount: dispose() installs a fresh
 * `_emptyRuntime()` per deck whose loadToken restarts at 0, so a token
 * captured before disposal compares equal again after the same number of
 * loads in the NEW session (discussion_r3919692507 P1 BLOCKING). This counter
 * never repeats a value, so anything that captures it at entry can tell
 * "still my session" from "a different session that happens to look like
 * mine" after any await. */
let _engineSession = 0;

/** The actual AudioContext state, not a transport-state inference. */
export function audioContextState(): AudioContextState | 'uninitialized' {
	return _ctx?.state ?? 'uninitialized';
}
const _rt: Record<DeckId, _DeckRuntime> = {
	1: _emptyRuntime(),
	2: _emptyRuntime(),
	3: _emptyRuntime(),
	4: _emptyRuntime()
};

/**
 * Channel level meter reading, taken POST-TRIM, POST-EQ, POST-FADER (issue
 * #3529) through an AudioWorklet tap: level, peak, segment count, clip latch.
 *
 * REPLACED `peekDeckMeter`, which returned `Math.min(1, rms * 5.5)`: linear
 * amplitude against a magic constant, no dB scale, no ballistics, and tapped
 * BEFORE `trim`, so it responded to neither the trim knob nor the EQ and
 * could never show clipping. Every number now comes from `meter-math`.
 *
 * The AnalyserNode itself stays where it is: `captureDeckAudio` wants raw
 * deck output for its diagnostic FFT snapshot, a legitimate use of it.
 */
/** Deck id to its channel meter tap. This module owns the deck mapping so
 * meter-tap stays deck-agnostic and reusable for a master meter. */
const _meterTaps: Record<DeckId, MeterTap | null> = { 1: null, 2: null, 3: null, 4: null };

export function peekDeckMeterReading(deck: DeckId): MeterReading {
	const tap = _meterTaps[deck];
	if (_rt[deck].nodes === null || tap === null) return SILENT_METER_READING;
	return readMeterTap(tap, meterClockMs());
}

/** Test seam: live channel-fader gain after setFader, null when the graph is absent. */
export function peekDeckFaderGain(deck: DeckId): number | null {
	const nodes = _rt[deck].nodes;
	if (nodes === null) return null;
	return nodes.fader.gain.value;
}

/** Master output level. The tap, the silent fallback and the "what does red
 * mean here" contract all live in meter-tap.ts; the engine supplies only the
 * clock, so the barrel stays the one import edge a meter component needs. */
export function peekMasterMeterReading(): MeterReading {
	return masterMeterReading(meterClockMs());
}

/** CUEOUT-14: the live room delay line, null until the graph exists. Read by
 * loopback checks that want to prove the LAST node before the destination is
 * the delay; nothing else may write its delayTime (player/headphones.ts owns it). */
export function masterDelayNode(): DelayNode | null {
	return _masterDelay;
}

/** Re-exported so a meter component can tell a genuinely broken meter
 * (worklet failed to arm) apart from a genuinely silent bus. See
 * meter-tap.ts's metersUnavailable/UNAVAILABLE_METER_READING. */
export { metersUnavailable, onMetersUnavailableChange };

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

// ---------------------------------------------------------------- _helpers

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

/** Issue #2155: new AudioContext plus re-attached decks after output position stall. */
async function rebuildAudioGraphKeepingDecks(): Promise<void> {
	await recreateFromEngineAccess({
		decks: DECK_IDS,
		runtime: (deck) => _rt[deck],
		state: (deck) => deckStates[deck],
		releasePendingStemUpgrade: (deck) => _releasePendingStemUpgrade(_rt[deck]),
		detachProcessor: (deck) => detachProcessorForDisposal(_rt[deck]),
		disarmInstrumentation: () => disarmContextInstrumentation(),
		muteNode: () => _masterMuteGain,
		disposeResources: ({ processors, nodes }) =>
			disposeAudioResources({
				rafId: _rafId,
				processors,
				nodes: [...nodes, ..._djOutputNodes],
				masterGain: _masterGain,
				context: _ctx
			}),
		resetGraphState: () => {
			disposeHeadphoneMonitor();
			_rafId = null;
			_masterGain = null;
			releaseMasterMeterTap();
			attachMasterMuteNode(null);
			_masterMuteGain = null;
			_masterDelay = null;
			_externalMerger = _externalRouteAnalyser = null;
			_djOutputNodes = [];
			_djOutputProfileActive = null;
			_ctx = null;
			resetMasterSilenceWatch();
			resetPresentationClockStall();
		},
		ensureGraph: () => _ensureGraph(),
		resetPresentation: (deck, positionSec) => {
			_rt[deck].presentation = createPresentedTransportTimeline(positionSec);
			_rt[deck].nextScheduleRevision = 0;
		},
		attachProcessor: (deck, processor, buffer, latencySec) => {
			const rt = _rt[deck];
			rt.processor = processor;
			rt.audioBuffer = buffer;
			rt.latencySec = latencySec;
		},
		processorFailed: (deck, processor, error) => {
			if (_rt[deck].processor === processor) _recordProcessorFailure(deck, error);
		},
		schedulePlayingDeck: async (snap, ctx) => {
			await _scheduleDeck(
				snap.deck,
				ctx.currentTime,
				snap.positionSec,
				true,
				snap.tempoRatio,
				snap.masterTempoEnabled,
				snap.loop,
				snap.keyShiftSemitones,
				undefined
			);
		},
		maybeUpgradeStems: (snap, buffer, ctx) => {
			if ((snap.stemsReady || snap.stemsLoading) && snap.stableId.length > 0) {
				void _upgradeDeckStems(snap.deck, snap.stableId, _rt[snap.deck].loadToken, ctx, buffer);
			}
		}
	});
	await _resumeContext();
}

function _ensureGraph(): AudioContext {
	if (typeof window === 'undefined') {
		throw new Error('AudioEngine requires a browser AudioContext (no SSR usage)');
	}
	if (_ctx !== null) return _ctx;
	// Construction options travel through ONE named constant so a future
	// user-facing buffer/latency setting has a single place to write to.
	_ctx = new AudioContext(AUDIO_CONTEXT_OPTIONS);
	registerAudioContext(_ctx);
	stampContextDeviceFloors(_ctx);
	// A context that is allowed to start running immediately never fires
	// statechange, so the build stamp above already caught it; one that starts
	// suspended is re-stamped the moment it runs, whichever path resumed it.
	// The watchdog owns that re-stamp AND every non-running state: suspended,
	// interrupted and closed used to fall through in silence, which is how
	// Wed 2 Sep 2026 cost ~24 minutes of audio with nothing on screen.
	armAudioContextWatchdog(
		_ctx,
		() => DECK_IDS.some((deck) => deckStates[deck].playing),
		rebuildAudioGraphKeepingDecks
	);
	setPlayingPositionReader(() =>
		DECK_IDS.filter((d) => deckStates[d].playing).map((d) => ({
			deck: d,
			position_ms: deckStates[d].position_ms,
			decoded_duration_ms: deckStates[d].duration_ms,
			metadata_duration_ms: _rt[d].metadataDurationMs
		}))
	);
	_masterGain = _ctx.createGain();
	_masterGain.gain.value = mixerState.master * _ceilingGainMultiplier();
	// Silence watchdog tap: an AnalyserNode with nothing downstream is a pure
	// observer, and it sits BEFORE _masterMuteGain so `?muted=1` is not a dropout.
	_masterGain.connect((_masterAnalyser = _ctx.createAnalyser()));
	resetMasterSilenceWatch(); resetPresentationClockStall();
	// Silence belt for headless test agents (`?muted=1`): the LAST node before
	// the destination, so a mute is one gain value and every node upstream --
	// decks, EQ, crossfader, analysers, headphone monitor -- keeps running
	// identically. See player/master-mute.svelte.ts.
	_masterMuteGain = _ctx.createGain();
	attachMasterMuteNode(_masterMuteGain);
	_masterDelay = createMasterDelayNode(_ctx);
	const routing = parseExternalRouting();
	_djOutputProfileActive = parseDjOutputProfile(window.location.search);
	const headphones = ensureHeadphoneGraph(_ctx, _masterGain);
	const output = wireAudioOutputTopology({
		context: _ctx,
		routing,
		profile: _djOutputProfileActive,
		masterGain: _masterGain,
		masterMuteGain: _masterMuteGain,
		masterDelay: _masterDelay,
		headphoneDelay: headphones.delay
	});
	_externalMerger = output.externalMerger; _externalRouteAnalyser = output.externalRouteAnalyser;
	_djOutputNodes = output.ownedNodes;
	setMultichannelMonitorActive(output.multichannelMonitorActive);
	if (routing === null && _djOutputProfileActive === null) {
		wirePracticeBlendIntoMasterPath(_masterGain, _masterMuteGain, headphones);
		wireSplitCableIntoMasterPath(_masterGain, _masterMuteGain, headphones);
	}
	// Post-fader channel tap points, one per deck, PLUS one master tap sourced
	// from `_masterGain` itself (post master gain, so the master volume
	// control genuinely moves it - pin 5a5c3b8033d8's still-open half).
	// Collected here and armed after the loop because addModule is async and
	// the graph build is not.
	const meterSources: MeterTapSource[] = [];
	meterSources.push(createMasterMeterSource(_masterGain));
	meterSources.push(
		...buildDeckChannelGraph({
			ctx: _ctx,
			mixerState,
			masterGain: _masterGain,
			externalMerger: _externalMerger,
			routing,
			cueSum: headphones.cueSum,
			xfGainFor: _xfGainFor,
			onDeck: (deck, nodes, tap) => {
				_rt[deck].nodes = nodes;
				_meterTaps[deck] = tap;
			}
		})
	);
	void ensureStretchWorkletReady(_ctx).catch((error: unknown) => {
		recordPerfEvent(
			'stretch-worklet-preload-failed',
			`Signalsmith worklet did not become ready during graph build: ${String(error)}`
		);
	});
	armXrunSentinel(_ctx);
	armDeckMeters(_ctx, meterSources);
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

/** Listener-facing KEY SYNC plan for UI and browser agents. The preview is
 * deliberately calculated from the exact same control and presented-audio
 * sources as `syncKey`, never from a visible but potentially stale deck field. */
export interface KeySyncPreview {
	masterDeck: DeckId;
	targetManualShiftSemitones: number;
	deltaSemitones: number;
}

export interface KeySyncEffectiveOffsetSource {
	audible: boolean;
	transportPending: boolean;
	pendingMutation: boolean;
	control: DeckControlSettings;
	presentation: PresentedTransportTimeline;
}

function _keySyncPlan(deck: DeckId, masterDeck: DeckId, sourceBaseline: number): KeySyncPreview {
	const source = deckStates[deck];
	const master = deckStates[masterDeck];
	const targetManualShiftSemitones = deriveKeySyncTargetManualShift(
		source.key,
		master.key,
		_effectiveAudibleSemitones(deck),
		_effectiveAudibleSemitones(masterDeck),
		sourceBaseline
	);
	return {
		masterDeck,
		targetManualShiftSemitones,
		deltaSemitones: targetManualShiftSemitones - sourceBaseline
	};
}

/** Return null only when an input or output-presented plan is unavailable. */
export function keySyncPreview(deck: DeckId): KeySyncPreview | null {
	if (!DECK_IDS.includes(deck)) throw new RangeError(`KEY SYNC deck must be within 1..4, got ${deck}`);
	const masterDeck = _masterDeck;
	if (masterDeck === null || masterDeck === deck) return null;
	const source = deckStates[deck];
	const master = deckStates[masterDeck];
	if (
		source.stable_id === null ||
		master.stable_id === null ||
		parseCamelotKey(source.key) === null ||
		parseCamelotKey(master.key) === null
	) {
		return null;
	}
	const sourceInput = _keySyncSource(deck);
	const masterInput = _keySyncSource(masterDeck);
	if (!keySyncPreviewAvailable(sourceInput) || !keySyncPreviewAvailable(masterInput)) return null;
	return _keySyncPlan(deck, masterDeck, keySyncManualShiftBaseline(sourceInput));
}

/** Set the loaded deck track's rating (deck-header pin 4de63478782c). Same
 * PATCH /tracks/{sid} + If-Match path as the library rating cell
 * (BrowserPanel._patchRating) - deck-header just has no ETag of its own to
 * carry, so it always fetches one fresh first. A race where a different
 * track loads onto this deck while the request is in flight is guarded by
 * re-checking stable_id before writing the result back. */
export async function rateDeckTrack(deck: DeckId, next: number): Promise<void> {
	const stable_id = deckStates[deck].stable_id;
	if (stable_id === null) return;
	try {
		const etag = (await getTrack(stable_id)).etag;
		const { track, etag: fresh } = await patchTrack(stable_id, etag, { rating: next });
		void fresh;
		if (deckStates[deck].stable_id === stable_id) deckStates[deck].rating = track.rating ?? null;
	} catch (exc) {
		if (exc instanceof ConflictError) {
			if (deckStates[deck].stable_id === stable_id) {
				deckStates[deck].rating = exc.current.rating ?? null;
			}
			pushToast('rating conflict: track changed elsewhere - showing current value', 'error');
			return;
		}
		pushToast(`rating update failed: ${String(exc)}`, 'error');
	}
}

/** True while the rAF-driven presentation clock has not caught up to the last
 * acknowledged schedule. Read this, never `st.transport_pending`, when gating a
 * transport mutation: the state flag also folds in reanchor ramps. */
function _presentationPending(rt: _DeckRuntime): boolean {
	return rt.presentation.desired_revision !== rt.presentation.presented_revision;
}

// ./audio-engine-guards (pure functions, no engine state) - re-exported here
// so external importers and the bundled unit tests keep one import site.
import {
	nextPlayingMaster,
	assertDeckLoadConsistency,
	type DeckReplacementActivity,
	assertDeckReplacementAllowed,
	loadCandidateCanPublish,
	assertPausedMasterSelectionAllowed,
	pausedMasterSelectionBlockers,
	masterSwitchFollowers,
	naturalEndNeedsRevisionedStop,
	transportNeedsScheduledMutation,
	type TransportMutationActivity,
	filterParamsFromKnob
} from './audio-engine-guards';
import {
	electMaster,
	onAirGain,
	SILENCE_GAIN_EPSILON,
	type MasterElectionInput
} from './master-election';
export {
	nextPlayingMaster,
	assertDeckLoadConsistency,
	type DeckReplacementActivity,
	assertDeckReplacementAllowed,
	loadCandidateCanPublish,
	assertPausedMasterSelectionAllowed,
	pausedMasterSelectionBlockers,
	masterSwitchFollowers,
	naturalEndNeedsRevisionedStop,
	transportNeedsScheduledMutation,
	type TransportMutationActivity
};

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

function _quantizeGrid(st: DeckState): readonly AnlzBeat[] | null {
	const beats = st.anlz?.beatgrid.beats;
	return effectiveQuantize(st) && beats !== undefined ? beats : null;
}

function _quantizeGridBeats(st: DeckState): 1 | 4 | 8 {
	const beats = st.quantize_grid_beats;
	if (beats === 'phase') {
		throw new Error(`quantize_grid_beats: deck ${st.deck_id} is 'phase', which is not implemented and must never reach a snap calculation`);
	}
	return beats;
}

function _assignMaster(deck: DeckId | null, reason?: MasterReason): void {
	_masterDeck = deck;
	if (reason !== undefined) _masterReason = reason;
	for (const candidate of DECK_IDS) deckStates[candidate].is_master = candidate === deck;
}

function _ownedMaster(): DeckId | null {
	return _masterDeck;
}

/** Master that can actually drive Beat Sync phase. */
function _syncClockMaster(): DeckId | null {
	return _masterDeck !== null && deckStates[_masterDeck].playing ? _masterDeck : null;
}

function _syncMaster(): DeckId | null {
	return _syncClockMaster();
}

function _electionInput(): MasterElectionInput {
	return {
		crossfader: mixerState.crossfader,
		master: mixerState.master,
		decks: DECK_IDS.map((id) => {
			const st = deckStates[id];
			const ch = mixerState.channels[id];
			return {
				id,
				loaded: st.stable_id !== null,
				playing: st.playing,
				beat_sync_enabled: effectiveBeatSync(st),
				fader: ch.fader,
				trim: ch.trim,
				assign: ch.assign
			};
		})
	};
}

function _electPlayingMaster(options?: { force?: boolean; reason?: MasterReason }): DeckId | null {
	if (_masterMode === 'locked' && !options?.force) return _masterDeck;
	const next = electMaster(_electionInput());
	_assignMaster(next, options?.reason ?? 'master-left');
	return next;
}

function _maybeHandoffOnAir(): void {
	if (_masterMode !== 'auto' || _masterDeck === null) return;
	const masterDeck = _masterDeck;
	const st = deckStates[masterDeck];
	if (!st.playing) return;
	const ch = mixerState.channels[masterDeck];
	const masterGain = onAirGain(
		{
			id: masterDeck,
			loaded: st.stable_id !== null,
			playing: st.playing,
			beat_sync_enabled: effectiveBeatSync(st),
			fader: ch.fader,
			trim: ch.trim,
			assign: ch.assign
		},
		mixerState.crossfader,
		mixerState.master
	);
	if (masterGain >= SILENCE_GAIN_EPSILON) return;
	const hasOtherOnAir = DECK_IDS.some((candidate) => {
		if (candidate === masterDeck) return false;
		const other = deckStates[candidate];
		if (other.stable_id === null || !other.playing) return false;
		const otherCh = mixerState.channels[candidate];
		return (
			onAirGain(
				{
					id: candidate,
					loaded: true,
					playing: true,
					beat_sync_enabled: effectiveBeatSync(other),
					fader: otherCh.fader,
					trim: otherCh.trim,
					assign: otherCh.assign
				},
				mixerState.crossfader,
				mixerState.master
			) >= SILENCE_GAIN_EPSILON
		);
	});
	if (!hasOtherOnAir) return;
	_electPlayingMaster({ reason: 'master-left' });
}

/** `syncMayWriteTempo` against LIVE state, for the writes that outlive the
 * command that planned them. Deliberately reads `_masterDeck` and the deck's
 * BEAT SYNC flag at the moment of the call: a captured copy is exactly the bug
 * this guards (issue #1134). Effective, not raw, so a lit-but-inert BEAT SYNC
 * on a gridless deck does not count as sync ownership either. */
function _syncOwnsFollowerTempo(deck: DeckId): boolean {
	return syncMayWriteTempo(deck, effectiveBeatSync(deckStates[deck]), _masterDeck);
}

function _reanchorRampPending(rt: _DeckRuntime): boolean {
	return (
		rt.reanchorRampOwnerGeneration !== null &&
		rt.reanchorRampOwnerGeneration === rt.reanchorOperationGeneration
	);
}

function _bumpReanchorOperation(deck: DeckId): number {
	const rt = _rt[deck];
	const previous = rt.reanchorOperationGeneration;
	rt.reanchorOperationGeneration = previous + 1;
	if (rt.reanchorRampOwnerGeneration !== null && rt.reanchorRampOwnerGeneration <= previous) {
		rt.reanchorRampOwnerGeneration = null;
	}
	return rt.reanchorOperationGeneration;
}

function _reanchorOperationIsCurrent(deck: DeckId, generation: number): boolean {
	return _rt[deck].reanchorOperationGeneration === generation;
}

function _claimReanchorRampOwner(deck: DeckId, generation: number): void {
	_rt[deck].reanchorRampOwnerGeneration = generation;
}

function _releaseReanchorRampOwner(deck: DeckId, generation: number): void {
	const rt = _rt[deck];
	if (rt.reanchorRampOwnerGeneration === generation) rt.reanchorRampOwnerGeneration = null;
}

function _handleAudibleTransition(deck: DeckId, wasAudible: boolean, audible: boolean): void {
	void wasAudible;
	void audible;
	if (_masterMode !== 'auto') return;
	const st = deckStates[deck];
	if (_masterDeck === null && st.playing) {
		_electPlayingMaster({ reason: 'first-claim' });
	} else if (_masterDeck === deck && !st.playing) {
		_electPlayingMaster({ reason: 'master-left' });
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
	const positionMs = st.position_ms;
	const decodedDurationMs = st.duration_ms;
	const metadataDurationMs = rt.metadataDurationMs;
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
	recordUnexpectedPause({
		cause: 'worklet-error',
		deck,
		position_ms: positionMs,
		context_state: _ctx?.state ?? 'uninitialized',
		decoded_duration_ms: decodedDurationMs,
		metadata_duration_ms: metadataDurationMs,
		cause_error: error
	});
	withPauseOrigin('worklet', () => {
		_clearLoadedTrackState(st);
		st.processor_error = message;
		st.sync_error = message;
	});
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
	pressT0Ms?: number,
	reanchorGeneration?: number
): Promise<number> {
	const scheduleSession = _engineSession;
	const isCurrent = (): boolean => scheduleSession === _engineSession;
	if (!isCurrent()) throw new Error(`_scheduleDeck: engine session changed before deck ${deck} scheduled`);
	const rt = _rt[deck];
	const expectedLoadToken = rt.loadToken;
	const expectedProcessor = rt.processor;
	const predecessor = rt.scheduleTail;
	let release!: () => void;
	rt.desiredActive = active;
	// LATENCY-01: optimistic play glyph; LATENCY-02 armed launch keeps triangle until commit.
	deckStates[deck].playing = _quantizedLaunchAt[deck] !== null && active ? false : active;
	if (!active) {
		const st = deckStates[deck];
		notePlayingFallingEdge({
			origin: readPauseOrigin(),
			deck,
			position_ms: st.position_ms,
			duration_ms: st.duration_ms,
			metadata_duration_ms: rt.metadataDurationMs,
			processor_error: st.processor_error,
			context_state: _ctx?.state ?? 'uninitialized'
		});
	}
	rt.scheduleIntentCount += 1;
	rt.scheduleTail = new Promise<void>((resolve) => {
		release = resolve;
	});
	await predecessor;
	try {
		if (!isCurrent()) throw new Error(`_scheduleDeck: engine session changed before deck ${deck} was scheduled`);
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
			pressT0Ms,
			reanchorGeneration
		);
	} catch (error) {
		// Reconcile the optimistic write to the STANDING intent, not to the value
		// it had before: a newer command may already have superseded this one, and
		// rolling back to a stale value would clobber it. A processor failure and
		// a mid-queue reload both reset desiredActive to false first, so this
		// lands on "not playing" for a genuinely failed start.
		if (isCurrent()) {
			deckStates[deck].playing =
				_quantizedLaunchAt[deck] !== null && rt.desiredActive ? false : rt.desiredActive;
		}
		throw error;
	} finally {
		rt.scheduleIntentCount -= 1;
		release();
	}
}

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
	pressT0Ms: number | undefined,
	reanchorGeneration?: number
): Promise<number> {
	const scheduleSession = _engineSession;
	const isCurrent = (): boolean => scheduleSession === _engineSession;
	if (!isCurrent()) throw new Error(`_scheduleDeck: engine session changed before deck ${deck} was scheduled`);
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
		masterDelayMs: mixerState.headphones.master_delay_ms,
		active,
		...(pressToScheduleMs === undefined ? {} : { pressToScheduleMs })
	});
	const row = scheduleRowFacts(scheduleStages, _ctx.state, pressT0Ms);
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
		if (isCurrent() && rt.processor === processor) _recordProcessorFailure(deck, error);
		throw error;
	}
	if (!isCurrent()) throw new Error(`_scheduleDeck: engine session changed before deck ${deck} acknowledged`);
	if (rt.processor !== processor) {
		throw new Error(`_scheduleDeck: deck ${deck} processor was replaced before acknowledgement`);
	}
	if (
		reanchorGeneration !== undefined &&
		!_reanchorOperationIsCurrent(deck, reanchorGeneration)
	) {
		return scheduledInputSec;
	}
	recordPerfTiming(row.kind, scheduleStages, deck, row.labels);
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
	st.playing = _quantizedLaunchAt[deck] !== null ? false : rt.desiredActive;
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
		_reanchorRampPending(rt);
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
		if (pending.active && _quantizedLaunchAt[deck] !== null) {
			deckStates[deck].playing = true;
			_quantizedLaunchAt[deck] = null;
		}
	}
}

function _projectPositionAt(deck: DeckId, when: number): number {
	if (_ctx === null) throw new Error('_projectPositionAt: audio graph not initialised');
	const rt = _rt[deck];
	_commitPendingIfDue(deck);
	return _positionForSegment(_controlSegmentAt(rt, when), when, rt.durationSec);
}

function _transportLeadSec(deck: DeckId): number {
	return processorOnsetLeadSec(_rt[deck].latencySec);
}

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
	// CUEOUT-14: the room hears everything master_delay_ms later than the
	// render clock, so the playhead and PLAY light lag by exactly that.
	const observation = observePresentedTransportTimeline(
		rt.presentation,
		outputTimestamp,
		rt.durationSec,
		_ctx === null ? undefined : _ctx.currentTime,
		masterDelaySeconds(mixerState.headphones.master_delay_ms)
	);
	notePresentationClock(deck, observation.clock_stalled);
	if (!observation.accepted) return observation;
	const st = deckStates[deck];
	const wasAudible = st.audible;
	st.position_ms = observation.position_sec * 1000;
	st.audible = observation.audible;
	st.transport_pending = observation.transport_pending || _reanchorRampPending(rt);
	const presentedKeyShift = presentedKeyShiftSemitonesAt(
		rt.presentation,
		outputTimestamp.contextTime
	);
	if (presentedKeyShift !== null) st.key_shift_semitones = presentedKeyShift;
	if (wasAudible !== observation.audible) {
		_handleAudibleTransition(deck, wasAudible, observation.audible);
	}
	if (_ctx !== null) {
		const segment = _controlSegmentAt(rt, _ctx.currentTime);
		const safety = st.safety_loop;
		if (
			playbackReachedSafetyLoopOut({
				active: segment.active,
				startPositionSec: segment.startPositionSec,
				startContextTime: segment.startContextTime,
				tempoRatio: segment.tempoRatio,
				atContextTime: _ctx.currentTime,
				safety,
				liveLoop: st.loop
			})
		) {
			const nextLoop = phaseLockedSafetyLoop(
				st.anlz?.beatgrid.beats ?? [],
				safety!,
				rt.durationSec * 1000
			);
			if (nextLoop !== null) {
				void _scheduleDeck(
					deck,
					safeTransportScheduleTime(_ctx.currentTime, _transportLeadSec(deck)),
					nextLoop.in_ms / 1000,
					true,
					undefined,
					undefined,
					nextLoop
				).catch((error: unknown) => {
					if (rt.processor !== null) _recordProcessorFailure(deck, error);
				});
				return observation;
			}
		}
	}
	if (naturalEndNeedsRevisionedStop(st.playing, observation, rt.durationSec, rt.scheduleIntentCount)) {
		if (_ctx === null) throw new Error('natural-end cleanup requires an AudioContext');
		const ctx = _ctx;
		void withPauseOrigin('natural-end', () =>
			_scheduleDeck(
				deck,
				safeTransportScheduleTime(ctx.currentTime, _transportLeadSec(deck)),
				rt.durationSec,
				false
			)
		)
			.then(() => {
				if (_masterDeck === deck && !st.playing) {
					_electPlayingMaster({ reason: 'natural-end' });
				}
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
			if (observation?.audible || observation?.transport_pending || deckStates[deck].playing || deckStates[deck].audible) anyTransport = true;
		}
		// Feed TopBar audio-Hz meter (presentation publish rate ~= game FPS).
		const intentionalSilence = cueOnlyMonitoringActive(_djOutputProfileActive, mixerState, deckStates) || playingStemsIntentionallySilent(Object.values(deckStates));
		noteMasterSilence(_masterAnalyser, _externalRouteAnalyser, anyTransport, Date.now(), intentionalSilence);
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

function _clearLoadedTrackState(st: DeckState): void {
	const deck = st.deck_id, wasMaster = _masterDeck === deck;
	// audible flips BEFORE re-election (excludes this deck as its own replacement) and election runs BEFORE reconciling (r3912339497); stranded is captured NOW, before clearForDeck wipes it and before the scoped continuation below starts (r3912339491, second pass).
	st.playing = false; st.audible = false;
	if (wasMaster) {
		if (_masterMode === 'locked') _masterMode = 'auto';
		_electPlayingMaster({ force: true, reason: 'unload' });
	}
	const stranded = _beatgridResyncPorts.takePending(deck); _resyncTracking.clearForDeck(deck);
	_beatgridGuards.beforeClear(deck, stranded, 'reload');
	st.stable_id = null;
	st.source_path = null;
	st.title = null;
	st.artist = null;
	st.rating = null;
	st.bpm = null;
	st.key = null;
	st.key_shift_semitones = 0;
	st.key_sync_enabled = false;
	st.duration_ms = null;
	_rt[st.deck_id].metadataDurationMs = null;
	st.position_ms = 0;
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
	_rt[st.deck_id].reanchorOperationGeneration = 0;
	_rt[st.deck_id].reanchorRampOwnerGeneration = null;
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
	if (ctx.state === 'suspended' || (ctx.state as string) === 'interrupted') await resumeAudioContextOrReportDead(ctx);
	if (ctx.state !== 'running') {
		throw new Error(`AudioContext did not enter running state; current state is ${ctx.state}`);
	}
	// Belt for the statechange listener; the device-floor row stays idempotent.
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
	/** Q1: the press behind this sync, when one deck's start caused it. Set
	 * ONLY by `play`, whose followers are the single pressed deck; the resync
	 * callers leave it unset so a background re-anchor never files a press row. */
	pressT0Ms?: number;
}

/**
 * When a synced waveform seek snaps backwards, crossfade from the current bar
 * into beat 4 of the target bar so `landingSec` lands softer. Falls back to a
 * plain schedule with no buffer/lead-in, or where the mix buffer bypasses stems/pitch under Master Tempo.
 */
// `_scheduleDeck` for a sync path: explicit tempo/masterTempo (not `_schedulePress`'s "keep"), no loop/key, active always true.
function _scheduleSyncDeck(
	deck: DeckId,
	when: number,
	inputSec: number,
	tempoRatio: number,
	masterTempoEnabled: boolean,
	pressT0Ms?: number,
	reanchorGeneration?: number
): Promise<number> {
	return _scheduleDeck(
		deck,
		when,
		inputSec,
		true,
		tempoRatio,
		masterTempoEnabled,
		undefined,
		undefined,
		pressT0Ms,
		reanchorGeneration
	);
}

async function _scheduleFollowerBackwardBlend(
	deck: DeckId,
	syncAt: number,
	landingSec: number,
	tempoRatio: number,
	masterTempoEnabled: boolean,
	currentSec: number,
	isScheduleCurrent?: () => boolean,
	pressT0Ms?: number
): Promise<number> {
	if (isScheduleCurrent !== undefined && !isScheduleCurrent()) {
		throw new Error(`sync seek blend: engine session changed before deck ${deck} scheduled`);
	}
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
		processor instanceof AlignedStemDeckProcessor || masterTempoEnabled ||
		landingSec >= currentSec - 0.08
	) {
		return _scheduleSyncDeck(deck, syncAt, landingSec, tempoRatio, masterTempoEnabled, pressT0Ms);
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
		return _scheduleSyncDeck(deck, syncAt, landingSec, tempoRatio, masterTempoEnabled, pressT0Ms);
	}

	const scheduled = await _scheduleSyncDeck(deck, t0, incomingSec, tempoRatio, masterTempoEnabled, pressT0Ms);

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
 * one step turns that noise into an audible tempo jump, so the RATE eases via
 * `planTempoRatioRamp`'s small steps on the real AudioContext clock (never a
 * JS timer racing the audio graph). A rate-only ramp leaves phase behind by
 * its rate shortfall for good (LATENCY-06), so the start is shifted by exactly
 * that sum (`planPhaseCompensatedReanchor`) and phase lands on the last step.
 *
 * Every step is awaited while the shared `sync` command scope is claimed.
 * A later master update must not observe an intermediate desired revision
 * while this follower's own schedule tail is still registering its ramp.
 *
 * `reanchorRampOwnerGeneration` holds `transport_pending` true for this deck
 * across the whole ramp. Each step's own schedule revision becomes "presented"
 * as soon as real playback reaches it - which can happen before the *next*
 * step's own worklet round-trip finishes registering - so without this
 * override a caller polling `!transport_pending` could catch that gap and
 * read/measure position while the rate is still mid-transition.
 */
async function _scheduleReanchoredFollower(
	deck: DeckId,
	syncAt: number,
	inputSec: number,
	toTempoRatio: number,
	masterTempoEnabled: boolean,
	isScheduleCurrent?: () => boolean,
	pressT0Ms?: number
): Promise<number> {
	if (isScheduleCurrent !== undefined && !isScheduleCurrent()) {
		throw new Error(`sync re-anchor: engine session changed before deck ${deck} scheduled`);
	}
	const fromTempoRatio = _tempoAt(deck, syncAt);
	const { startPositionSec, steps: ramp } = planPhaseCompensatedReanchor(inputSec, fromTempoRatio, toTempoRatio);
	const generation = _bumpReanchorOperation(deck);
	_claimReanchorRampOwner(deck, generation);
	let scheduledInputSec: number;
	try {
		scheduledInputSec = await _scheduleSyncDeck(
			deck,
			syncAt,
			startPositionSec,
			ramp[0].tempoRatio,
			masterTempoEnabled,
			pressT0Ms,
			generation
		);
	} catch (error) {
		_releaseReanchorRampOwner(deck, generation);
		throw error;
	}
	await _continueTempoRamp(deck, syncAt, ramp.slice(1), masterTempoEnabled, generation);
	return scheduledInputSec;
}

/** Remaining tail of an awaited re-anchor ramp. Position is deliberately left to
 * `_projectPositionAt` (the same projection every other tempo-only mutation
 * here uses) rather than re-stated from the plan, since only the rate is
 * changing at each step. Owner-aware finalization clears `transport_pending`
 * only for the generation that claimed it. */
async function _continueTempoRamp(
	deck: DeckId,
	syncAt: number,
	remainingSteps: readonly TempoRampStep[],
	masterTempoEnabled: boolean,
	generation: number
): Promise<void> {
	try {
		for (const step of remainingSteps) {
			// Re-read ownership before EVERY step, never once at plan time. The
			// DJ can dim BEAT SYNC or promote this deck to master mid-ramp, and
			// from that instant the remaining steps would be sync moving a
			// tempo it no longer owns - issue #1134's "the master's BPM keeps
			// changing with both toggles off". Stop, do not fight the operator.
			// #1112 made this tail awaited and its failures loud; ownership is
			// the orthogonal half, so the schedule below stays exactly as it is.
			if (!_reanchorOperationIsCurrent(deck, generation) || !_syncOwnsFollowerTempo(deck)) return;
			await _scheduleDeck(
				deck,
				syncAt + step.offsetSec,
				(effectiveWhen) => _projectPositionAt(deck, effectiveWhen),
				true,
				step.tempoRatio,
				masterTempoEnabled,
				undefined,
				undefined,
				undefined,
				generation
			);
			if (!_reanchorOperationIsCurrent(deck, generation)) return;
		}
	} finally {
		_releaseReanchorRampOwner(deck, generation);
	}
}

async function _synchronizeFollowers(
	master: DeckId,
	followers: readonly DeckId[],
	options: _SyncOptions = {}
): Promise<void> {
	if (followers.length === 0 && options.masterSchedule === undefined) return;
	// Every guard the two beatgrid-resync callers apply to their PORTS is asked
	// before this function is entered; none of them survives the awaits INSIDE
	// it. `_resumeContext()` alone is an open-ended wait, and everything below
	// reads and writes the module-global `_rt` / `deckStates` by deck id - so a
	// dispose() plus route remount landing in that window let this continuation
	// schedule transport on a brand new session's decks
	// (discussion_r3919692507 P1 BLOCKING). The session id is captured once
	// here and re-asked after each await, which abandons the whole call rather
	// than threading a cancellation token through every port.
	const session = _engineSession;
	const scheduleSessionIsCurrent = (): boolean => session === _engineSession;
	let succeededDecks: readonly DeckId[] = [];
	// Invariant, not a defensive nicety: the master is never one of its own
	// followers. Every call site already filters it out, so reaching here means
	// a caller inverted the roles - fail loudly rather than schedule the master
	// a follower's tempo and leave the DJ wondering why nothing locks (#1134).
	if (followers.includes(master)) {
		throw new RangeError(
			`Beat Sync: master deck ${master} cannot be one of its own followers ` +
				`(followers=[${followers.join(',')}])`
		);
	}
	try {
		const ctx = await _resumeContext();
		if (session !== _engineSession) return;
		_commitPendingIfDue(master);
		for (const deck of followers) _commitPendingIfDue(deck);
		const masterState = deckStates[master];
		const masterRuntime = _requireLoaded(master, 'Beat Sync master').rt;
		if (!masterRuntime.desiredActive) {
			throw new Error(`Beat Sync master deck ${master} is neither audible nor scheduled to play`);
		}
		const masterGrid = requireBeatGrid(masterState, 'Beat Sync');
		// Only clear stale pending-membership once the master's own grid
		// precondition is confirmed - clearing it BEFORE this point (the
		// original ordering) discarded a follower's pending record on a
		// synchronization attempt that itself turns out to be against a
		// gridless master, so a later grid landing never retried it and the
		// follower stayed enabled with every future PLAY rejecting the same
		// way (discussion_r3914557002). Once the master's grid is confirmed,
		// any pending record naming this master IS stale (it can only have
		// been recorded while the master had no grid), so it is safe to drop
		// for both master and followers regardless of whether a later plan
		// or schedule stage in this same call still fails for an unrelated
		// (tempo/BAR) reason.
		_resyncTracking.clearPendingMembership(master);
		for (const deck of followers) _resyncTracking.clearPendingMembership(deck);
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
			if (session !== _engineSession) return;
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
		// Ownership is re-read HERE, downstream of every await above (context
		// resume, and the superseded-pending wait that can park this call for a
		// scheduled instant). A deck whose BEAT SYNC went dark inside that
		// window owns its own tempo again: withdrawing it is the correct
		// outcome, not a phase-lock failure, so it carries no sync_error.
		if (session !== _engineSession) return;
		const owned = followers.filter((deck) => _syncOwnsFollowerTempo(deck));
		if (owned.length === 0 && options.masterSchedule === undefined) return;
		for (const deck of owned) {
			try {
				const { st } = _requireLoaded(deck, 'Beat Sync follower');
				const followerGrid = requireBeatGrid(st, 'Beat Sync');
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
					mode: syncModeForBeatSyncMax(uiPrefs.beat_sync_max, st.sync_mode)
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
		const scheduleOperations = schedules.map((item, index) => {
				if (item.blendFromSec !== null) {
					return _scheduleFollowerBackwardBlend(
						item.deck,
						scheduleTimes[index],
						item.inputSec,
						item.tempoRatio,
						item.masterTempoEnabled,
						item.blendFromSec,
						scheduleSessionIsCurrent,
						options.pressT0Ms
					);
				}
				if (options.reanchorDecks?.has(item.deck) === true) {
					// 'master-max' fills reanchorDecks with the OTHER followers, never
					// the pressed master, so withhold whenever masterSchedule is set.
					return _scheduleReanchoredFollower(
						item.deck,
						scheduleTimes[index],
						item.inputSec,
						item.tempoRatio,
						item.masterTempoEnabled,
						scheduleSessionIsCurrent,
						options.masterSchedule === undefined ? options.pressT0Ms : undefined
					);
				}
				if (!scheduleSessionIsCurrent()) {
					throw new Error(`Beat Sync: engine session changed before deck ${item.deck} scheduled`);
				}
				return _scheduleDeck(
					item.deck,
					scheduleTimes[index],
					item.inputSec,
					true,
					item.tempoRatio,
					item.masterTempoEnabled,
					undefined,
					undefined,
					options.pressT0Ms
				);
			});
		const outcomes = await Promise.allSettled(scheduleOperations);
		if (session !== _engineSession) return;
		const failedDecks = outcomes.flatMap((outcome, index) =>
			outcome.status === 'rejected' ? [schedules[index].deck] : []
		);
		if (failedDecks.length > 0) {
			succeededDecks = schedules.map((item) => item.deck).filter((deck) => !failedDecks.includes(deck));
			const message =
				`Beat Sync partial failure: succeeded [${succeededDecks.join(',')}], ` +
				`failed [${failedDecks.join(',')}]`;
			for (const item of schedules) {
				item.st.sync_error = failedDecks.includes(item.deck) ? message : null;
			}
			throw new Error(message, {
				cause: outcomes.find((outcome) => outcome.status === 'rejected')
			});
		}
		for (const item of planned) item.st.sync_error = null;
		// What a completed sync tells the DJ is decided in beat-sync-math.ts as
		// a pure function; the engine only performs the effects it returns.
		for (const notice of beatSyncOutcomeNotices(planned, planFailed, master)) {
			pushToast(notice.message, notice.kind, undefined, undefined, {}, notice.groupKey);
			for (const event of notice.events) {
				recordPerfEvent(event.kind, event.detail, event.deck, notice.kind);
			}
		}
	} catch (error) {
		// Same session gate as the awaits above: a rejection that surfaces only
		// after disposal must not brand a remounted session's decks with an
		// error raised against the decks they replaced.
		if (session !== _engineSession) throw error;
		for (const deck of followers) {
			if (!succeededDecks.includes(deck) && deckStates[deck].sync_error === null) deckStates[deck].sync_error = String(error);
		}
		if (options.masterSchedule !== undefined && !succeededDecks.includes(master)) deckStates[master].sync_error = String(error);
		throw error;
	}
}
const _resyncTracking = createBeatgridResyncTracking(); // pending/gridless tracking; cleared per deck below, unload() and dispose() - see beatgrid-resync.ts
const _beatgridResyncPorts: BeatgridResyncPorts = {
	deckIds: DECK_IDS, syncMaster: _syncMaster, playing: (deck) => deckStates[deck].playing,
	beatSyncEnabled: (deck) => deckStates[deck].beat_sync_enabled, setBeatSyncEnabled: (deck, enabled) => (deckStates[deck].beat_sync_enabled = enabled),
	hasRealBeatGrid: (deck) => deckHasRealBeatGrid(deckStates[deck]), hasSyncError: (deck) => deckStates[deck].sync_error !== null,
	setSyncError: (deck, message) => (deckStates[deck].sync_error = message), requiresReschedule: syncChangeRequiresReschedule,
	synchronizeFollowers: _synchronizeFollowers, ..._resyncTracking
};
const _beatgridGuards = createBeatgridResyncGuards({
	ports: _beatgridResyncPorts,
	deckRuntime: (deck) => _rt[deck],
	deckLoadToken: (deck) => _rt[deck].loadToken,
	deckStableId: (deck) => deckStates[deck].stable_id,
	deckAnlz: (deck) => deckStates[deck].anlz,
	publishDeckAnlz: (deck, anlz) => (deckStates[deck].anlz = anlz),
	publishDeckBpm: (deck, bpm) => (deckStates[deck].bpm = bpm),
	setDeckAnlzError: (deck, code) => (deckStates[deck].anlz_error = code),
	reconcileDeckLoop: (deck, anlz) => reconcileLoopForAuthoritativeGrid(deckStates[deck], anlz),
	reportError: (message) => pushToast(message, 'error'),
	desiredBeatgridSource: () => analysisSourceState.features.beatgrid
});
installAuthoritativeAnlzGridSink(_beatgridGuards.adoptAuthoritativeGrid);
installAuthoritativeAnlzErrorSink(_beatgridGuards.adoptAuthoritativeError);
export const installScopedSyncRunner = _beatgridGuards.installScopedSyncRunner; // rationale for [deck]-then-widen: performance-ipc.svelte.ts's installScopedSyncRunner

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
	const stale = (): boolean => token !== rt.loadToken || ctx !== _ctx; // a graph rebuild restarts it
	let built: AlignedStemDeckProcessor | null = null;
	try {
		const probe = await time('probeStem', awaitStemArtifact(stableId, { isStale: stale }));
		if (probe === null || stale()) return;
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
		// Q18: four workers, not four awaits on WebKit's single decode thread.
		const decoded = await time('decodeStems', decodeStemBuffers(ctx, encodedParts, layoutParts));
		if (stale()) return;
		const stemBuffers = decoded.buffers;
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
		recordPerfTiming(`deck-stems sid=${stableId.slice(0, 12)}`, stages, deck, decoded.labels);
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

/** CUEOUT-15: build and resume the graph for a non-deck source (the library
 * preview), which must work before any deck has loaded. Rejects, never silent. */
export async function ensureAudioGraphForCue(): Promise<void> {
	await _resumeContext();
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
		_engineSession += 1;
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
		if (_masterDelay !== null) nodes.push(_masterDelay);
		nodes.push(..._djOutputNodes);
		const closingContext = _ctx;
		const closing = disposeAudioResources({
			rafId: _rafId,
			processors,
			nodes,
			masterGain: _masterGain,
			context: closingContext
		});

		_rafId = null;
		_masterGain = null;
		releaseMasterMeterTap();
		// The mute VALUE survives teardown on purpose: a route remount must not
		// hand a headless agent its audio back. Only the node is released.
		attachMasterMuteNode(null);
		_masterMuteGain = null;
		_masterDelay = null;
		_externalMerger = _externalRouteAnalyser = null;
		_djOutputNodes = [];
		_djOutputProfileActive = null;
		if (closingContext !== null) unregisterAudioContext(closingContext);
		_ctx = null;
		_masterDeck = null;
		_masterMode = 'auto';
		_masterReason = 'dispose';
		for (const deck of DECK_IDS) {
			_rt[deck] = _emptyRuntime();
			deckStates[deck] = _emptyDeckState(deck);
			deckLoadErrors[deck] = null; pitchRanges[deck] = 16;
			mixerState.channels[deck] = _defaultChannel(deck); _resyncTracking.clearForDeck(deck); // singleton - reused ids must not inherit stale state (r3912757819)
		}
		mixerState.crossfader = 0.5;
		mixerState.master = 1; mixerState.headphones = _defaultHeadphones();
		await closing;
	}

	async load(deck: DeckId, stable_id: string): Promise<void> {
		if (stable_id.length === 0) throw new Error('load: stable_id must be non-empty');
		const st = deckStates[deck];
		const rt = _rt[deck];
		_assertCurrentDeckReplacementAllowed(deck);
		const token = ++rt.loadToken;
		const replacingMaster = _masterDeck === deck;
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
		// Stage timings + load conditions for DevTools `[perf]`; spanId binds every recordDeckLoad below to THIS load's own span (#1658).
		const { clock: perfMs, spanId } = beginDeckLoad(deck);
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
			// A cache hit stays on the critical path but is no longer TRUSTED for
			// the session - see `revalidateAnlz`'s own doc for why.
			if (anlzCached) revalidateAnlz(stable_id);
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
			const audio = deckLoadAudio(stable_id, _ctx?.sampleRate ?? null, fetchAudioArrayBuffer);
			// LAZY-STEMS: the critical path fetches ONLY what first playback needs.
			// `probeStem` used to ride here as a fifth request and, being last in
			// the list behind a multi-MB audio download on a single-worker engine,
			// it held the fetch wall on its own (PR #601 measured 47% of it for a
			// 1-9ms endpoint). It now runs after the swap, in _upgradeDeckStems.
			const [trackRes, audioBytes, requiredAnlz, requiredHotCueSlots] =
				await Promise.all([
					time('getTrack', getTrack(stable_id)),
					time(audio.fetchStage, audio.bytes),
					time(anlzCached ? 'anlzCacheHit' : 'fetchAnlz', anlzPromise),
					time('fetchHotCues', fetchHotCueSlots(stable_id))
				]);
			stages.fetchWall = perfMs();
			Object.assign(stages, audio.stats(audioBytes));
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
				time(audio.decodeStage, decodeDeckLoadAudio(ctx, stable_id, audio, audioBytes)),
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
			recordDeckLoad('deck-load-fail', stages, deck, candidateStemState, spanId);
			if (processor !== null) {
				try {
					await processor.dispose();
				} catch {
					// Preserve the load failure; dispose() closes the port in finally.
				}
			}
			if (token !== rt.loadToken) throw exc;
			assertDeckLoadConsistency(st.stable_id, rt.durationSec, rt.processor !== null);
			const raw =
				exc instanceof RbApiError ? `${exc.code}: ${exc.message}` : String(exc);
			const msg = formatDeckLoadFailureMessage(track?.title, stable_id, raw);
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
			st.source_path =
				typeof candidateTrack.file_path === 'string' && candidateTrack.file_path.length > 0
					? candidateTrack.file_path
					: null;
			// TrackOut spells every nullable field optional (a pydantic default
			// becomes a not-required property), so absent and null both land as
			// the deck's "unknown" null.
			st.title = candidateTrack.title ?? null;
			st.artist = candidateTrack.artist ?? null;
			st.rating = candidateTrack.rating ?? null;
			st.key = candidateTrack.key ?? null;
			// The decoded buffer is the audio actually scheduled. Metadata can
			// differ, so it must not define waveform bounds or transport truth.
			st.duration_ms = decodedTransportDurationMs(candidateBuffer.duration);
			rt.metadataDurationMs = candidateTrack.duration_ms ?? null;
			// Re-reads the shared cache rather than trusting `candidateAnlz` -
			// see `resolvePublishedAnlz`'s own doc for the race this guards.
			const latestAnlzEntry = getAnlzEntry(stable_id);
			const usableAnlz = isAnlzEntryUsable(latestAnlzEntry) ? (latestAnlzEntry.data as AnlzWithVocals) : null;
			const latestAnlzError = latestAnlzEntry?.status === 'error' ? latestAnlzEntry.code : null;
			const { anlz: publishedAnlz, anlzError: publishedAnlzError, bpm: publishedBpm } = resolvePublishedAnlz(usableAnlz, latestAnlzError, candidateAnlz, candidateTrack.bpm ?? null);
			st.anlz = publishedAnlz;
			st.anlz_error = publishedAnlzError;
			// Kept in lockstep with publishedAnlz - see `resolvePublishedAnlz`'s own doc.
			st.bpm = publishedBpm;
			st.processor_error = null;
			st.sync_error = null;
			st.stems = candidateStemState;
			st.hot_cues = _hotCuesFromSlots(hotCueSlots);
			st.hot_cue_revisions = _hotCueRevisionsFrom(hotCueSlots);
			st.has_rb_mapping = candidateTrack.has_rb_mapping;
			st.loop = displayLoopFrom(publishedAnlz.cues, publishedAnlz.beatgrid.beats);
			if (replacingMaster) {
				if (_masterMode === 'locked') _masterMode = 'auto';
				_electPlayingMaster({ force: true, reason: 'unload' });
			}
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
		}).catch((exc: unknown) => {
			// A swap failure is still a load that STARTED; without this the span
			// beginDeckLoad opened never closes and later rows report stale solo=0 (#1658 review).
			stages.total = perfMs();
			recordDeckLoad('deck-load-fail-swap', stages, deck, candidateStemState, spanId);
			throw exc;
		});
		stages.total = perfMs();
		st.last_load_latency_ms = stages.total;
		st.load_generation += 1;
		st.last_load_stages = { ...stages };
		recordDeckLoad(`deck-load sid=${stable_id.slice(0, 12)}`, stages, deck, candidateStemState, spanId);
		// LAZY-STEMS: deliberately NOT awaited. `load` resolves as soon as the
		// deck can play; the stem bundle lands afterwards and moves st.stems off
		// `loading` on its own. Errors are handled inside, so no rejection can
		// escape into an unhandled promise.
		if (loadCtx === null) throw new Error('load: audio context was never resolved');
		void _upgradeDeckStems(deck, stable_id, token, loadCtx, candidateBuffer);
			void upgradeDeckBeatgrid(deck, stable_id, st, () => token !== rt.loadToken, (d, landed, publish) => _beatgridGuards.afterBeatgridUpgrade(d, landed, publish, () => token !== rt.loadToken)); // PARITY-10: same deferral for the grid as _upgradeDeckStems above; errors are handled inside, no unhandled rejection
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
		const generation = currentAnlzFetchGeneration();
		const [initialFresh, slots] = await Promise.all([
			fetchAnlzBypassingHttpCache(stableId),
			fetchHotCueSlots(stableId)
		]).catch((err: unknown) => {
			invalidateAnlzCacheEntry(stableId);
			throw err;
		});
		// A source switch (PARITY-02) mid-flight can supersede the fetch above;
		// re-run it under the shared guard (fetchAnlzUntilSourceConfirmed,
		// anlz-cache.svelte.ts) rather than publish a superseded grid
		// (discussion_r3973991964 P1 BLOCKING). Also re-checked here on the fast
		// path (anlzMatchesConfirmedSource): the generation counter alone misses
		// an EXTERNAL client's direct source toggle, which never bumps it
		// (discussion_r3978049099 P1 BLOCKING). Hot-cue slots are not
		// source-dependent, so the ones already fetched stay valid either way.
		const fresh =
			generation === currentAnlzFetchGeneration() && anlzMatchesConfirmedSource(initialFresh)
				? initialFresh
				: await fetchAnlzUntilSourceConfirmed(() => fetchAnlzBypassingHttpCache(stableId));
		if (st.stable_id !== stableId) return; // deck was swapped mid-request
		refreshAnlzCacheEntry(stableId, fresh);
		st.anlz = fresh;
		st.hot_cues = _hotCuesFromSlots(slots);
		st.hot_cue_revisions = _hotCueRevisionsFrom(slots);
		st.loop = displayLoopFrom(fresh.cues, fresh.beatgrid.beats);
	}

	/** Q1: `pressT0Ms` is the operator's input stamp - see `$lib/rb/press-stamp`. */
	async play(deck: DeckId, pressT0Ms?: number, startAtContextSec?: number): Promise<void> {
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
		const syncClock = _syncClockMaster();
		const owned = _ownedMaster();
		const schedulePlainTransport = async (forcedWhen?: number): Promise<void> => {
			const minimumWhen = safeTransportScheduleTime(ctx.currentTime, _transportLeadSec(deck));
			const when =
				forcedWhen === undefined ? minimumWhen : Math.max(minimumWhen, forcedWhen);
			await _schedulePress(deck, when, startSec, true, pressT0Ms);
			st.sync_error = null;
		};
		if (startAtContextSec !== undefined) {
			await schedulePlainTransport(startAtContextSec);
			return;
		}
		if (_masterMode === 'locked' && owned !== null && owned !== deck) {
			if (syncClock !== null && syncActive) {
				await _synchronizeFollowers(syncClock, [deck], {
					...(pressT0Ms === undefined ? {} : { pressT0Ms })
				});
			} else {
				await schedulePlainTransport();
			}
		} else if (_masterMode === 'auto' && syncClock === null) {
			await schedulePlainTransport();
			_electPlayingMaster({ reason: 'play-claim' });
			const elected = _masterDeck;
			if (elected !== null && elected !== deck && syncActive) {
				await _synchronizeFollowers(elected, [deck], {
					...(pressT0Ms === undefined ? {} : { pressT0Ms })
				});
			}
		} else if (_masterMode === 'locked' && owned === deck && syncClock === null) {
			await schedulePlainTransport();
		} else if (syncClock === deck || !syncActive) {
			await schedulePlainTransport();
		} else if (syncClock !== null) {
			await _synchronizeFollowers(syncClock, [deck], {
				...(pressT0Ms === undefined ? {} : { pressT0Ms })
			});
		}
	}

	/** Q1: see `play` for the `pressT0Ms` contract. */
	async pause(deck: DeckId, pressT0Ms?: number): Promise<void> {
		return withPauseOrigin('command', async () => {
			this.clearQuantizedLaunch(deck);
			const { st, rt } = _requireLoaded(deck, 'pause');
			if (!rt.desiredActive) return; // already paused is a valid state
			_bumpReanchorOperation(deck);
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
				? quantizedPositionMs(pauseBeats, positionSec * 1000, true, _quantizeGridBeats(st))
				: positionSec * 1000;
			st.cue_ms = cueMs;
			if (st.slip_active) _clearSlip(deck);
			// LAZY-STEMS: the deck has just come to rest, so a stem bundle that
			// finished decoding mid-play can land now without touching live audio.
			_drainPendingStemUpgrade(deck);
		});
	}

	async cueJump(deck: DeckId, ms: number): Promise<void> {
		await this.quantizedSeek(deck, ms);
	}

	async quantizedSeek(deck: DeckId, ms: number, skipGridQuantize = false, pressT0Ms?: number): Promise<void> {
		const { st, rt } = _requireLoaded(deck, 'cueJump');
		const durMs = _durationSec(deck) * 1000;
		if (!Number.isFinite(ms) || ms < 0 || ms > durMs) {
			throw new RangeError(`cueJump: ms must be within 0..${Math.round(durMs)}, got ${ms}`);
		}
		const seekBeats = _quantizeGrid(st);
		const { targetMs, exitLoop } = quantizedSeekDecisionMs(seekBeats, ms, _quantizeGridBeats(st), skipGridQuantize, st.loop);
		if (targetMs > durMs) {
			throw new RangeError(`cueJump: quantized target ${targetMs} exceeds duration ${durMs}`);
		}
		// Rekordbox: seeking outside an engaged loop exits the loop and plays
		// from the clicked point. Keep modulo wrap only for in-loop transport.
		// Clear the loop BEFORE phase sync so the shared schedule path does not
		// wrap the target back into the old loop (BeatSyncMax / follower sync).
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
					reanchorDecks: new Set([deck]),
					...(pressT0Ms === undefined ? {} : { pressT0Ms })
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
					reanchorDecks: new Set(syncPlan.followers),
					...(pressT0Ms === undefined ? {} : { pressT0Ms })
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
					scheduleLoop,
					undefined,
					pressT0Ms
				);
			}
		} else {
			_setPausedPosition(deck, targetMs);
		}
	}

	/** The physical CUE button. Playing: return to the cue point and pause.
	 * Paused with a cue set: jump the playhead to it. Paused with no cue:
	 * set the cue at the current position. Q1: see `play` for the stamp; the
	 * seek branch also carries it through `quantizedSeek`. */
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
					? quantizedPositionMs(cueBeats, st.position_ms, true, _quantizeGridBeats(st))
					: st.position_ms;
		} else {
			await this.quantizedSeek(deck, st.cue_ms, undefined, pressT0Ms);
		}
	}

	/**
	 * DECKUX-09: schedule `targetPositionMs` to land at `armAtPositionSec` on
	 * this deck's OWN transport clock, via the graph's normal pending-segment
	 * queue - no timer needed, since a pending segment leaves the reported
	 * position and audible playback untouched until its own startContextTime
	 * (`_commitPendingIfDue`). Returns the absolute AudioContext time the
	 * schedule lands at, for the dispatcher's armed/countdown IPC projection.
	 *
	 * Self-referential only: unlike `quantizedSeek`'s syncPlan branch, this
	 * does not additionally re-plan cross-deck follower phase (#884 scope -
	 * that is the other, unrelated meaning of BeatSyncMax, for seek).
	 */
	async armHotCueTrigger(deck: DeckId, targetPositionMs: number, armAtPositionSec: number, pressT0Ms?: number): Promise<number> {
		const { rt } = _requireLoaded(deck, 'armHotCueTrigger');
		if (_ctx === null) throw new Error('armHotCueTrigger: audio graph not initialised');
		const nowPositionSec = _projectPositionAt(deck, _ctx.currentTime);
		if (armAtPositionSec < nowPositionSec) {
			throw new RangeError(
				`armHotCueTrigger: armAtPositionSec ${armAtPositionSec} precedes current position ${nowPositionSec}`
			);
		}
		const deltaContextSec = (armAtPositionSec - nowPositionSec) / rt.controlTempoRatio;
		const targetContextTime = Math.max(_futureScheduleTime(deck), _ctx.currentTime + deltaContextSec);
		await _schedulePress(deck, targetContextTime, targetPositionMs / 1000, rt.desiredActive, pressT0Ms);
		return targetContextTime;
	}

	clearQuantizedLaunch(deck: DeckId): void {
		const rt = _rt[deck];
		const launchAt = _quantizedLaunchAt[deck];
		if (launchAt !== null) {
			rt.pending = rt.pending.filter((pending) => !(pending.active && pending.startContextTime >= launchAt));
			_quantizedLaunchAt[deck] = null;
		}
		if (!rt.controlActive) {
			rt.desiredActive = false;
			deckStates[deck].playing = false;
		}
	}

	async armQuantizedLaunch(deck: DeckId, pressT0Ms?: number): Promise<number> {
		const { st, rt } = _requireLoaded(deck, 'armQuantizedLaunch');
		const master = _syncClockMaster();
		const ctx = await _resumeContext();
		const masterState = master === null ? null : deckStates[master];
		if (master !== null && masterState === null) {
			throw new Error(`armQuantizedLaunch: deck ${master} has no state despite being the sync clock master`);
		}
		const { launchAtContextSec, followerStartSec } = computeQuantizedLaunchArm({
			followerPlaying: st.playing,
			followerDesiredActive: rt.desiredActive,
			followerTrustedGrid: st.anlz !== null && hasTrustedBeatGrid(st.anlz),
			masterDeck: master,
			masterSameAsFollower: master === deck,
			masterPlaying: masterState?.playing === true,
			masterTrustedGrid:
				masterState !== null && masterState.anlz !== null && masterState.anlz !== undefined && hasTrustedBeatGrid(masterState.anlz),
			nowContextTimeSec: ctx.currentTime,
			processorLeadSec: _transportLeadSec(deck),
			masterBeats: masterState === null ? [] : requireBeatGrid(masterState, QUANTIZED_LAUNCH),
			masterPositionSec: master === null ? 0 : _projectPositionAt(master, ctx.currentTime),
			masterTempoRatio: master === null ? 1 : _tempoAt(master, ctx.currentTime),
			followerBeats: requireBeatGrid(st, QUANTIZED_LAUNCH),
			followerPositionSec: st.position_ms / 1000
		});
		this.clearQuantizedLaunch(deck);
		rt.desiredActive = true;
		_quantizedLaunchAt[deck] = launchAtContextSec;
		const startSec = playResumePositionSec(followerStartSec, rt.durationSec, st.loop);
		await _schedulePress(deck, launchAtContextSec, startSec, true, pressT0Ms);
		return launchAtContextSec;
	}

	/** The engine's AudioContext clock, for projecting an armed hot-cue
	 * trigger's remaining wait without exposing the context itself. */
	contextTimeNowSec(): number {
		if (_ctx === null) throw new Error('contextTimeNowSec: audio graph not initialised');
		return _ctx.currentTime;
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
		await this._setLoop(deck, loop, null);
	}

	/** Beat-loop callers already resolved endpoints from PQTZ. Do not re-snap
	 * those to the manual loop grid: that would collapse fractional loops. */
	private async _setLoop(
		deck: DeckId, loop: { in_ms: number; out_ms: number } | null, beatLength: number | null
	): Promise<void> {
		const { st } = _requireLoaded(deck, 'setLoop');
		const wasPlaying = st.playing;
		const scheduleAt = wasPlaying ? _futureScheduleTime(deck) : 0;
		if (loop === null) {
			st.safety_loop = disarmSafetyLoopOnExplicitExit(st.safety_loop);
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
		const loopBeats = _quantizeGrid(st);
		const nextLoop = resolvedLoopState(
			loop, beatLength, _durationSec(deck) * 1000, loopBeats,
			loopBeats !== null && beatLength === null ? _quantizeGridBeats(st) : null
		);
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

	/** Engage a beat loop using exact consecutive PQTZ timestamps.
	 *
	 * A fresh (no explicit start_ms) four-beat loop anchors on the preceding
	 * PQTZ downbeat rather than the nearest beat, so timing is forgiving of a
	 * click that landed slightly late - see precedingDownbeatMs. Every other
	 * length, or an explicit start_ms, keeps the ordinary nearest-beat anchor.
	 *
	 * Reissuing the exact same beats + resulting range as the already-engaged
	 * loop is a RESTART, not a re-engage: instead of leaving the playhead
	 * running wherever it is (setLoop's ordinary behaviour), it schedules the
	 * playhead back to loop-in while retaining the same loop object, in one
	 * engine transaction. Paused decks publish loop-in immediately.
	 */
	async engageBeatLoop(deck: DeckId, beats: number, startMs?: number): Promise<void> {
		const { st } = _requireLoaded(deck, 'engageBeatLoop');
		const grid = requireBeatGrid(st, 'engageBeatLoop');
		const currentMs = st.playing ? _currentPosSec(deck) * 1000 : st.position_ms;
		const anchorMs =
			beats === 4 && startMs === undefined ? precedingDownbeatMs(grid, currentMs) : startMs;
		const range = exactBeatLoopRangeMs(grid, currentMs, beats, anchorMs);
		const isRestart =
			st.loop !== null &&
			st.loop.engaged &&
			st.loop.beat_length === beats &&
			st.loop.in_ms === range.in_ms &&
			st.loop.out_ms === range.out_ms;
		if (isRestart) {
			const retainedLoop = st.loop as LoopState;
			if (st.playing) {
				if (_ctx === null) throw new Error('engageBeatLoop: audio graph not initialised');
				await _scheduleDeck(
					deck,
					_futureScheduleTime(deck),
					range.in_ms / 1000,
					true,
					undefined,
					undefined,
					retainedLoop
				);
			} else {
				_setPausedPosition(deck, range.in_ms);
			}
			return;
		}
		const previousLoop = st.loop === null ? null : { ...st.loop };
		await this._setLoop(deck, range, beats);
		const pending = _rt[deck].pending;
		const pendingLoop = pending[pending.length - 1]?.loop;
		const nextLoop = pendingLoop ?? st.loop;
		if (previousLoop !== null && nextLoop !== null) {
			st.safety_loop = replaceMatchingSafetyLoopSnapshot(st.safety_loop, previousLoop, nextLoop);
		}
	}

	/** Jump whole PQTZ beats, anchored on the playhead projected to the next
	 * safe schedule time so back-to-back jumps compound, including when a
	 * pending mutation (e.g. a not-yet-presented pause) has not landed yet;
	 * math + duration clamp live in beat-sync-math.ts. */
	async beatJump(deck: DeckId, beats: number): Promise<void> {
		const { st } = _requireLoaded(deck, 'beatJump');
		const grid = requireBeatGrid(st, 'beatJump');
		const anchorMs = _projectPositionAt(deck, _futureScheduleTime(deck)) * 1000;
		const rawTargetMs = beatJumpTargetMs(grid, anchorMs, beats);
		const targetMs = beatJumpTargetWithinDurationMs(grid, rawTargetMs, _durationSec(deck) * 1000);
		if (st.loop !== null && st.loop.engaged) {
			const previousLoop = st.loop;
			// quantizedSeek preserves an in-range loop. Shift its exact PQTZ
			// endpoints first, then pass skipGridQuantize=true (334a50710ef0
			// defect A): the deck's own coarser 1/4/8-beat grid must never
			// re-snap the already-safe exact-beat target onto the out bound.
			const shiftedLoop = shiftLiveBeatLoopRangeMs(
				grid,
				previousLoop,
				beats,
				_durationSec(deck) * 1000
			);
			st.loop = {
				...shiftedLoop,
				engaged: true,
				beat_length: previousLoop.beat_length
			};
			try {
				await this.quantizedSeek(deck, targetWithinShiftedLiveLoopMs(grid, targetMs, shiftedLoop), true);
			} catch (error) {
				st.loop = previousLoop;
				throw error;
			}
			return;
		}
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
			pushToast(`Deck ${deck} QUANTIZE ${gridFeatureInertTip(st)}`, 'info');
		}
	}

	setQuantizeGrid(deck: DeckId, beats: Exclude<QuantizeGrid, 'phase'>): void {
		if (beats !== 1 && beats !== 4 && beats !== 8) {
			throw new TypeError('setQuantizeGrid: beats must be 1, 4, or 8');
		}
		const st = deckStates[deck];
		st.quantize_grid_beats = beats;
	}

	setBeatSync(deck: DeckId, enabled: boolean): Promise<void> {
		if (typeof enabled !== 'boolean') throw new TypeError('setBeatSync: enabled must be boolean');
		const st = deckStates[deck];
		st.beat_sync_enabled = enabled;
		if (!enabled) {
			_bumpReanchorOperation(deck);
			st.sync_error = null;
			// An explicit opt-out retires this deck's pending-follower records
			// too. Without this, a deck marked pending against a gridless
			// master kept that record after the DJ switched Beat Sync off, and
			// the master's later gridless settlement still processed it: with
			// no master elected (the master was paused first),
			// reconcileAfterBeatgridSettled's null-master branch abandons every
			// pending follower, which re-wrote a sync_error onto a deck that
			// had opted out, and flipped beat_sync_enabled back to false if the
			// DJ had since re-enabled it (discussion_r3919779327 P2 BLOCKING).
			// The record only ever existed to describe a wait this deck no
			// longer has, so opting out is exactly when it stops being true.
			_resyncTracking.clearPendingMembership(deck);
			return Promise.resolve();
		}
		// Same contract as setQuantize: the flag keeps the value the DJ chose,
		// the deck has nothing to lock to, and that is said out loud rather
		// than thrown into command_error.
		if (gridFeaturesInert(st)) {
			pushToast(`Deck ${deck} BEAT SYNC ${gridFeatureInertTip(st)}`, 'info');
			return Promise.resolve();
		}
		if (!_rt[deck].desiredActive) return Promise.resolve();
		const master = _syncClockMaster();
		if (master === null) {
			if (_masterMode === 'locked' && _ownedMaster() !== null) {
				return Promise.resolve();
			}
			_electPlayingMaster({ reason: 'beat-sync-enable' });
			const elected = _syncClockMaster();
			if (elected !== null && elected !== deck) {
				return _synchronizeFollowers(elected, [deck]).catch((error: unknown) => {
					st.beat_sync_enabled = false;
					const bounds = _tempoBounds(deck);
					const detail = error instanceof Error ? error.message : String(error);
					throw new Error(
						`cannot phase-lock within pitch [${bounds.min}, ${bounds.max}] (BAR): ${detail}`,
						{ cause: error instanceof Error ? error : undefined }
					);
				});
			}
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
		_requireLoaded(deck, 'syncKey');
		const masterDeck = _masterDeck;
		if (masterDeck === null) throw new Error('KEY SYNC requires an elected loaded master deck');
		if (masterDeck === deck) throw new Error('KEY SYNC cannot be applied to the selected master deck');
		_requireLoaded(masterDeck, 'KEY SYNC master');
		const deckManualShiftSemitones = _keySyncManualShiftBaseline(deck);
		await _setDeckKeyShift(deck, _keySyncPlan(deck, masterDeck, deckManualShiftSemitones).targetManualShiftSemitones);
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
		const wasMaster = _masterDeck === deck; // elect BEFORE reconciling below, same shape as _clearLoadedTrackState (r3912339497)
		// audible/playing flip BEFORE election, same shape again: the 2s replacement wait above BREAKS on its deadline, so a pause that never reached presentation leaves st.audible true on a deck whose processor is already detached and stopped. nextPlayingMaster picks the lowest audible id, so deck 1 would re-elect ITSELF here and _reconcileStrandedFollowers' master === deck branch would then drop every stranded follower instead of handing them to the deck that is actually audible (PR #765 'Exclude the unloading deck before master election').
		st.playing = false; st.audible = false;
		if (wasMaster) {
			if (_masterMode === 'locked') _masterMode = 'auto';
			_electPlayingMaster({ force: true, reason: 'unload' });
		}
		const stranded = _beatgridResyncPorts.takePending(deck); _resyncTracking.clearForDeck(deck); // stranded captured before clearForDeck, same race as r3912339491
		_beatgridGuards.beforeClear(deck, stranded, 'unload'); // scoped, not fire-unscoped (r3912960726)
		_rt[deck] = {
			..._emptyRuntime(),
			loadToken: rt.loadToken,
			nodes: rt.nodes,
			scheduleTail: Promise.resolve(),
			swapTail: Promise.resolve()
		};
		// The load generation is the ONE field an eject must not roll back. It
		// counts successful loads onto this slot for the life of the page, and
		// every consumer of it (the IPC snapshot, the e2e reload waits) reads
		// it as monotonic: "has a NEW load committed since the number I held?".
		// Resetting it to 0 here made that question unanswerable across the one
		// path that needs it most - a destructive replace, which unloads and
		// then loads, so a deck sitting on generation 1 went 1 -> 0 -> 1 and an
		// observer waiting for `> 1` waited for ever. Carried forward instead:
		// the slot is empty, but the count of loads it has served is history,
		// not state, and history does not un-happen.
		deckStates[deck] = { ..._emptyDeckState(deck), load_generation: st.load_generation };
		deckLoadErrors[deck] = null;
		recordPerfEvent('deck-unload', 'deck resources released', deck, 'info');
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

	async setDeckMaster(deck: DeckId, options?: { lock?: boolean }): Promise<void> {
		const { st } = _requireLoaded(deck, 'setDeckMaster');
		const previousMaster = _masterDeck;
		const previousMode = _masterMode;
		if (!st.audible) {
			if (options?.lock === false && _masterDeck === deck) {
				_masterMode = 'auto';
				if (!st.playing) _electPlayingMaster({ reason: 'unlock-reelect' });
				return;
			}
			const blockers = pausedMasterSelectionBlockers(deck, deckStates);
			assertPausedMasterSelectionAllowed(deck, st.audible, blockers);
			_assignMaster(deck, 'manual');
			if (options?.lock === true) _masterMode = 'locked';
			else if (options?.lock === false) {
				_masterMode = 'auto';
				if (!st.playing) _electPlayingMaster({ reason: 'unlock-reelect' });
			}
			return;
		}
		const followers = masterSwitchFollowers(deck, deckStates).filter((candidate) =>
			effectiveBeatSync(deckStates[candidate])
		);
		if (previousMaster !== deck) _bumpReanchorOperation(deck);
		_assignMaster(deck, 'manual');
		try {
			await _synchronizeFollowers(deck, followers, { reanchorDecks: new Set(followers) });
		} catch (error) {
			_assignMaster(previousMaster);
			_masterMode = previousMode;
			throw error;
		}
		if (options?.lock === true) {
			_masterMode = 'locked';
		} else if (options?.lock === false) {
			_masterMode = 'auto';
			if (!st.playing) _electPlayingMaster({ reason: 'unlock-reelect' });
		}
	}

	setStemMute(deck: DeckId, stem: StemControl, muted: boolean, pressT0Ms?: number): void {
		if (typeof muted !== 'boolean') throw new TypeError('setStemMute: muted must be boolean');
		applyStemControl(deck, stem, 'muted', muted, { requireLoaded: _requireLoaded, getChannel: (d) => mixerState.channels[d] });
		if (_ctx !== null) logMixerApply('stem-mute-apply', deck, pressT0Ms, _ctx.currentTime);
	}

	setStemSolo(deck: DeckId, stem: StemControl, solo: boolean, pressT0Ms?: number, exclusive = false): void {
		if (typeof solo !== 'boolean') throw new TypeError('setStemSolo: solo must be boolean');
		applyStemControl(deck, stem, 'solo', solo, { requireLoaded: _requireLoaded, getChannel: (d) => mixerState.channels[d], exclusive });
		if (_ctx !== null) logMixerApply('stem-solo-apply', deck, pressT0Ms, _ctx.currentTime);
	}

	setStemGain(deck: DeckId, stem: StemControl, value: number): void {
		assertUnitRange('setStemGain', value);
		applyStemControl(deck, stem, 'gain', value, {
			requireLoaded: _requireLoaded,
			getChannel: (d) => mixerState.channels[d]
		});
	}

	setStemEqMode(deck: DeckId, enabled: boolean): void {
		applyStemEqMode(deck, enabled, (d) => mixerState.channels[d]);
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
		return buildDeckAudioSnapshot({
			renderContextTimeS: _ctx.currentTime,
			presentationContextTimeS: presentationContextTime,
			sampleRateHz: _ctx.sampleRate,
			fftSize: analyser.fftSize,
			frequencyDb: finiteFrequencyDb,
			timeDomain: finiteTimeDomain
		});
	}

	setTrim(deck: DeckId, value: number): void {
		assertUnitRange('setTrim value', value);
		mixerState.channels[deck].trim = value;
		const nodes = _rt[deck].nodes;
		if (nodes !== null) _setParam(nodes.trim.gain, value * TRIM_MAX_GAIN);
		_maybeHandoffOnAir();
	}

	setEq(deck: DeckId, band: EqBand, value: number, pressT0Ms?: number): void {
		assertUnitRange('setEq value', value);
		const ch = mixerState.channels[deck], nodes = _rt[deck].nodes, db = eqDbFromKnob(value);
		if (band === 'low') ch.eq_low = value;
		else if (band === 'mid') ch.eq_mid = value;
		else if (band === 'high') ch.eq_high = value;
		else { const _exhaustive: never = band; throw new Error(`Unhandled EQ band: ${_exhaustive}`); }
		if (nodes === null) return;
		if (_ctx === null) throw new Error('audio graph not initialised');
		const now = _ctx.currentTime;
		const gain = band === 'low' ? nodes.low.gain : band === 'mid' ? nodes.mid.gain : nodes.high.gain;
		applyEqRamp(gain, db, now, PARAM_SMOOTH_S); logEqApply(deck, pressT0Ms, now, PARAM_SMOOTH_S);
	}

	setFilter(deck: DeckId, value: number, pressT0Ms?: number): void {
		assertUnitRange('setFilter value', value);
		mixerState.channels[deck].filter = value;
		const nodes = _rt[deck].nodes;
		if (nodes !== null) {
			const { lpHz, hpHz, dryGain, lpWetGain, hpWetGain } = filterParamsFromKnob(value);
			_setParam(nodes.filterLp.frequency, lpHz); _setParam(nodes.filterHp.frequency, hpHz);
			_setParam(nodes.filterDry.gain, dryGain); _setParam(nodes.filterLpWet.gain, lpWetGain);
			_setParam(nodes.filterHpWet.gain, hpWetGain);
		}
		if (_ctx !== null) logMixerApply('filter-apply', deck, pressT0Ms, _ctx.currentTime);
	}

	setFader(deck: DeckId, value: number, pressT0Ms?: number): void {
		assertUnitRange('setFader value', value);
		mixerState.channels[deck].fader = value;
		const nodes = _rt[deck].nodes;
		if (nodes !== null) _setParam(nodes.fader.gain, value);
		_maybeHandoffOnAir();
		if (_ctx !== null) logMixerApply('fader-apply', deck, pressT0Ms, _ctx.currentTime);
	}

	setCrossfader(value: number, pressT0Ms?: number): void {
		assertUnitRange('setCrossfader value', value);
		mixerState.crossfader = value;
		if (_ctx !== null) { _applyCrossfader(); logMixerApply('xfader-apply', null, pressT0Ms, _ctx.currentTime); }
		_maybeHandoffOnAir();
	}

	assignChannel(deck: DeckId, assign: CrossfaderAssign): void {
		if (assign !== 'A' && assign !== 'B' && assign !== 'THRU') {
			const _exhaustive: never = assign;
			throw new Error(`Unhandled crossfader assign: ${_exhaustive}`);
		}
		mixerState.channels[deck].assign = assign;
		if (_ctx !== null) _applyCrossfader();
		_maybeHandoffOnAir();
	}

	setChannelCue(deck: DeckId, enabled: boolean): void {
		if (typeof enabled !== 'boolean') throw new TypeError('channel cue enabled must be boolean');
		mixerState.channels[deck].cue_enabled = enabled;
		const nodes = _rt[deck].nodes;
		if (nodes !== null) _setParam(nodes.cue.gain, enabled ? 1 : 0);
	}

	setHeadphoneMix(value: number): void {
		assertUnitRange('setHeadphoneMix value', value);
		mixerState.headphones.mix = value;
		applyHeadphoneMix();
	}

	setHeadphoneLevel(value: number): void {
		assertUnitRange('setHeadphoneLevel value', value);
		mixerState.headphones.level = value;
		applyHeadphoneMix();
	}

	setHeadphoneOutputMode = setMonitorOutputMode;
	setHeadDelayMs = setMonitorHeadDelay;
	/** CUEOUT-14: the room delay line and how a measured offset is split. */
	setMasterDelayMs = setMonitorMasterDelay;
	setHeadphoneAlignmentMode = setMonitorAlignmentMode;
	refreshHeadphoneOutputs = (): Promise<void> => refreshMonitorOutputs(_monitorSource);
	/** Must be called from a visible user gesture. May briefly open the
	 * microphone to label output devices when selectAudioOutput is missing. */
	acquireHeadphoneOutput = (): Promise<void> => acquireMonitorOutput(_monitorSource);
	selectHeadphoneOutput = (deviceId: string): Promise<void> => selectMonitorOutput(deviceId, _monitorSource);
	selectMasterOutput = (deviceId: string): Promise<void> => selectMonitorMasterOutput(deviceId, _monitorSource);
	selectAudioInput = (deviceId: string): Promise<void> => selectMonitorAudioInput(deviceId);

	/** Topbar master-volume slider -> master GainNode (COMPONENT-MAP 1.1). */
	setMaster(value: number): void {
		assertUnitRange('setMaster value', value);
		mixerState.master = value;
		if (_masterGain !== null) _setParam(_masterGain.gain, value * _ceilingGainMultiplier());
	}

	/** #1475 M enforcement: primitives only, no prefs import here on purpose -
	 * audio-engine.svelte.ts is a hotspot file already at its fan-out ceiling,
	 * so the caller (Mixer.svelte, which already imports both this module and
	 * prefs.svelte) pushes the calibrated value in rather than this module
	 * pulling it. */
	setLevelCeiling(dbfs: number | null, enabled: boolean): void {
		_ceilingDbfs = dbfs;
		_ceilingEnabled = enabled;
		if (_masterGain !== null) {
			_setParam(_masterGain.gain, mixerState.master * _ceilingGainMultiplier());
		}
	}

	/** RESCUE-02: batch resume with one shared schedule instant for every deck. */
	async rescueResumeTogether(
		plans: ReadonlyArray<{ deck: DeckId; positionSec: number }>
	): Promise<void> {
		if (plans.length === 0) return;
		const ctx = await _resumeContext();
		let sharedWhen = ctx.currentTime;
		for (const plan of plans) {
			const minimum = safeTransportScheduleTime(ctx.currentTime, _transportLeadSec(plan.deck));
			sharedWhen = Math.max(sharedWhen, minimum);
		}
		await Promise.all(
			plans.map((plan) => _scheduleDeck(plan.deck, sharedWhen, plan.positionSec, true))
		);
	}

	/** RESCUE-02 Undo: stop every rescued deck at one shared schedule instant. */
	async rescueStopAllTogether(decks: readonly DeckId[]): Promise<void> {
		if (decks.length === 0) return;
		const ctx = await _resumeContext();
		let sharedWhen = ctx.currentTime;
		for (const deck of decks) {
			const minimum = safeTransportScheduleTime(ctx.currentTime, _transportLeadSec(deck));
			sharedWhen = Math.max(sharedWhen, minimum);
		}
		await Promise.all(
			decks.map((deck) =>
				_scheduleDeck(
					deck,
					sharedWhen,
					(effectiveWhen) => _projectPositionAt(deck, effectiveWhen),
					false
				)
			)
		);
	}
}

/** The singleton engine every /performance unit imports. */
export const engine: RbAudioEngine = new RbAudioEngine();

/** PERFMODE-14: whether the Gig deck graph is still armed. */
export function gigDeckGraphIsPresent(): boolean {
	return _ctx !== null;
}

export function getMasterMode(): MasterMode {
	return _masterMode;
}

export function getMasterReason(): MasterReason {
	return _masterReason;
}

export type { MasterMode, MasterReason };
