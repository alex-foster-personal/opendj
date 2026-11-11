// MOD-ZONE: ORANGE | (private path)
/**
 * Typed, fail-fast browser command surface for /performance.
 *
 * UI controls and browser automation both dispatch through the same command
 * function. Runtime validation rejects malformed IPC messages, while command
 * failures are recorded in reactive state and shown by each deck.
 *
 * Requirements:
 *   ✔︎ Independent deck queues prevent one slow load from blocking another.
 *     [if] deck 1 load is pending [then] deck 2 load starts independently
 *   ✔︎ Sync-sensitive transport uses one explicit coordination scope.
 *     [if] master/follower commands overlap [then] their engine writes serialize
 *   ✔︎ Continuous mixer controls execute immediately and round-trip in query().
 *     [if] an agent moves EQ [then] the visible UI and IPC snapshot both update
 *   ✔︎ Preset phases form one barrier across every deck and sync coordination.
 *     [if] a preset is claimed while deck work is active [then] it waits for
 *       every prior scope and no later command can overlap it
 *   ✔︎ ✅ 🎯 Route command sessions invalidate queued work before audio teardown.
 *     [if] load/play commands are queued when IPC uninstalls [then] they reject
 *       before invoking the engine or recreating an off-route audio graph ⛔️
 *     [if] a new route session starts while old work settles [then] its commands
 *       use fresh scheduler tails and clean pending counters
 *   ✔︎ ✅ 🎯 hot_cue_save works for every loaded deck, mapped or not (CUES-01;
 *     supersedes the #736 mapping gate). [if] a browser/CLI agent dispatches
 *       hot_cue_save for an unmapped deck [then] it reaches saveHotCue ⛔️
 *   ✔︎ ✅ 🎯 hot_cue_trigger honours BeatSyncMax on a playing, unlooped deck (#884).
 *     [if] BeatSyncMax is on, the deck is playing and unlooped [then] the jump
 *       arms for the deck's own next downbeat instead of firing immediately,
 *       and the wait is readable via query().decks[deck].hot_cue_armed ⛔️
 *     [if] the deck is stopped, or has an engaged loop, or BeatSyncMax is off
 *       [then] the jump fires immediately - a stopped deck has no audible
 *       transition to protect, and an engaged loop already owns its window
 *   ✔︎ DECKUX-4985 empty-deck play/cue refuse at the dispatcher (Sentry OPEN-DJ-BE-6Y).
 *     [if] play or cue is dispatched while stable_id is null [then] IPC throws
 *       'Deck N is empty - load a track before play|cueing' before engine.play /
 *       pressCue, and the toast is warn not a Sentry error [else stop]
 *     [if] pause is dispatched on an empty deck [then] it is a no-op [else stop]
 *   ✔︎ LATENCY-02 QUANTIZED LAUNCH is opt-in via play.quantize / Cmd+Space / Cmd+click.
 *     [if] Cmd+Space or Cmd+click on play starts a paused loaded deck while a
 *       playing master has a trusted beatgrid [then] playback is scheduled at the
 *       next point where this deck's beat 1 aligns with the master's beat 1, and
 *       the play control shows an armed/waiting countdown until that instant ⛔️
 *     [if] Space or a plain play click, or a play command with no quantize flag,
 *       starts a paused loaded deck [then] playback uses the Class A immediate path ⛔️
 *     [if] the AGENT-03 play command carries quantize: true [then] it takes the same
 *       QUANTIZED LAUNCH path as Cmd+Space, including the armed countdown in query() ⛔️
 *     [if] a QUANTIZED LAUNCH cannot be planned [then] the command refuses with a named
 *       error that includes QUANTIZED LAUNCH and the play button does not stay busy ⛔️
 */

import { _parseCommand, _deck, _validatedPressStamp } from './performance-ipc-guards';
import {
	controlShellWindow,
	type WindowAction,
	type WindowLabel
} from '$lib/shell/native-shell';
import { postVerdict, type Verdict } from '$lib/rb/autoplay-why';
import {
	COMPATIBLE_KEY_SHIFT_MAX,
	KEY_NUDGE_STEPS,
	isKeyNudgeStep,
	isKeySyncMode,
	readKeyNudgeStep,
	writeKeyNudgeStep,
	type KeyLadderRow,
	type KeyNudgeStep,
	type KeySyncMode,
	type KeySyncStatus
} from '$lib/player/key/camelot';
import { installAutomaticMasterElectionRunner } from '$lib/rb/automatic-handoff-runners';
import {
	copyToast,
	dismissToast,
	holdToast,
	pushToast,
	releaseToast,
	toasts,
	toastTimerArmed
} from '$lib/stores.svelte';
import { pairingBeatAt } from '$lib/rb/pairing-readiness';
import { clearHotCue, restoreHotCue, saveHotCue, vocalsOf, type Vocals } from '$lib/rb/api-rb';
import {
	deckCloudDownloads,
	ensureLocalBeforeLoad,
	type DeckCloudDownload
} from '$lib/offload/offload-ops.svelte';
import {
	analysisSourceState,
	installAnalysisSourceRefreshRunner,
	reinstallAcrossHotUpdates,
	setAnalysisSource,
	type AnalysisSource,
	type AnalysisSourceFeature
} from '$lib/rb/analysis-source.svelte';
import { capturePairing } from '$lib/rb/pairing-capture';
import {
	autoCueTargetMs,
	hasPhaseCapableBeatGrid,
	hasTrustedBeatGrid
} from '$lib/player/grid-features';
import {
	nextDownbeatAtOrAfter,
	planHotCueTrigger,
	quantizeToNearestDownbeat,
	type ArmAtPosition
} from '$lib/rb/beat-sync-math';
import { planWaveformSeek, type WaveformSeekSnap } from '$lib/rb/plan-waveform-seek';
import { bootScheduler } from '$lib/rb/boot-scheduler';
import {
	DECK_IDS,
	deckEffectiveBpm,
	deckTransportClock,
	engine,
	getDeckState,
	getMasterMode,
	getMasterReason,
	installAutomaticRejoinRunner,
	presentationTickFailure,
	installScopedSyncRunner,
	drillGraphRebuild,
	isMasterMuted,
	keyShiftOptions,
	keySyncPreview,
	keySyncStatus,
	mixerState,
	pitchRanges,
	reattachingDecks,
	setMasterMuted,
	setAutoGainLiveGuard,
	deckAutoGain,
	assertDeckLoadAllowed,
	DeckReplacementBlockedError,
	commitDueQuantizedLaunches,
	deckAudioClockPositionMs,
	type DeckTransportClock,
	type PitchRange
} from '$lib/rb/audio-engine.svelte';
import { manualLoopIn, manualLoopOut, manualReloop, type ManualLoopPort } from '$lib/rb/manual-loop-commands';
import {
	createGridNudgeCoalescer,
	parseGridEditIntent,
	runGridEditCommand,
	type GridEditIntent
} from '$lib/rb/grid-edit-command';
import { executeInRustEngine, initRustMode, rustMode } from '$lib/audio-engine/rust-mode.svelte';
import {
	DECK_MOVE_DEFAULT_CROSSFADE_BEATS,
	DECK_MOVE_MAX_CROSSFADE_BEATS,
	moveDeck,
	type DeckMoveMode
} from '$lib/rb/deck-move';
import { installDeckMoveRetireRunner, webAudioDeckMovePort } from '$lib/rb/deck-move-port.svelte';
import { onPerformanceArmedContextReset } from '$lib/rb/performance-armed-context-reset';
import { deckFacingMessage, reportFoldedLaunch } from '$lib/rb/deck-load-context';
import { DECK_LOAD_ERROR_TOAST_MS, reportDeckLoadCommandFailure } from '$lib/rb/deck-load-context';
import type { MasterMode, MasterReason } from '$lib/rb/audio-engine-types';
import { readTransition } from './transition-read.svelte';
import {
	allDecks,
	deckCommandSource,
	withDeckCommandSource,
	type CommandSource
} from './deck-stop-log';
import { replayDispatchGate, replayDispatchTakenOver } from './set-replay-takeover.svelte';
import { captureStemCommand } from '$lib/sets/control-event-recorder';
import {
	assertLoopGridBase,
	loopIntervalChoices,
	loopIntervalView,
	setLoopIntervalBase,
	setLoopIntervalMode
} from '$lib/rb/loop-interval-view.svelte';
import { beatJumpSizes, setBeatJumpSize } from '$lib/rb/beat-jump-size.svelte';
import {
	ObservedCommandScheduler,
	ScopedCommandInvalidatedError,
	scopesOutsideHold,
	type ScopeHold
} from '$lib/rb/performance-command-scheduler';
import type { WidenScope } from '$lib/player/scoped-sync-runner';
import {
	markArmedHotCuePress,
	pendingLoadPlayState,
	setPendingLoadPlayIntent,
	type DeckId
} from '$lib/rb/deck-slots';
import {
	setAutoCue,
	setAutoPlayEnabled,
	setLibraryPanelCollapsed,
	setShowStems,
	setWaveEqReflect,
	setMixerStems,
	setAutoPlayEarly,
	setAutoPlayTwoDeck,
	setPerfView,
	setShowPads,
	setWaveformDesign,
	setWaveHeight,
	setDeckNumberArt,
	setShowSections,
	setShowVocalLane,
	setLyricsWaveformOverlay,
	setWaveGridLines,
	setLyricsHoverScrub,
	setJogRadialWaveform,
	setLayoutOverride,
	setUiSkin,
	setWavePalette,
	setWaveSplitMaster,
	setWaveZoom,
	setWaveZoomLinked,
	setWaveZoomPlayhead,
	stepWaveZoomPref,
	deckWaveZoom,
	parseWavePlayheadMode,
	parseWaveZoom,
	type WavePlayheadMode,
	type WaveZoom,
	PERF_VIEWS,
	isPerfView,
	type LibraryPanel,
	type PerfView,
	enableBeatSyncAfterLoad,
	installBeatSyncMaxEnabler,
	patchRelatedCriteria,
	resetRelatedCriteria,
	uiPrefs
} from '$lib/rb/prefs.svelte';
import { isLibraryPanelsCollapsed, setLibraryPanelsCollapsed } from '$lib/rb/library-panels.svelte';
import {
	parseWaveformDesignPref,
	type WaveformDesign,
	type WaveformDesignPref
} from '$lib/rb/waveform-design';
import { layoutView, resetLayout, setLayoutSize } from '$lib/rb/layout-regions';
import {
	effectiveWaveformDesign,
	effectiveWavePalette,
	parseSkinSettings,
	type SkinSettings
} from '$lib/rb/ui-skin';
import {
	effectiveLibraryLayout,
	effectivePerformanceLayout,
	parseLayoutPreset,
	parseLayoutSurface,
	type LayoutOverride,
	type LayoutSurface,
	type LibraryLayout,
	type PerformanceLayout
} from '$lib/rb/skin-layout';
import { copyDeckAudioSnapshot } from '$lib/rb/deck-audio-snapshot';
import type {
	AnlzBeat,
	AnlzBeatgridSource,
	DeckAudioSnapshot,
	DeckState,
	HotCue,
	HotCueSlot,
	LoopMemory,
	LoopState,
	QuantizeGrid,
	SafetyLoopSlot,
	SyncMode
} from '$lib/rb/deck-state-types';
import type {
	CrossfaderAssign,
	EqBand,
	HeadphoneAlignmentMode,
	HeadphoneOutputMode,
	HeadphoneState,
	MixerChannelState
} from '$lib/rb/mixer-types';
import type { StemControl, StemDeckState } from '$lib/rb/stem-types';
import {
	type StemSeparationSnapshot,
	noteStemsLoadedOnceSettled,
	separateLoadedDeck,
	stemLoadRun,
	stemSeparationSnapshot
} from '$lib/rb/stem-separation-port';
import { abortCueAlignment, startCueAlignment } from '$lib/rb/cue-align-session.svelte';
import type { SortKey } from '$lib/components/rb/browser/browser-sort-ipc';
import {
	MUTED_MASTER_VOLUME,
	type PerformancePresetPhase
} from '$lib/rb/performance-preset-constants';
import { reloadResume, rescueRestoreStatus } from '$lib/rb/performance-rescue-restore.svelte';
import { deckLoadErrors } from '$lib/player/state.svelte';
import { onDeckLoadStart } from '$lib/rb/mixer-selection.svelte';
import { requestGenreInferOnAttention } from '$lib/rb/genre-infer-on-attention';
import {
	closeTrackInfo,
	openTrackInfoForDeck,
	openTrackInfoForRow,
	toggleTrackInfo,
	trackInfoSnapshot,
	type TrackInfoDeck
} from '$lib/rb/track-info-state.svelte';
import { toggleOjSidebar } from '$lib/assistant/oj-sidebar.svelte';
import {
	TRACK_LOAD_RESET_IDS,
	applyTrackLoadChannelReset,
	readTrackLoadReset,
	resetNowPrefs,
	stemResetActions,
	writeTrackLoadReset,
	type TrackLoadResetId,
	type TrackLoadResetPrefs
} from '$lib/rb/track-load-reset';
export { uiPrefs, getDeckState };
import { notifyRescueTransportEvent } from '$lib/rb/rescue-ring-writer.svelte';
export {
	installRescueRingWriterHooks,
	uninstallRescueRingWriterHooks
} from '$lib/rb/rescue-ring-writer.svelte';
import { noteRecentDeck } from '$lib/rb/recent-deck';
import { TempoCoalescer } from '$lib/rb/tempo-coalesce';
import {
	hoveredEdgeList,
	isEqRaised,
	isOptRevealActive,
	isPeeking,
	isTechModeActive,
	setEdgeHovered,
	setEqRaised,
	setOptReveal,
	setPeeking,
	toggleTechMode,
	type EdgeRegion
} from '$lib/rb/technically-working.svelte';
import { waveformStutterSnapshot } from '$lib/rb/audio-health.svelte';
import {
	performanceFeedbackSummary,
	recordPerformanceFeedback
} from '$lib/rb/performance-feedback';
import {
	previewCacheStats,
	previewCue,
	previewCueSeek,
	silencePreviewCue,
	stopPreviewCue
} from '$lib/player/preview-cue.svelte';
import { midiTakeoverUi, setMidiTakeoverMode } from '$lib/rb/midi/takeover-ui.svelte';
import type { MidiTakeoverMode } from '$lib/rb/midi/takeover-policy';
import { planColorFxSwitch } from '$lib/rb/color-fx';

import type {
	AutoPlayNextController,
	CmdkAdapter,
	CmdkSnapshot,
	CoordinatedHold,
	HeadphoneCommand,
	PairingSnapshot,
	PerformanceBrowserAdapter,
	PerformanceBrowserIpc,
	PerformanceBrowserPaneSnapshot,
	PerformanceCommand,
	PerformanceDeckSnapshot,
	PerformanceHotCueDriver,
	PerformancePresetLifecyclePhase,
	PerformancePresetLifecycleSnapshot,
	PerformancePresetTransactionDriver,
	PerformanceQuantizedLaunchDriver,
	PerformanceState,
	PerformanceUiCommandResult,
	PersistenceScope,
	PlaylistHistoryAdapter,
	ToastIpcRow,
	CommandScope
} from './performance-ipc-types';

export type {
	HeadphoneCommand,
	PerformanceCommand,
	PerformanceDeckSnapshot,
	PerformanceState,
	PairingSnapshot,
	PerformanceBrowserPaneSnapshot,
	PerformanceBrowserAdapter,
	AutoPlayNextController,
	PlaylistHistoryAdapter,
	CmdkSnapshot,
	CmdkAdapter,
	PerformancePresetLifecyclePhase,
	PerformancePresetLifecycleSnapshot,
	PerformancePresetTransactionDriver,
	PerformanceBrowserIpc,
	ToastIpcRow,
	PerformanceQuantizedLaunchDriver,
	PerformanceHotCueDriver,
	CoordinatedHold,
	PerformanceUiCommandResult,
	CommandScope,
	PersistenceScope
} from './performance-ipc-types';

import {
	applyColorFxSettings,
	colorFx,
	colorFxView,
	type ColorFxDeckView
} from '$lib/rb/color-fx.svelte';

let _autoPlayNextController: AutoPlayNextController | null = null;

export function registerAutoPlayNextController(controller: AutoPlayNextController): () => void {
	_autoPlayNextController = controller;
	return () => {
		if (_autoPlayNextController === controller) _autoPlayNextController = null;
	};
}

let _playlistHistoryAdapter: PlaylistHistoryAdapter | null = null;

let _cmdkAdapter: CmdkAdapter | null = null;
/** The mounted bar, or the reason a cmdk_* command cannot run without it. */
function _cmdk(type: string): CmdkAdapter {
	if (_cmdkAdapter === null)
		throw new Error(`${type} requires the Cmd K v2 bar (flag cmdk.v2) on /performance`);
	return _cmdkAdapter;
}

/** Loads and unloads ever dispatched per deck, from any source (CMDK-16). Plain, not reactive: read at a point in time. */
const _deckLoadDispatches: Record<DeckId, number> = { 1: 0, 2: 0, 3: 0, 4: 0 };

/** How many loads or unloads have been dispatched to `deck` so far, committed or not. */
export function deckLoadDispatchCount(deck: DeckId): number {
	return _deckLoadDispatches[deck];
}

/** Fader writes ever dispatched per deck, from any source (CMDK-19). Plain, not reactive: read at a point in time. */
const _deckFaderWrites: Record<DeckId, number> = { 1: 0, 2: 0, 3: 0, 4: 0 };

/**
 * How many fader writes have been dispatched to `deck` so far, counted before
 * they run: a write of the same value, or one still awaiting the engine, is
 * newer than any read of the fader taken before it.
 */
export function deckFaderWriteCount(deck: DeckId): number {
	return _deckFaderWrites[deck];
}

/**
 * Deck-queue commands ever dispatched per deck other than loads, unloads and beat_sync (CMDK-19):
 * play, cue, seek, tempo, loops and the rest that claim the deck's own scope. Counted when sent,
 * before they wait or run, so one queued behind a load is already newer than a read taken before
 * it. beat_sync is left out because a load under Beat Sync Max queues its own after the load
 * (DECKUX-37), and an aligned pair turns it off as its first step anyway.
 */
const _deckCommandDispatches: Record<DeckId, number> = { 1: 0, 2: 0, 3: 0, 4: 0 };
/** beat_sync commands ever dispatched per deck, counted apart so a reader can date them itself (CMDK-19). */
const _deckBeatSyncDispatches: Record<DeckId, number> = { 1: 0, 2: 0, 3: 0, 4: 0 };

/**
 * How many such commands have been dispatched to `deck` so far, run or not; with `'beat_sync'`,
 * how many beat_sync commands instead, so an aligned pair can see one sent after its own
 * (Codex r4277769141).
 */
export function deckCommandDispatchCount(deck: DeckId, kind?: 'beat_sync'): number {
	return kind === 'beat_sync' ? _deckBeatSyncDispatches[deck] : _deckCommandDispatches[deck];
}

export function registerCmdkAdapter(adapter: CmdkAdapter): () => void {
	_cmdkAdapter = adapter;
	return () => {
		if (_cmdkAdapter === adapter) _cmdkAdapter = null;
	};
}

export function registerPlaylistHistoryAdapter(adapter: PlaylistHistoryAdapter): () => void {
	_playlistHistoryAdapter = adapter;
	return () => {
		if (_playlistHistoryAdapter === adapter) _playlistHistoryAdapter = null;
	};
}

let _browserAdapter: PerformanceBrowserAdapter | null = null;
let _activeBrowserPlaylist: string | null = null;
let _commandSequence = 0;
let _commandHistory: Array<{ id: string; type: PerformanceCommand['type'] }> = [];
let _deckCommandIds: Record<DeckId, string | null> = { 1: null, 2: null, 3: null, 4: null };
let _pairingSnapshot: PairingSnapshot | null = $state(null);

/** Labels of the extra shell windows this page has opened and not closed,
 * reported as `PerformanceState.windows`. Focus leaves the set unchanged. */
let _openShellWindows: WindowLabel[] = $state([]);

/** Narrow test seam for exercising queryPerformanceState()'s pairing_snapshot
 * clone directly, without driving the full pairing_snapshot_open/save command
 * sequence. Production always reaches _pairingSnapshot through those
 * commands, which is where the Svelte $state reactive proxy wrapping this
 * module's test bundler cannot reproduce (see performance-ipc.test.mjs). */
export function installPairingSnapshotForTest(snapshot: PairingSnapshot | null): () => void {
	const previous = _pairingSnapshot;
	_pairingSnapshot = snapshot;
	return () => {
		_pairingSnapshot = previous;
	};
}

export function registerPerformanceBrowserAdapter(adapter: PerformanceBrowserAdapter): () => void {
	if (_browserAdapter !== null)
		throw new Error('performance browser adapter is already registered');
	_browserAdapter = adapter;
	return () => {
		if (_browserAdapter !== adapter)
			throw new Error('performance browser adapter ownership changed');
		_browserAdapter = null;
	};
}

const _EMPTY_BROWSER_PANE_SNAPSHOT: PerformanceBrowserPaneSnapshot = {
	search: null,
	sort: null,
	selected_row: null,
	load: null,
	related_active: false
};

function _readBrowserPaneSnapshot(): PerformanceBrowserPaneSnapshot {
	if (_browserAdapter === null) return _EMPTY_BROWSER_PANE_SNAPSHOT;
	const snapshot = _browserAdapter.readSnapshot();
	return {
		search: snapshot.search,
		sort:
			snapshot.sort === null
				? null
				: { key: snapshot.sort.key, direction: snapshot.sort.direction },
		selected_row: snapshot.selected_row,
		load: snapshot.load === null ? null : { ...snapshot.load },
		related_active: snapshot.related_active
	};
}

function _recordPerformanceCommand(command: PerformanceCommand): void {
	const event = { id: `pc-${++_commandSequence}`, type: command.type };
	_commandHistory = [..._commandHistory.slice(-199), event];
	const deck = _commandDeck(command);
	if (deck !== null) _deckCommandIds[deck] = event.id;
}

export const performanceCommandStatus: {
	last_error: string | null;
	deck_errors: Record<DeckId, string | null>;
	deck_pending: Record<DeckId, number>;
	active: number;
	queued: number;
} = $state({
	last_error: null,
	deck_errors: { 1: null, 2: null, 3: null, 4: null },
	deck_pending: { 1: 0, 2: 0, 3: 0, 4: 0 },
	active: 0,
	queued: 0
});

export const performancePresetLifecycle: PerformancePresetLifecycleSnapshot = $state({
	id: null,
	phase: 'idle',
	active: false,
	error: null
});

const hotCueReversals: Record<
	DeckId,
	{ slot: HotCueSlot; revision: string; reversal_id: string } | null
> = $state({ 1: null, 2: null, 3: null, 4: null });

/** #884: raw armed record. `remaining_ms` is deliberately NOT stored here -
 * it is derived live from `target_context_time` at query() time, the same
 * "recompute from the presentation clock, never cache a countdown" pattern
 * `deckTransportClock` uses, so it can never go stale between queries. */
const hotCueArmed: Record<
	DeckId,
	{ slot: HotCueSlot; target_position_ms: number; target_context_time: number } | null
> = $state({ 1: null, 2: null, 3: null, 4: null });

const waveformSeekArmed: Record<
	DeckId,
	{ target_position_ms: number; target_context_time: number } | null
> = $state({ 1: null, 2: null, 3: null, 4: null });

const quantizedLaunchArmed: Record<DeckId, { launch_at_context_sec: number } | null> = $state({
	1: null,
	2: null,
	3: null,
	4: null
});

const _defaultQuantizedLaunchDriver: PerformanceQuantizedLaunchDriver = {
	arm: (deck, pressT0Ms) => engine.armQuantizedLaunch(deck, pressT0Ms),
	clear: (deck) => engine.clearQuantizedLaunch(deck),
	contextTimeNowSec: () => engine.contextTimeNowSec()
};
let _quantizedLaunchDriver: PerformanceQuantizedLaunchDriver = _defaultQuantizedLaunchDriver;

export function installPerformanceQuantizedLaunchDriverForTest(
	driver: PerformanceQuantizedLaunchDriver
): () => void {
	const previous = _quantizedLaunchDriver;
	_quantizedLaunchDriver = driver;
	return () => {
		_quantizedLaunchDriver = previous;
	};
}

export function resetQuantizedLaunchArmedForTest(): void {
	for (const deckId of DECK_IDS) quantizedLaunchArmed[deckId] = null;
}

/** AGENT-30: a scheduled step's state as the engine owns it (waiting, landed, cancelled), or null if never scheduled. */
export function performanceStepState(
	stepId: string
): 'waiting' | 'landed' | 'cancelled' | 'lost' | null {
	return engine.stepState(stepId);
}

/** AGENT-30: the plan runner has retired a finished step, so the engine may drop its state. */
export function performanceForgetStep(stepId: string): void {
	engine.forgetStep(stepId);
}

/** AGENT-30: a deck's playhead with the AudioContext time it was presented at (one observation), or null. */
/** Launches that came due while no tab was painting: see commitDueQuantizedLaunches. */
export function commitPerformanceLaunches(): void {
	commitDueQuantizedLaunches();
}

export function performancePositionAnchor(
	deckId: DeckId
): { position_ms: number; context_sec: number; pitch: number } | null {
	return engine.positionAnchor(deckId);
}

/** The engine's AudioContext clock: the time base `at_context_sec` is written in. */
export function performanceContextTimeNowSec(): number {
	return _quantizedLaunchDriver.contextTimeNowSec();
}

/** DECKUX-10478: the engine surface manual LOOP IN / OUT / RELOOP drive (manual-loop-commands.ts). */
const _manualLoopPort: ManualLoopPort = {
	deckState: getDeckState,
	playheadMs: deckAudioClockPositionMs,
	setLoop: (deck, loop, quantize) => engine.setLoop(deck, loop, quantize),
	jumpIntoLoop: (deck, loop, op) => engine.jumpIntoLoop(deck, loop, op)
};

const _defaultHotCueDriver: PerformanceHotCueDriver = {
	stableId: (deck) => getDeckState(deck).stable_id,
	refresh: (deck) => engine.refreshHotCues(deck),
	triggerState: (deck, slot) => {
		const state = getDeckState(deck);
		return {
			cue: state.hot_cues.find((cue) => cue.slot === slot) ?? null,
			playing: state.playing,
			loopEngaged: state.loop !== null && state.loop.engaged,
			positionSec: state.position_ms / 1000,
			beats: state.anlz?.beatgrid.beats ?? []
		};
	},
	jump: (deck, positionMs, pressT0Ms) => engine.quantizedSeek(deck, positionMs, undefined, pressT0Ms),
	engageLoop: (deck, loop) => engine.setLoop(deck, loop, false),
	arm: (deck, positionMs, armAt, pressT0Ms) => engine.armHotCueTrigger(deck, positionMs, armAt, pressT0Ms),
	contextTimeNowSec: () => engine.contextTimeNowSec()
};
let _hotCueDriver: PerformanceHotCueDriver = _defaultHotCueDriver;

/** HOTCUE-LOOP-01 (rekordbox/CDJ, mimicked): a loop hot cue's range, or null
 * for a point cue. A loop hot cue jumps to its start on the quantize grid at
 * once, then engages that same loop; it is never armed for a later downbeat. */
function _loopHotCueRange(cue: HotCue): { in_ms: number; out_ms: number } | null {
	return cue.is_loop && cue.out_ms !== null && cue.out_ms > cue.in_ms
		? { in_ms: cue.in_ms, out_ms: cue.out_ms }
		: null;
}

/** Rust engine mode (NAE-13) drives hot cues through its own driver
 * (`rustHotCueDriver`); Web Audio uses the engine-owned one above. */
export function installPerformanceHotCueDriver(driver: PerformanceHotCueDriver): void {
	_hotCueDriver = driver;
}

/** Narrow test seam for exercising the public IPC command protocol without
 * initializing Web Audio. */
export function installPerformanceHotCueDriverForTest(driver: PerformanceHotCueDriver): () => void {
	const previous = _hotCueDriver;
	_hotCueDriver = driver;
	return () => {
		_hotCueDriver = previous;
	};
}

let _presetClaim: { id: string } | null = null;
// AUTOGAIN: Settings and event-driven auto gain refreshes change live gain
// outside this dispatcher, so they ask the same lock first.
const _drainHeldAutoGain = setAutoGainLiveGuard(() =>
	_presetClaim === null ? null : `performance preset ${_presetClaim.id} owns controls`
);
type _GridEditCommand = Extract<PerformanceCommand, { type: 'grid_edit' }>;
// GRIDEDIT-6235: held-button repeats merge before the persistence scope wait.
const _gridNudges = createGridNudgeCoalescer<_GridEditCommand, PerformanceState>((scope) =>
	typeof scope === 'string' && scope.startsWith('persistence-')
		? Number(scope.slice('persistence-'.length))
		: null
);
const _commandScheduler = new ObservedCommandScheduler<CommandScope>(
	(scopes) => _gridNudges.cut(scopes),
	() => _gridNudges.clear()
);
// S1 (round 2): a queued fader step that a newer one has overtaken is a no-op
// when its turn comes, so a sweep costs one group re-lock, not one per message.
// A command queued between two tempos is a barrier (tempo-coalesce.ts).
const _tempoCoalescer = new TempoCoalescer<DeckId | null, CommandScope>();
// PARITY-10: the only module that owns the scoped command scheduler, so a
// beatgrid-landed resync fired long after its load() command released [deck]
// reclaims scope here rather than racing whatever now holds it. The
// settlement itself claims ONLY [_deck] (r3913693383 P1 BLOCKING: an earlier
// design reserved every other deck plus 'sync' up front on every settlement,
// even the common narrow one that never touches them - rejected as an
// unacceptable latency/independence cost, not merely a trade-off). `widen`
// decides fresh, from inside the granted [_deck] claim (r3913350814 P1:
// never decided before scheduling), whether this settlement needs the wide
// barrier PERFORMANCE_PRESET_COMMAND_SCOPES below uses for the same
// coordinate-many-decks reason (headphone deliberately excluded: this resync
// never touches headphone routing) PLUS [_deck] itself (r3914267990 P1
// BLOCKING: excluding it left a gap between this narrow claim releasing
// [_deck] and the wide claim's own registration, where a fresh load/unload
// on the same deck could race the still-in-flight wide reconciliation -
// including [_deck] in the wide claim's scopes makes the wide claim the
// tail map's new occupant of it, so a later command on this deck correctly
// queues behind the wide work instead of interleaving with it). `widen`
// fires this as a SEPARATE top-level `_commandScheduler.run` and returns
// that promise to the caller WITHOUT this settlement's own claim awaiting or
// returning it (r3913492572 P1 BLOCKING: awaiting a nested wide claim from
// inside an already-granted claim can deadlock against a successor,
// submitted in the interim, that shares [_deck] - that successor captures
// this claim's tail as ITS predecessor, so this claim later depending on
// the wide claim too, once the successor has taken 'sync' first, closes a
// cycle). Because [_deck] is now also a wide scope, the wide claim's own
// predecessors (computed before its registration overwrites the tail map)
// include this claim's tail too - an acyclic, ordinary "wait for whoever
// currently holds [_deck]" dependency, safe precisely because this claim
// never waits on the wide claim back. The caller (audio-engine.svelte.ts)
// must attach its own `.catch()` directly to what `widen` returns instead of
// returning it as this settlement's own result - see
// `_resyncAfterBeatgridUpgrade` and the two `reconcileBeforeClear` call
// sites. A settlement
// audio-engine.svelte.ts's resyncSettlementNeedsFullBarrier proves, freshly,
// can only ever touch its own deck (r3913096141 P1: the common
// gridless-with-no-master-or-followers case) never calls `widen` at all, so
// it never touches the other decks' or 'sync' scopes in any way.
installScopedSyncRunner((_deck, run) => {
	const statusGeneration = _commandStatusGeneration;
	let started = false;
	performanceCommandStatus.queued += 1;
	performanceCommandStatus.deck_pending[_deck] += 1;
	const widen: WidenScope = (work) => {
		let wideStarted = false;
		if (statusGeneration === _commandStatusGeneration) {
			performanceCommandStatus.queued += 1;
			for (const deck of DECK_IDS) performanceCommandStatus.deck_pending[deck] += 1;
		}
		const wide = _commandScheduler.run([...DECK_IDS, 'sync'], async () => {
			wideStarted = true;
			if (statusGeneration === _commandStatusGeneration) {
				performanceCommandStatus.queued -= 1;
				performanceCommandStatus.active += 1;
			}
			try {
				return await work();
			} finally {
				if (statusGeneration === _commandStatusGeneration) {
					performanceCommandStatus.active -= 1;
				}
			}
		});
		return wide.finally(() => {
			if (statusGeneration === _commandStatusGeneration) {
				if (!wideStarted) performanceCommandStatus.queued -= 1;
				for (const deck of DECK_IDS) performanceCommandStatus.deck_pending[deck] -= 1;
			}
		});
	};
	return _commandScheduler
		.run([_deck], async () => {
			started = true;
			if (statusGeneration === _commandStatusGeneration) {
				performanceCommandStatus.queued -= 1;
				performanceCommandStatus.active += 1;
			}
			try {
				return await run(widen);
			} finally {
				if (statusGeneration === _commandStatusGeneration) {
					performanceCommandStatus.active -= 1;
					performanceCommandStatus.deck_pending[_deck] -= 1;
				}
			}
		})
		.finally(() => {
			if (!started && statusGeneration === _commandStatusGeneration) {
				performanceCommandStatus.queued -= 1;
				performanceCommandStatus.deck_pending[_deck] -= 1;
			}
		});
});
// PARITY-02: the poll in AnalysisSourceToggle.svelte adopts an agent's direct
// PUT with no command of its own, so its deck/cache refresh needs the same
// all-deck-plus-sync claim the `analysis_source` command takes. Same scopes,
// so a poll-detected switch queues behind PREPARE/START and every deck
// mutation instead of replacing grids underneath them
// (discussion_r3968214009 P1 BLOCKING). Installed rather than imported
// because analysis-source.svelte.ts is imported FROM here.
// A dev hot update re-runs this module while analysis-source keeps its
// runner; reinstallAcrossHotUpdates releases the old one first (no-op in prod).
reinstallAcrossHotUpdates(import.meta.hot, 'analysisSourceRefreshRunner', () =>
	installAnalysisSourceRefreshRunner((work, scopes) =>
		_commandScheduler.run(scopes === undefined ? [...DECK_IDS, 'sync'] : [...scopes], work)
	)
);
// An automatic master handoff (unload, pause, natural end) re-joins the
// followers under the same wide claim, queued behind the command that moved the
// master, so later deck commands wait for it rather than racing it.
// Counted like a widened claim, so a caller waiting for the queue to drain
// waits for the re-join too.
installAutomaticRejoinRunner((work) => {
	const statusGeneration = _commandStatusGeneration;
	const counted = () => statusGeneration === _commandStatusGeneration;
	let started = false;
	if (counted()) {
		performanceCommandStatus.queued += 1;
		for (const deck of DECK_IDS) performanceCommandStatus.deck_pending[deck] += 1;
	}
	return _commandScheduler
		.run([...DECK_IDS, 'sync'], async () => {
			started = true;
			if (counted()) {
				performanceCommandStatus.queued -= 1;
				performanceCommandStatus.active += 1;
			}
			try {
				await work();
			} finally {
				if (counted()) performanceCommandStatus.active -= 1;
			}
		})
		.finally(() => {
			if (!counted()) return;
			if (!started) performanceCommandStatus.queued -= 1;
			for (const deck of DECK_IDS) performanceCommandStatus.deck_pending[deck] -= 1;
		});
});
installAutomaticMasterElectionRunner((work) => _commandScheduler.run([...DECK_IDS, 'sync'], work));

/** Serialize a native-engine master/follower mutation with every deck and sync command. */
export function runSyncScopedWork<T>(work: () => Promise<T>): Promise<T> {
	return _commandScheduler.run([...DECK_IDS, 'sync'], work);
}

// PRRT_kwDOSEvNd860mzuu: post-fade retire waits for [from, to, sync] idle together and
// reserves none of them while waiting, so a slow load on either deck never holds sync
// against other decks' transport.
installDeckMoveRetireRunner((from, to, work) =>
	_commandScheduler.runWhenAllIdle([from, to, 'sync'], work)
);
let _commandGeneration = 0;
let _commandStatusGeneration = 0;
let _activeCommandSession: { generation: number } | null = null;
export const PERFORMANCE_PRESET_COMMAND_SCOPES: readonly CommandScope[] = [
	...DECK_IDS,
	...DECK_IDS.map(_persistenceScope),
	'sync',
	'headphone'
];

export const PERFORMANCE_RESCUE_COMMAND_SCOPES: readonly CommandScope[] =
	PERFORMANCE_PRESET_COMMAND_SCOPES;

let _rescueRestoredDecks: DeckId[] = [];

export function rescueRestoreLocksControls(): boolean {
	return rescueRestoreStatus.phase === 'restoring' || rescueRestoreStatus.phase === 'resuming';
}

export function setRescueRestorePhaseForTest(phase: typeof rescueRestoreStatus.phase): void {
	rescueRestoreStatus.phase = phase;
}

declare global {
	interface Window {
		musicDjToolsPerformance?: PerformanceBrowserIpc;
	}
}

/** Browser globals used by installPerformanceBrowserIpc and unit tests that set globalThis.window. */
function _performanceIpcHosts(): (Window & typeof globalThis)[] {
	const hosts: (Window & typeof globalThis)[] = [];
	const seen = new Set<object>();
	const add = (candidate: unknown): void => {
		if (candidate === null || candidate === undefined || typeof candidate !== 'object') return;
		if (seen.has(candidate)) return;
		seen.add(candidate);
		hosts.push(candidate as Window & typeof globalThis);
	};
	add(globalThis.window);
	if (typeof window !== 'undefined') add(window);
	if (hosts.length === 0) {
		throw new Error('performance IPC requires a browser window');
	}
	return hosts;
}

// --------------------------------------------------------------- state query

interface _BeatgridProjection {
	beatgrid: PerformanceDeckSnapshot['beatgrid'];
	beatgrid_ms: PerformanceDeckSnapshot['beatgrid_ms'];
}

type _AnlzPayload = NonNullable<DeckState['anlz']>;

/**
 * Per-deck memo of the beatgrid projections, keyed by ANLZ object identity.
 *
 * queryPerformanceState() runs after EVERY command, including the null-scope
 * continuous ones - trim, EQ, faders, crossfader - which fire once per
 * pointermove. It rebuilt all four decks' beatgrids each time: two full array
 * maps per deck, one of them allocating an object per beat. Measured at 3.9ms
 * p50 and 5.4ms worst with four decks loaded (2221 beats), i.e. 42-65% of the
 * main thread at a 120Hz pointer rate, spent rebuilding values that only change
 * on load. It never delayed the audio - that is a different bug, already fixed -
 * but it is what makes a knob drag feel heavy, and it gets worse as decks fill.
 *
 * Identity is the right key precisely because the engine REPLACES st.anlz
 * (`st.anlz = fresh`) rather than mutating it, so a reload or a hot-cue refresh
 * invalidates this for free and a stale grid cannot outlive its track.
 *
 * The key is held WEAKLY (PERFMODE-14). A strong `{anlz, ...}` slot only let go
 * of a payload on the NEXT query, and Library mode disposes the engine and then
 * never queries again, so it kept all four decks' ANLZ alive for the whole
 * Library session: waveform detail as reactive proxies (34 MB of JS heap on
 * silver, Sat 26 Sep 2026) plus the full-track band images that
 * wave/render.ts keys weakly on that same waveform. Per-deck maps keep decks
 * from ever sharing a projection, even when two decks carry one payload.
 */
const _beatgridProjections: Record<DeckId, WeakMap<_AnlzPayload, _BeatgridProjection>> = {
	1: new WeakMap(),
	2: new WeakMap(),
	3: new WeakMap(),
	4: new WeakMap()
};

const _EMPTY_BEATGRID_PROJECTION: _BeatgridProjection = {
	beatgrid: _freezeRows([]),
	beatgrid_ms: _freezeRows([])
};

/** Deep-freeze the projection. The arrays are now SHARED across every snapshot
 * instead of rebuilt, so a consumer mutating one would poison every later read;
 * frozen, that attempt throws in strict mode instead of corrupting the cache
 * silently. These are immutable analysis values - nothing has cause to write
 * them - and the IPC payload is unchanged, since it crosses the boundary by
 * structured clone or JSON. */
function _freezeRows<T>(rows: T[]): T[] {
	for (const row of rows) Object.freeze(row);
	Object.freeze(rows);
	return rows;
}

function _beatgridProjection(deckId: DeckId, deck: DeckState): _BeatgridProjection {
	const anlz = deck.anlz;
	if (anlz === null) return _EMPTY_BEATGRID_PROJECTION;
	const memo = _beatgridProjections[deckId];
	const cached = memo.get(anlz);
	if (cached !== undefined) return cached;
	const beats = anlz.beatgrid.beats;
	const fresh: _BeatgridProjection = {
		beatgrid: _freezeRows(
			beats.map((beat) => ({ n: beat.n, bpm: beat.bpm, time_ms: beat.t * 1000 }))
		),
		beatgrid_ms: _freezeRows(beats.map((beat) => beat.t * 1000))
	};
	memo.set(anlz, fresh);
	return fresh;
}

/** Live-derive `remaining_ms` from the AudioContext clock rather than
 * trusting a cached countdown - the same "recompute, don't cache" rule
 * `deckTransportClock` follows. A landed schedule reads as null.
 *
 * These snapshots are READ paths: queryPerformanceState() runs inside
 * `$derived` (WaveRow.svelte, Deck.svelte), and Svelte throws
 * `state_unsafe_mutation` on any `$state` write from there. Queries must
 * not mutate reactive state while a consumer derives or renders the snapshot.
 * An expired record is reported as null and left for the command paths and
 * session lifecycle owners (the next arm, unload, session teardown, or an
 * audio-graph rebuild that replaces the AudioContext) to clear or replace. */
function _remainingMsUntilContextTime(
	targetContextSec: number,
	readContextSec: () => number
): number | null {
	try {
		return (targetContextSec - readContextSec()) * 1000;
	} catch {
		// Graph rebuild/teardown can land between an armed record and the next read.
		return null;
	}
}

function _waveformSeekArmedSnapshot(
	deckId: DeckId
): { target_position_ms: number; remaining_ms: number } | null {
	const armed = waveformSeekArmed[deckId];
	if (armed === null) return null;
	const remainingMs = _remainingMsUntilContextTime(armed.target_context_time, () =>
		_hotCueDriver.contextTimeNowSec()
	);
	if (remainingMs === null || remainingMs <= 0) return null;
	return { target_position_ms: armed.target_position_ms, remaining_ms: remainingMs };
}

function _hotCueArmedSnapshot(
	deckId: DeckId
): { slot: HotCueSlot; target_position_ms: number; remaining_ms: number } | null {
	const armed = hotCueArmed[deckId];
	if (armed === null) return null;
	const remainingMs = _remainingMsUntilContextTime(armed.target_context_time, () =>
		_hotCueDriver.contextTimeNowSec()
	);
	if (remainingMs === null || remainingMs <= 0) return null;
	return {
		slot: armed.slot,
		target_position_ms: armed.target_position_ms,
		remaining_ms: remainingMs
	};
}

function _quantizedLaunchArmedSnapshot(
	deckId: DeckId
): { remaining_ms: number; launch_at_context_sec: number } | null {
	const armed = quantizedLaunchArmed[deckId];
	if (armed === null) return null;
	const remainingMs = _remainingMsUntilContextTime(armed.launch_at_context_sec, () =>
		_quantizedLaunchDriver.contextTimeNowSec()
	);
	if (remainingMs === null || remainingMs <= 0) return null;
	return { remaining_ms: remainingMs, launch_at_context_sec: armed.launch_at_context_sec };
}

// ------------------------------------------- narrow reads for UI $derived

/*
 * PERF-GRID-03: a component that needs ONE field must not `$derived` the whole
 * `queryPerformanceState()`. That snapshot reads every deck's position, so the
 * derived re-ran on every transport tick and rebuilt four deck snapshots plus
 * the transition read each time. Measured on demon-llama with two synced
 * decks playing: 61% of main-thread time after the grid memo landed. These
 * accessors return exactly what the matching snapshot field holds and track
 * only what that field depends on.
 *
 * The two armed countdowns are clock-derived (`remaining_ms`, expiry read as
 * null), so while one is armed it also tracks the decks' positions: it then
 * ticks and expires exactly as the full snapshot did, and costs nothing while
 * nothing is armed.
 */

function _trackTransportTicks(): void {
	for (const deckId of DECK_IDS) void getDeckState(deckId).position_ms;
}

/** Same value as `queryPerformanceState().master_mode`. */
export function queryMasterMode(): MasterMode {
	return getMasterMode();
}

/** Same value as `queryPerformanceState().decks[deckId].waveform_seek_armed`. */
export function queryWaveformSeekArmed(
	deckId: DeckId
): { target_position_ms: number; remaining_ms: number } | null {
	if (waveformSeekArmed[deckId] === null) return null;
	_trackTransportTicks();
	return _waveformSeekArmedSnapshot(deckId);
}

/** Same value as `queryPerformanceState().decks[deckId].quantized_launch_armed`. */
export function queryQuantizedLaunchArmed(
	deckId: DeckId
): { remaining_ms: number; launch_at_context_sec: number } | null {
	if (quantizedLaunchArmed[deckId] === null) return null;
	_trackTransportTicks();
	return _quantizedLaunchArmedSnapshot(deckId);
}

function _openPairingSnapshot(): PairingSnapshot {
	const loaded = DECK_IDS.flatMap((deckId) => {
		const deck = getDeckState(deckId);
		if (deck.stable_id === null) return [];
		// A playhead before the first beat (a deck parked at 0:00) is in the
		// lead-in, not gridless; pairingBeatAt counts back to a beat-in-bar.
		// Null only when the grid has no beats at all (pairing-readiness.ts).
		const positionBeat = pairingBeatAt(deck.anlz?.beatgrid.beats ?? [], deck.position_ms);
		return [{ deckId, deck, positionBeat }];
	});
	// Beat timestamps only when EVERY loaded deck has one. An empty grid used
	// to throw here and leave the sheet unopenable; it now captures every deck
	// in real time instead, and the snapshot says so (beat_sync_max false =
	// time units), so nothing is faked. The top-bar button is disabled for an
	// empty grid; a lead-in playhead still records a beat.
	const beats = uiPrefs.beat_sync_max && loaded.every((item) => item.positionBeat !== null);
	const unit: 'beats' | 'time' = beats ? 'beats' : 'time';
	const decks = loaded.map(({ deckId, deck, positionBeat }) => {
		const channel = mixerState.channels[deckId];
		const timestampValue = beats && positionBeat !== null ? positionBeat : deck.position_ms;
		const adjustments: Array<{ band: EqBand; value: number }> = [
			{ band: 'low', value: channel.eq_low },
			{ band: 'mid', value: channel.eq_mid },
			{ band: 'high', value: channel.eq_high }
		];
		const eq_adjusts = adjustments.filter((adjust) => adjust.value !== 0.5);
		return {
			deck_id: deckId,
			stable_id: deck.stable_id as string,
			title: deck.title ?? (deck.stable_id as string),
			position_ms: deck.position_ms,
			timestamp: { unit, value: timestampValue },
			eq_adjusts
		};
	});
	return { version: 1, beat_sync_max: beats, decks };
}

function _vocalsSnapshot(anlz: ReturnType<typeof getDeckState>['anlz']): Vocals | null {
	if (anlz === null) return null;
	if ((anlz as { vocals?: unknown }).vocals === undefined) return null;
	const vocals = vocalsOf(anlz);
	if (vocals.status === 'not_analyzed') return { status: 'not_analyzed' };
	return {
		status: vocals.status,
		fps: vocals.fps,
		regions: vocals.regions.map((region) => ({ ...region }))
	};
}

function _deckSnapshot(deckId: DeckId): PerformanceDeckSnapshot {
	const deck = getDeckState(deckId);
	const beatgrid = _beatgridProjection(deckId, deck);
	return {
		deck_id: deckId,
		stable_id: deck.stable_id,
		source_path: deck.source_path,
		has_rb_mapping: deck.has_rb_mapping,
		title: deck.title,
		artist: deck.artist,
		bpm: deck.bpm,
		key: deck.key,
		key_shift_semitones: deck.key_shift_semitones,
		key_sync_preview: (() => {
			const preview = keySyncPreview(deckId);
			return preview === null
				? null
				: {
						master_deck: preview.masterDeck,
						target_manual_shift_semitones: preview.targetManualShiftSemitones,
						delta_semitones: preview.deltaSemitones
					};
		})(),
		effective_bpm: deckEffectiveBpm(deckId),
		duration_ms: deck.duration_ms,
		position_ms: deck.position_ms,
		playing: deck.playing,
		audible: deck.audible,
		transport_pending: deck.transport_pending,
		transport_clock: deckTransportClock(deckId),
		cue_ms: deck.cue_ms,
		pitch: deck.pitch,
		pitch_range: pitchRanges[deckId],
		quantize_enabled: deck.quantize_enabled,
		quantize_grid_beats: deck.quantize_grid_beats,
		beat_jump_size_beats: beatJumpSizes[deckId],
		beat_sync_enabled: deck.beat_sync_enabled,
		key_sync_enabled: deck.key_sync_enabled,
		key_sync_status: keySyncStatus(deckId),
		key_sync_mode: deck.key_sync_mode,
		key_sync_target: deck.key_sync_target ?? 'master',
		key_options: Object.fromEntries(
			DECK_IDS.filter((target) => target !== deckId)
				.map((target) => [`${target}`, keyShiftOptions(deckId, target)] as const)
				.filter((entry) => entry[1] !== null)
		),
		master_tempo_enabled: deck.master_tempo_enabled,
		slip_enabled: deck.slip_enabled,
		slip_active: deck.slip_active,
		slip_position_ms: deck.slip_position_ms,
		is_master: deck.is_master,
		sync_mode: deck.sync_mode,
		sync_error: deck.sync_error,
		processor_error: deck.processor_error,
		presentation_clock_error: presentationTickFailure(),
		last_load_latency_ms: deck.last_load_latency_ms,
		load_generation: deck.load_generation,
		last_load_stages: deck.last_load_stages === null ? null : { ...deck.last_load_stages },
		stems: {
			...deck.stems,
			alignment: deck.stems.alignment === null ? null : { ...deck.stems.alignment },
			controls: {
				vocal: { ...deck.stems.controls.vocal },
				instrumental: { ...deck.stems.controls.instrumental },
				drums: { ...deck.stems.controls.drums },
				bass: { ...deck.stems.controls.bass },
				other: { ...deck.stems.controls.other }
			}
		},
		stem_separation: stemSeparationSnapshot(deckId, deck.stable_id),
		loop: deck.loop === null ? null : { ...deck.loop },
		safety_loop: deck.safety_loop === null ? null : { ...deck.safety_loop },
		pending_loop_in_ms: deck.pending_loop_in_ms,
		last_loop: deck.last_loop === null ? null : { ...deck.last_loop },
		loop_interval: {
			grid_mode: loopIntervalView[deckId].gridMode,
			grid_base: loopIntervalView[deckId].gridBase,
			choices: loopIntervalChoices(loopIntervalView[deckId].gridBase)
		},
		beatgrid: beatgrid.beatgrid,
		beatgrid_ms: beatgrid.beatgrid_ms,
		transition_in_ms: deck.anlz?.cues?.find((cue) => cue.kind === 'transition_in')?.in_ms ?? null,
		phrases:
			deck.anlz?.phrases?.map((phrase) => ({
				start_ms: phrase.start_s * 1000,
				end_ms: phrase.end_s * 1000,
				kind: phrase.kind,
				mood: phrase.mood
			})) ?? [],
		vocals: _vocalsSnapshot(deck.anlz),
		hot_cue_slots: (['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'] as HotCueSlot[]).map((slot) => ({
			slot,
			cue: deck.hot_cues.find((cue) => cue.slot === slot) ?? null,
			revision: deck.hot_cue_revisions[slot]
		})),
		hot_cue_reversal: hotCueReversals[deckId],
		hot_cue_armed: _hotCueArmedSnapshot(deckId),
		waveform_seek_armed: _waveformSeekArmedSnapshot(deckId),
		quantized_launch_armed: _quantizedLaunchArmedSnapshot(deckId),
		loop_engaged: deck.loop !== null && deck.loop.engaged,
		command_error: performanceCommandStatus.deck_errors[deckId],
		command_pending: performanceCommandStatus.deck_pending[deckId] > 0,
		last_command_id: _deckCommandIds[deckId]
	};
}

export function queryPerformanceState(): PerformanceState {
	const masterDecks = DECK_IDS.filter((deck) => getDeckState(deck).is_master);
	if (masterDecks.length > 1) {
		throw new Error(`engine contract violation: ${masterDecks.length} master decks selected`);
	}
	const previewStats = previewCacheStats();
	return {
		version: 1,
		midi_takeover: { mode: midiTakeoverUi.mode },
		master_deck: masterDecks[0] ?? null,
		master_mode: getMasterMode(),
		master_reason: getMasterReason(),
		transition: readTransition(),
		command_pending: performanceCommandStatus.active > 0 || performanceCommandStatus.queued > 0,
		command_queued: performanceCommandStatus.queued,
		load_play_intent: Object.fromEntries(
			Object.entries(pendingLoadPlayState()).map(([deck, pending]) => [
				deck,
				pending === null
					? null
					: { generation: pending.generation, desired_play: pending.desiredPlay }
			])
		) as Record<DeckId, { generation: number; desired_play: boolean } | null>,
		cloud_downloads: deckCloudDownloads(),
		auto_gain: $state.snapshot(deckAutoGain),
		decks: {
			1: _deckSnapshot(1),
			2: _deckSnapshot(2),
			3: _deckSnapshot(3),
			4: _deckSnapshot(4)
		},
		mixer: {
			crossfader: mixerState.crossfader,
			master: mixerState.master,
			headphones: {
				...mixerState.headphones,
				calibration: {
					...mixerState.headphones.calibration,
					diagnostics: {
						...mixerState.headphones.calibration.diagnostics,
						master_measurements_ms: [
							...mixerState.headphones.calibration.diagnostics.master_measurements_ms
						],
						cue_measurements_ms: [
							...mixerState.headphones.calibration.diagnostics.cue_measurements_ms
						]
					}
				},
				signals: {
					master: { ...mixerState.headphones.signals.master },
					cue: { ...mixerState.headphones.signals.cue },
					input: { ...mixerState.headphones.signals.input }
				},
				routes: {
					master: { ...mixerState.headphones.routes.master },
					cue: { ...mixerState.headphones.routes.cue },
					booth: { ...mixerState.headphones.routes.booth }
				},
				outputs: mixerState.headphones.outputs.map((output) => ({ ...output })),
				inputs: mixerState.headphones.inputs.map((input) => ({ ...input })),
				device_access: {
					...mixerState.headphones.device_access,
					notices: [...mixerState.headphones.device_access.notices]
				}
			},
			channels: {
				1: { ...mixerState.channels[1] },
				2: { ...mixerState.channels[2] },
				3: { ...mixerState.channels[3] },
				4: { ...mixerState.channels[4] }
			}
		},
		master: { muted: isMasterMuted() },
		browser: { active_playlist: _activeBrowserPlaylist, ..._readBrowserPaneSnapshot() },
		history: _commandHistory.map((event) => ({ ...event })),
		preset: { ...performancePresetLifecycle },
		rescue_restore: {
			phase: rescueRestoreStatus.phase,
			playing_deck_ids: [...rescueRestoreStatus.playing_deck_ids],
			per_deck: { ...rescueRestoreStatus.per_deck },
			started_at_ms: rescueRestoreStatus.started_at_ms
		},
		reload_resume:
			reloadResume.offer === null
				? null
				: { decks: [...reloadResume.offer.decks], error: reloadResume.error },
		preview: {
			stable_id: previewCue.stable_id,
			playing: previewCue.playing,
			position_ms: previewCue.position_ms,
			duration_ms: previewCue.duration_ms,
			route: previewCue.route,
			rate: previewCue.rate,
			snap:
				previewCue.snap === null
					? null
					: {
							master_beat_number: previewCue.snap.masterBeatNumber,
							beat_phase: previewCue.snap.beatPhase,
							offset_ms: previewCue.snap.offsetSec * 1000,
							clamped: previewCue.snap.clamped,
							tempo_folded: previewCue.snap.tempoNormalization !== 1
						},
			snap_refusal: previewCue.snap_refusal,
			cache_tracks: previewStats.tracks,
			cache_bytes: previewStats.bytes,
			cache_budget_bytes: previewStats.budget_bytes
		},
		last_error: performanceCommandStatus.last_error,
		// _pairingSnapshot is a $state variable, so Svelte hands back a reactive
		// Proxy wrapping the assigned object - and a Proxy, regardless of what
		// it wraps, is never structured-cloneable (DataCloneError). Every other
		// field here is rebuilt fresh with a spread/map, which is naturally
		// plain; this one instead fed the live proxy straight into
		// structuredClone(). $state.snapshot() deep-unwraps it back to plain
		// data first, matching the Svelte 5 idiom for "give me a cloneable copy
		// of reactive state" - see performance-ipc.test.mjs.
		pairing_snapshot:
			_pairingSnapshot === null ? null : structuredClone($state.snapshot(_pairingSnapshot)),
		cmdk: _cmdkAdapter === null ? null : structuredClone($state.snapshot(_cmdkAdapter.snapshot())),
		windows: [..._openShellWindows],
		technically_working: {
			active: isTechModeActive(),
			peeking: isPeeking(),
			opt_reveal_active: isOptRevealActive(),
			eq_raised: isEqRaised(),
			hovered_edges: hoveredEdgeList()
		},
		track_info: trackInfoSnapshot(_trackInfoDecks()),
		waveform_stutter: { ...waveformStutterSnapshot() },
		library_panels: {
			collapsed: isLibraryPanelsCollapsed(),
			next_collapsed: uiPrefs.next_panel_collapsed,
			recommended_collapsed: uiPrefs.recommended_panel_collapsed
		},
		// Spread, not the live rune: this snapshot is structuredClone'd across
		// the IPC boundary and a $state Proxy is never cloneable.
		analysis_source: { ...analysisSourceState.features },
		// Spread for the same reason as the line above: analysisSourceState is a
		// $state rune, so handing the live Proxy out breaks structuredClone for
		// every agent reading this snapshot over IPC.
		analysis_source_decks: { ...analysisSourceState.deckFeatures },
		feedback_marks: performanceFeedbackSummary(),
		color_fx: colorFxView(),
		layout: layoutView(),
		ui: {
			show_stems: uiPrefs.show_stems,
			mixer_stems: uiPrefs.mixer_stems,
			library_related_criteria: { ...uiPrefs.library_related_criteria },
			auto_play_enabled: uiPrefs.auto_play_enabled,
			deck_autocue: uiPrefs.deck_autocue,
			auto_play_two_deck: uiPrefs.auto_play_two_deck,
			perf_view: uiPrefs.perf_view,
			show_pads: uiPrefs.show_pads,
			track_load_reset: { ...readTrackLoadReset() },
			key_nudge_step: readKeyNudgeStep(),
			deck_number_art: uiPrefs.deck_number_art,
			waveform_design: uiPrefs.waveform_design,
			wave_height: uiPrefs.wave_height,
			waveform_design_effective: effectiveWaveformDesign(uiPrefs.waveform_design, uiPrefs.ui_skin),
			ui_skin: uiPrefs.ui_skin,
			wave_palette: uiPrefs.wave_palette,
			wave_palette_effective: effectiveWavePalette(uiPrefs.wave_palette, uiPrefs.ui_skin),
			wave_split_master: uiPrefs.wave_split_master,
			layout_override: { ...uiPrefs.layout_override },
			layout: {
				performance: effectivePerformanceLayout(uiPrefs.layout_override.performance, uiPrefs.ui_skin),
				library: effectiveLibraryLayout(uiPrefs.layout_override.library, uiPrefs.ui_skin)
			},
			wave_eq_reflect: uiPrefs.wave_eq_reflect,
			wave_zoom: uiPrefs.wave_zoom,
			wave_zoom_decks: {
				'1': deckWaveZoom(uiPrefs, 1),
				'2': deckWaveZoom(uiPrefs, 2),
				'3': deckWaveZoom(uiPrefs, 3),
				'4': deckWaveZoom(uiPrefs, 4)
			},
			wave_zoom_playhead: uiPrefs.wave_zoom_playhead,
			wave_zoom_linked: uiPrefs.wave_zoom_linked,
			waveform_overlays: {
				sections: uiPrefs.show_sections,
				lyrics: uiPrefs.lyrics_waveform_overlay,
				lyrics_scrub: uiPrefs.lyrics_hover_scrub,
				jog_radial: uiPrefs.jog_radial_waveform,
				vocal_lane: uiPrefs.show_vocal_lane
			},
			wave_grid_lines: uiPrefs.wave_grid_lines
		}
	};
}

function _feedbackMark(vote: 'bad' | 'good' | 'great') {
	return {
		recorded_at_ms: Date.now(),
		vote,
		decks: DECK_IDS.map((deckId) => {
			const deck = getDeckState(deckId);
			return {
				deck_id: deckId,
				stable_id: deck.stable_id,
				playing: deck.playing,
				audible: deck.audible,
				position_ms: deck.position_ms,
				loop: deck.loop === null ? null : { ...deck.loop }
			};
		}),
		mixer: {
			crossfader: mixerState.crossfader,
			master: mixerState.master,
			channels: DECK_IDS.map((deckId) => ({ ...mixerState.channels[deckId] }))
		}
	};
}

// ----------------------------------------------------------- command routing

/**
 * DECKGAP-7691h: Reset now. The channel half is the on-load reset itself
 * (`applyTrackLoadChannelReset`), its writers sent as `fader` / `trim` / `eq` /
 * `filter` commands, as the Rust load path sends them; the stem half returns
 * mute, solo and stem level to what a new track publishes. Each write is an
 * ordinary command through this dispatcher, so both engines take it.
 */
async function _resetDeckNow(deck: DeckId, enabled: TrackLoadResetPrefs): Promise<void> {
	const commands: PerformanceCommand[] = [];
	applyTrackLoadChannelReset(mixerState.channels[deck], {
		setFader: (value) => commands.push({ type: 'fader', deck, value }),
		setTrim: (value) => commands.push({ type: 'trim', deck, value }),
		setFilter: (value) => commands.push({ type: 'filter', deck, value }),
		setEq: (band, value) => commands.push({ type: 'eq', deck, band, value })
	}, enabled);
	const stems = getDeckState(deck).stems;
	if (stems.status === 'ready') {
		for (const action of stemResetActions(enabled, stems.controls, stems.available_controls)) {
			if (action.kind === 'stem_solo')
				commands.push({ type: 'stem_solo', deck, stem: action.stem, solo: action.solo });
			else if (action.kind === 'stem_mute')
				commands.push({ type: 'stem_mute', deck, stem: action.stem, muted: action.muted });
			else if (action.kind === 'stem_gain')
				commands.push({ type: 'stem_gain', deck, stem: action.stem, value: action.value });
			else {
				const _exhaustive: never = action;
				throw new Error(`Unhandled stem reset: ${String(_exhaustive)}`);
			}
		}
	}
	for (const next of commands) await dispatchPerformanceCommand(next);
}

function _commandDeck(command: PerformanceCommand): DeckId | null {
	return 'deck' in command ? command.deck : null;
}

function _completeCommand(command: PerformanceCommand): PerformanceState {
	notifyRescueTransportEvent(command);
	return queryPerformanceState();
}

/** The track and grid a queued grid edit was pressed for, captured at dispatch. */
interface _GridEditTarget {
	stable_id: string | null;
	load_generation: number;
	source: AnlzBeatgridSource | null;
}
const _gridEditTargets = new WeakMap<PerformanceCommand, _GridEditTarget>();

function _sameGridEditTarget(a: PerformanceCommand, b: PerformanceCommand): boolean {
	const x = _gridEditTargets.get(a);
	const y = _gridEditTargets.get(b);
	return (
		x !== undefined &&
		y !== undefined &&
		x.stable_id === y.stable_id &&
		x.load_generation === y.load_generation &&
		x.source === y.source
	);
}

function _bindGridEditTarget(command: PerformanceCommand): void {
	if (command.type !== 'grid_edit') return;
	const st = getDeckState(command.deck);
	_gridEditTargets.set(command, {
		stable_id: st.stable_id,
		load_generation: st.load_generation,
		source: st.anlz?.beatgrid.source ?? null
	});
}

/** Neither a load nor an analysis-source switch takes the persistence scope,
 * so by the time a queued edit runs the deck may hold another track or draw
 * the other grid; never edit either (each source keeps its own edit). */
function _assertGridEditTarget(
	command: Extract<PerformanceCommand, { type: 'grid_edit' }>,
	st: {
		stable_id: string | null;
		load_generation: number;
		anlz: { beatgrid: { source: AnlzBeatgridSource } } | null;
	}
): void {
	const target = _gridEditTargets.get(command);
	if (target === undefined) return;
	if (st.stable_id !== target.stable_id || st.load_generation !== target.load_generation) {
		throw new Error(
			`grid edit: deck ${command.deck} loaded another track before the edit ran; nothing was written`
		);
	}
	if ((st.anlz?.beatgrid.source ?? null) !== target.source) {
		throw new Error(
			`grid edit: deck ${command.deck} switched beatgrid source before the edit ran; nothing was written`
		);
	}
}

function _persistenceScope(deck: DeckId): PersistenceScope {
	return `persistence-${deck}`;
}

export function performanceCommandQueueScopes(
	command: PerformanceCommand
): readonly CommandScope[] | null {
	if (command.type === 'rescue_resume' || command.type === 'rescue_stop_all') {
		return PERFORMANCE_RESCUE_COMMAND_SCOPES;
	}
	if (
		command.type === 'headphone_outputs_refresh' ||
		command.type === 'headphone_output_acquire' ||
		command.type === 'headphone_output_select' ||
		command.type === 'headphone_master_select' ||
		command.type === 'headphone_booth_select' ||
		command.type === 'headphone_input_select' ||
		command.type === 'output_mode' ||
		// CUEOUT-14: a calibration owns the monitor graph while it chirps, so the
		// delay writes wait behind it rather than moving the nodes it is verifying.
		command.type === 'headphone_calibrate' ||
		command.type === 'head_delay_ms' ||
		command.type === 'headphone_alignment_mode' ||
		command.type === 'master_delay_ms'
	) {
		return ['headphone'];
	}
	if (command.type === 'analysis_source') return [...DECK_IDS, 'sync'];
	// DECKMOVE-01: both decks and the master election (the master follows the audio).
	// The fade wait stays outside this claim (moveDeck returns after scheduleHandover); post-fade retire is runWhenAllIdle([from, to, 'sync'], ...).
	if (command.type === 'deck_move') return [command.from, command.to, 'sync'];
	const deck = _commandDeck(command);
	// AUTOGAIN-04: an awaited server write then a trim-stage apply. It holds
	// its deck's scope for the whole await, so a preset's all-scope barrier
	// waits for it instead of overlapping it.
	if (command.type === 'channel_cue' || command.type === 'auto_gain_offset') {
		if (deck === null) throw new Error(`${command.type} has no deck command queue scope`);
		return [deck];
	}
	// prettier-ignore
	if (
		command.type === 'trim' ||
		command.type === 'eq' ||
		command.type === 'filter' ||
		command.type === 'fader' ||
		command.type === 'stem_mute' ||
		command.type === 'stem_solo' ||
		// Same drag as EQ: a stem-mode HI/MID/LOW turn must not wait behind
		// this deck's transport queue or the needle never follows the pointer.
		command.type === 'stem_gain' ||
		command.type === 'stem_eq_mode' ||
		// STEM-47: landing stems on a playing deck waits for the transport to
		// be idle; in the deck's queue it would BE the thing keeping it busy.
		command.type === 'stem_load' ||
		// STEM-77: an HTTP enqueue with no engine write; in the deck's queue
		// the POST would hold up that deck's live controls.
		command.type === 'stem_separate' ||
		command.type === 'assign' ||
		command.type === 'crossfader' ||
		command.type === 'master_volume' ||
		command.type === 'master_mute' ||
		command.type === 'browser_select_playlist' ||
		command.type === 'browser_related' ||
		command.type === 'browser_related_criteria' ||
		command.type === 'browser_related_criteria_reset' ||
		command.type === 'midi_takeover_mode' ||
		command.type === 'headphone_mix' ||
		command.type === 'headphone_level' ||
		command.type === 'headphone_booth_level' ||
		// CUEOUT-14: the abort must never queue behind the calibration it stops.
		command.type === 'headphone_calibrate_abort' ||
		// CUEOUT-15: the preview owns no deck, so serializing it behind one
		// would make a library click wait on a transport it cannot touch.
		command.type === 'preview_cue' ||
		command.type === 'preview_stop' ||
		command.type === 'library_panels' ||
		command.type === 'library_panels_toggle' ||
		command.type === 'show_stems' ||
		command.type === 'wave_eq_reflect' ||
		command.type === 'mixer_stems' ||
		command.type === 'autoplay' ||
		// TOPVIEW: the two-track AutoPlay toggle is a preference write with no deck.
		command.type === 'auto_play_two_track' ||
		command.type === 'auto_play_early' ||
		// TOPVIEW: the view icons and the PAD button are preference writes with no deck.
		command.type === 'set_perf_view' ||
		command.type === 'show_pads' ||
		command.type === 'track_load_reset' ||
		command.type === 'key_nudge_step' ||
		command.type === 'set_waveform_design' ||
		command.type === 'set_deck_number_art' ||
		command.type === 'set_waveform_overlay' ||
		command.type === 'set_skin' ||
		command.type === 'set_layout_preset' ||
		// DL-2: layout preference writes with no deck.
		command.type === 'set_layout_size' ||
		command.type === 'reset_layout' ||
		command.type === 'set_wave_zoom' ||
		command.type === 'wave_zoom_step' ||
		command.type === 'set_wave_zoom_options' ||
		// View state only: no engine write to serialize, so queueing these
		// behind a deck's command scope would stall a control that cannot
		// conflict with anything.
		command.type === 'loop_interval_mode' ||
		command.type === 'loop_interval_base' ||
		command.type === 'beat_jump_size' ||
		// LIBUX-05 overlay chrome: no deck, no engine write, nothing to
		// serialize against.
		command.type === 'tech_mode_toggle' ||
		command.type === 'tech_mode_peek' ||
		command.type === 'tech_mode_opt_reveal' ||
		command.type === 'tech_mode_eq_raised' ||
		command.type === 'tech_mode_edge_hover'
		|| command.type === 'track_info_toggle'
		|| command.type === 'track_info_open'
		|| command.type === 'track_info_close'
		|| command.type === 'oj_toggle'
		|| command.type === 'pairing_snapshot_open'
		|| command.type === 'pairing_snapshot_remove_eq_adjuster'
		|| command.type === 'pairing_snapshot_save'
		// AutoPlay Next arm/cancel dispatch their own scoped loop/eq
		// commands internally (auto-play-next.svelte.ts); this entry point
		// itself has no deck and nothing to serialize against.
		|| command.type === 'auto_play_next_arm'
		|| command.type === 'auto_play_next_cancel'
		|| command.type === 'auto_play_decide_now'
		|| command.type === 'autoplay_verdict'
		|| command.type === 'auto_play_next_intent'
		|| command.type === 'load_play_intent'
		|| command.type === 'playlist_undo'
		|| command.type === 'cmdk_open'
		|| command.type === 'cmdk_close'
		|| command.type === 'cmdk_pick'
		|| command.type === 'cmdk_mode'
		|| command.type === 'window'
		|| command.type === 'playlist_redo'
		|| command.type === 'playlist_history_jump'
		|| command.type === 'feedback_mark'
		|| command.type === 'deck_autocue'
		// DECKGAP-7691h: dispatches its own deck-scoped mixer and stem commands
		// below; holding the deck scope here would queue them behind itself.
		|| command.type === 'reset_deck_now'
	) {
		return null;
	}
	if (deck === null) throw new Error(`${command.type} has no command queue scope`);
	// prettier-ignore
	if (command.type === 'hot_cue_save' || command.type === 'hot_cue_clear' || command.type === 'hot_cue_restore') {
		return [_persistenceScope(deck)];
	}
	if (command.type === 'grid_edit') return [_persistenceScope(deck)];
	if (
		command.type === 'play' ||
		command.type === 'cue' ||
		command.type === 'memory_cue_call' ||
		command.type === 'seek' ||
		command.type === 'waveform_seek' ||
		// Arming resolves as soon as the graph's pending-segment queue accepts
		// the future schedule (no timer holds this scope across the wait -
		// see armHotCueTrigger), so grouping with seek/play cannot stall.
		command.type === 'hot_cue_trigger' ||
		command.type === 'beat_jump' ||
		command.type === 'loop_in' ||
		command.type === 'loop_out' ||
		command.type === 'reloop' ||
		command.type === 'grid_jump' ||
		command.type === 'tempo' ||
		command.type === 'beat_sync' ||
		command.type === 'sync_mode' ||
		command.type === 'master' ||
		command.type === 'master_tempo' ||
		command.type === 'key_sync' ||
		command.type === 'key_nudge' ||
		command.type === 'key_shift' ||
		// KEYSHIFT-03: a shifted load runs the same KEY nudges as key_nudge,
		// which feed KEY SYNC followers, so it claims the shared sync scope too.
		(command.type === 'load' && command.key_shift !== undefined && command.key_shift !== 0)
	) {
		return [deck, 'sync'];
	}
	return [deck];
}

function _errorMessage(error: unknown): string {
	// A failed load already worded its error for the deck (CLOUDSYNC-33).
	const deckFacing = deckFacingMessage(error);
	if (deckFacing !== undefined) return deckFacing;
	return error instanceof Error ? `${error.name}: ${error.message}` : String(error);
}

function _deckIsEmpty(deck: DeckId): boolean {
	return getDeckState(deck).stable_id === null;
}

function _refuseEmptyDeckTransport(deck: DeckId, what: 'play' | 'cueing'): void {
	if (_deckIsEmpty(deck)) {
		throw new Error(`Deck ${deck} is empty - load a track before ${what}`);
	}
}

function _isEmptyDeckTransportError(messageText: string): boolean {
	return (
		/no track loaded on deck \d+$/.test(messageText) ||
		/ is empty - load a track before /.test(messageText)
	);
}

/**
 * Q1 / S2: `pressT0Ms` is the input stamp on the `performance.now()` epoch, cut
 * at the UI boundary BEFORE the command can queue. It travels as an argument
 * rather than as a command field on purpose: `PerformanceCommand` is the
 * validated IPC wire shape (`_exactKeys` rejects unknown fields), and an
 * instrument has no business widening a public protocol.
 */
/** KEYSHIFT-03: a load from a shifted compatible-filter row lands at that key
 * shift. A fresh load starts at 0 semitones, so the shift is applied as unit
 * KEY nudges, the same path as the deck's KEY - / + buttons and the
 * `key_nudge` command (which is also the agent parity: load, then key_nudge). */
async function _applyLoadKeyShift(deck: DeckId, shift: number): Promise<void> {
	if (!Number.isInteger(shift) || Math.abs(shift) > COMPATIBLE_KEY_SHIFT_MAX) {
		throw new RangeError(
			`load key_shift must be an integer within ±${COMPATIBLE_KEY_SHIFT_MAX}, got ${shift}`
		);
	}
	const step = shift > 0 ? 1 : -1;
	for (let i = 0; i < Math.abs(shift); i += 1) await engine.nudgeKey(deck, step);
}

/** AGENT-8332b: would a plain load onto `deck` be refused as replacing a sounding deck? */
function _loadWouldBeBlocked(deck: DeckId): boolean {
	const st = getDeckState(deck);
	if (rustMode.enabled) return st.playing || st.audible;
	try {
		assertDeckLoadAllowed(deck);
		return false;
	} catch (error) {
		if (error instanceof DeckReplacementBlockedError) return true;
		throw error;
	}
}

async function _execute(command: PerformanceCommand, pressT0Ms?: number): Promise<void> {
	_recordPerformanceCommand(command);
	// Empty-deck transport is a user/agent mistake, not an engine fault.
	// Refuse here (same copy as MIDI action-glue) so Quick Draw, Trackify,
	// CLI, and a play queued across an unload never reach _requireLoaded's
	// `play: no track loaded on deck N` throw, which Sentry grouped as
	// OPEN-DJ-BE-6Y. Pause of an empty deck is a no-op (rescue layout restore).
	if (command.type === 'play' || command.type === 'cue') {
		if (command.type === 'play' && command.playing !== true) {
			if (_deckIsEmpty(command.deck)) {
				quantizedLaunchArmed[command.deck] = null;
				_quantizedLaunchDriver.clear(command.deck);
				return;
			}
		} else {
			_refuseEmptyDeckTransport(command.deck, command.type === 'cue' ? 'cueing' : 'play');
		}
	}
	if (command.type === 'master_volume') _operatorMasterVolume = command.value;
	// A device release can wait behind a load of the very same track ID. The
	// load generation, checked under the deck queue claim, owns the gesture.
	if (
		(command.type === 'loop' || command.type === 'beat_loop') &&
		command.if_load_generation !== undefined &&
		getDeckState(command.deck).load_generation !== command.if_load_generation
	)
		return;
	// Rust engine mode (opt-in, ?engine=rust): audio commands go to odj-audio
	// instead of the Web Audio engine; see lib/audio-engine/rust-mode.svelte.ts.
	//
	// refuseIfMaster is enforced HERE, before either engine sees the command:
	// rust-engine.ts handles load/unload without reading it, so a check inside
	// the Web Audio branches alone would let ?engine=rust replace the live
	// master. It runs inside the queued run() slot for this deck's scope, not
	// just at the UI dispatch boundary: 'master' shares this deck's scope
	// ([deck, 'sync']), so a MASTER command queued moments before this one
	// can complete BETWEEN the UI-layer is_master check and this command's
	// own turn (r3920224754). getDeckState reads engine state directly, which
	// is authoritative once this command's scope is held. Opt-in only (see
	// the PerformanceCommand union comment): a standalone load or eject must
	// stay able to target the master; engine.unload elects another playing
	// master afterward, which Deck.svelte's Unload button and Quick Draw rely
	// on even when it is the only loaded deck (r3920297846). Only
	// BrowserPanel's destructive-replace path opts in.
	// prettier-ignore
	if (
		(command.type === 'load' || command.type === 'unload') &&
		command.refuseIfMaster === true &&
		getDeckState(command.deck).is_master
	) {
		throw new Error(`cannot ${command.type}: CH${command.deck} is the live master - unload or reassign it first`);
	}
	// A strip click auditions in the headphones. The moment any deck load is
	// accepted, that audition stops: the operator is putting a track on a
	// deck to play it (pin 3f7259934680). A refused master load threw above,
	// so the preview keeps going, and so does a load the Web Audio engine
	// refuses up front (the deck is playing, audible or mid-transport): that
	// refusal throws here, before the silence (Codex P2 4287743996), and
	// engine.load repeats it under its own claim. Silence leaves the decoded
	// buffer for engine.load to reuse. AutoPlay loading the next track is not
	// a user load and leaves the audition playing (pin 104cf3b626fd).
	// AGENT-8332b: only an explicit replace_playing ejects a sounding deck first
	// (the same pause, presented stop and unload Deck's eject runs), so the
	// guard below then passes. Without it the refusal stands.
	if (command.type === 'load' && command.replace_playing === true) {
		initRustMode();
		if (_loadWouldBeBlocked(command.deck))
			await _execute({ type: 'unload', deck: command.deck }, pressT0Ms);
	}
	if (command.type === 'load') {
		initRustMode();
		if (!rustMode.enabled) assertDeckLoadAllowed(command.deck);
		if (deckCommandSource(command.deck) !== 'autoplay-handoff') silencePreviewCue();
	}
	// OFFLOAD: an offloaded track comes back from the cloud before either engine loads it.
	if (command.type === 'load') await ensureLocalBeforeLoad(command.deck, command.stable_id);
	// Before either engine: the Rust dispatch below returns early, and a folded launch must be reported once on both paths.
	if (command.type === 'beat_sync' && command.enabled && command.normalization !== undefined && command.master !== undefined) {
		reportFoldedLaunch(command.deck, command.master, command.normalization);
	}
	if (await executeInRustEngine(command, pushToast)) {
		if (command.type === 'load') await _quantizeOnForNewTrack(command.deck);
		return;
	}
	if (command.type === 'load') {
		// refuseIfMaster was enforced above, before the engine dispatch.
		// THE deck-load entry point from every UI surface, so it is also the
		// one place the boot scheduler has to be told a load is in flight:
		// deferred boot work waits for this to settle rather than racing it
		// for the origin's six connections (PERF-R6). The beacon is a
		// counter, not a lock -- it cannot fail the load, and the scheduler
		// releases on its own ceiling if a load never settles.
		const deckLoadSettled = bootScheduler.deckLoadStarted();
		try {
			await engine.load(command.deck, command.stable_id, {
				stems: command.stems,
				suppressFailureToast: command.suppressCommandErrorToast === true,
				...(command.edit_id !== undefined ? { edit_id: command.edit_id } : {}),
				...(command.in_ms !== undefined ? { in_ms: command.in_ms } : {}),
				...(command.out_ms !== undefined ? { out_ms: command.out_ms } : {})
			});
			if (command.in_ms !== undefined) {
				await engine.quantizedSeek(command.deck, command.in_ms, true);
			}
		} finally {
			deckLoadSettled();
		}
		if (command.key_shift !== undefined && command.key_shift !== 0) {
			await _applyLoadKeyShift(command.deck, command.key_shift);
		}
		hotCueReversals[command.deck] = null;
		hotCueArmed[command.deck] = null;
		waveformSeekArmed[command.deck] = null;
		quantizedLaunchArmed[command.deck] = null;
		noteRecentDeck(command.deck);
		await _quantizeOnForNewTrack(command.deck);
		_autoCueNewTrack(command.deck);
		requestGenreInferOnAttention([command.stable_id]);
	} else if (command.type === 'load_play_intent') {
		// Q1: the stamp rides WITH the intent, so the play this eventually
		// becomes can time from the operator's keydown and not from the load.
		if (
			!setPendingLoadPlayIntent(command.deck, command.generation, command.desired_play, pressT0Ms)
		) {
			throw new Error(
				`load_play_intent generation ${command.generation} is not pending on CH${command.deck}`
			);
		}
	} else if (command.type === 'unload') {
		// refuseIfMaster was enforced above, before the engine dispatch.
		await engine.unload(command.deck);
		hotCueReversals[command.deck] = null;
		hotCueArmed[command.deck] = null;
		waveformSeekArmed[command.deck] = null;
		quantizedLaunchArmed[command.deck] = null;
	} else if (command.type === 'play') {
		if (command.playing) {
			if (command.start_at_context_sec !== undefined) {
				await engine.play(command.deck, pressT0Ms, command.start_at_context_sec);
			} else if (
				_quantizedLaunchArmedSnapshot(command.deck) !== null &&
				command.quantize !== true
			) {
				// The snapshot, not the raw record: a launch that has already
				// landed is not cleared by reads, and must not turn this press
				// into a disarm.
				quantizedLaunchArmed[command.deck] = null;
				_quantizedLaunchDriver.clear(command.deck);
			} else if (command.quantize === true) {
				if (pressT0Ms !== undefined) markArmedHotCuePress(pressT0Ms);
				const launchAt = await _quantizedLaunchDriver.arm(command.deck, pressT0Ms);
				quantizedLaunchArmed[command.deck] = { launch_at_context_sec: launchAt };
			} else {
				await engine.play(command.deck, pressT0Ms);
			}
		} else {
			quantizedLaunchArmed[command.deck] = null;
			_quantizedLaunchDriver.clear(command.deck);
			await engine.pause(command.deck, pressT0Ms);
		}
		noteRecentDeck(command.deck);
	} else if (command.type === 'cue') {
		await engine.pressCue(command.deck, pressT0Ms);
		noteRecentDeck(command.deck);
	} else if (command.type === 'memory_cue_call') {
		const plan = await engine.callMemoryCue(command.deck, command.direction, pressT0Ms);
		// None in that direction moves nothing and says why (the grid_jump pattern).
		if (plan.kind === 'refused') pushToast(`Deck ${command.deck}: ${plan.reason}`, 'info');
		noteRecentDeck(command.deck);
	} else if (command.type === 'seek') {
		waveformSeekArmed[command.deck] = null;
		// exact: the authored position lands as given, not re-snapped to the grid (a plan's prepare cue).
		await engine.quantizedSeek(command.deck, command.position_ms, command.exact === true);
	} else if (command.type === 'waveform_seek') {
		const state = getDeckState(command.deck);
		const beats = state.anlz?.beatgrid.beats ?? [];
		const loopEngaged = state.loop !== null && state.loop.engaged;
		const positionSec = state.position_ms / 1000;
		const trustAnlz = state.anlz ?? {
			beatgrid: { source: 'rekordbox', beats: [...beats], beat_count: beats.length, status: 'ok' }
		};
		const plan = planWaveformSeek(
			uiPrefs.beat_sync_max && hasTrustedBeatGrid(trustAnlz) && hasPhaseCapableBeatGrid(trustAnlz),
			state.playing,
			loopEngaged,
			positionSec,
			beats,
			command.snap
		);
		if (plan.kind === 'immediate') {
			waveformSeekArmed[command.deck] = null;
			hotCueArmed[command.deck] = null;
			await _hotCueDriver.jump(command.deck, command.position_ms, pressT0Ms);
		} else {
			hotCueArmed[command.deck] = null;
			if (pressT0Ms !== undefined) markArmedHotCuePress(pressT0Ms);
			// The plan above only decides WHETHER to defer. The arm point is
			// re-resolved from the engine's live position inside the scheduling
			// transaction: `state.position_ms` is a published snapshot that keeps
			// falling behind while this command queues, and a click at or just
			// after a downbeat must roll to the next one, not throw (#4011).
			const targetContextTime = await _hotCueDriver.arm(
				command.deck,
				command.position_ms,
				(nowPositionSec) => nextDownbeatAtOrAfter(beats, nowPositionSec),
				pressT0Ms
			);
			waveformSeekArmed[command.deck] = {
				target_position_ms: command.position_ms,
				target_context_time: targetContextTime
			};
		}
	} else if (command.type === 'loop') {
		await engine.setLoop(command.deck, command.loop, command.quantize !== false);
	} else if (command.type === 'beat_loop') {
		await engine.engageBeatLoop(command.deck, command.beats, command.start_ms);
	} else if (command.type === 'loop_in') {
		manualLoopIn(_manualLoopPort, command.deck);
	} else if (command.type === 'loop_out') {
		await manualLoopOut(_manualLoopPort, command.deck);
	} else if (command.type === 'reloop') {
		await manualReloop(_manualLoopPort, command.deck);
	} else if (command.type === 'beat_jump') {
		await engine.beatJump(command.deck, command.beats);
	} else if (command.type === 'deck_move') {
		const moved = await moveDeck(webAudioDeckMovePort(), {
			from: command.from,
			to: command.to,
			mode: command.mode ?? 'move',
			force: command.force === true,
			crossfade_beats: command.crossfade_beats ?? DECK_MOVE_DEFAULT_CROSSFADE_BEATS
		});
		pushToast(
			`Deck ${moved.from} ${moved.mode === 'move' ? 'moved' : 'copied'} to deck ${moved.to}`,
			'info'
		);
	} else if (command.type === 'grid_jump') {
		const plan = await engine.gridJump(command.deck, command.bars);
		// A refusal moves nothing and says why (DECKUX-6794 AC-13), the way a
		// grid-inert control does, so a key press is never a silent no-op.
		if (plan.kind === 'refused') pushToast(`Deck ${command.deck}: ${plan.reason}`, 'info');
	} else if (command.type === 'beat_jump_size') {
		setBeatJumpSize(command.deck, command.beats);
	} else if (command.type === 'loop_interval_mode') {
		setLoopIntervalMode(command.deck, command.enabled);
	} else if (command.type === 'loop_interval_base') {
		setLoopIntervalBase(command.deck, command.base);
	} else if (command.type === 'tempo') {
		await engine.setTempoRatio(command.deck, command.ratio);
	} else if (command.type === 'pitch_range') {
		engine.setPitchRange(command.deck, command.range);
	} else if (command.type === 'quantize') {
		engine.setQuantize(command.deck, command.enabled);
	} else if (command.type === 'quantize_grid') {
		engine.setQuantizeGrid(command.deck, command.beats);
	} else if (command.type === 'beat_sync') {
		await engine.setBeatSync(command.deck, command.enabled);
	} else if (command.type === 'sync_mode') {
		await engine.setSyncMode(command.deck, command.mode);
	} else if (command.type === 'master') {
		await engine.setDeckMaster(
			command.deck,
			command.lock === undefined ? undefined : { lock: command.lock }
		);
	} else if (command.type === 'master_tempo') {
		await engine.setMasterTempo(command.deck, command.enabled);
	} else if (command.type === 'stem_mute') {
		engine.setStemMute(command.deck, command.stem, command.muted, pressT0Ms);
		void captureStemCommand(
			command,
			deckCommandSource(command.deck) ?? 'app-command',
			pressT0Ms ?? performance.now()
		);
	} else if (command.type === 'stem_solo') {
		engine.setStemSolo(command.deck, command.stem, command.solo, pressT0Ms, command.exclusive);
		void captureStemCommand(
			command,
			deckCommandSource(command.deck) ?? 'app-command',
			pressT0Ms ?? performance.now()
		);
	} else if (command.type === 'stem_eq_mode') {
		engine.setStemEqMode(command.deck, command.enabled);
	} else if (command.type === 'stem_load') {
		// Checked here, when the command runs, not when it was issued: a load
		// of another track may have replaced the deck's track meanwhile, and
		// that track must not be reloaded on this command's behalf (Codex on
		// #6819). Not an error: nothing failed, and a toast or deck banner
		// would fire on every track change. The caller reads the deck to tell
		// a skipped load from one that ran (Deck.svelte's probe does).
		const held = getDeckState(command.deck).stable_id;
		if (command.stable_id !== undefined && held !== command.stable_id) {
			console.debug(
				`[performance-ipc] stem_load skipped: deck ${command.deck} no longer holds ${command.stable_id}`
			);
			return;
		}
		// The ended run this load can look for, read before the reload starts
		// (Codex P1 on #6819).
		const run = stemLoadRun(held);
		await engine.retryStems(command.deck);
		// The look that lets this deck's stem_separate follow an ended job,
		// for the button and an agent alike: recorded once the reload
		// retryStems started has left loading, if the deck still holds that
		// track (Sol P1 on #6819). Not awaited, so the deck queue never waits
		// out a stem decode; until it lands, stem_separate still refuses.
		void noteStemsLoadedOnceSettled(command.deck, held, run, () => getDeckState(command.deck));
	} else if (command.type === 'stem_separate') {
		// The deck is read when the command runs, and again once the store has
		// loaded (separateLoadedDeck): stem_separate is outside the deck queue,
		// and a load of another track can land while this command waits, so a
		// click on one track must not enqueue a paid separation of the next
		// (Codex on #6819). Unlike a skipped stem_load this is an error: the
		// separation asked for did not start.
		// A command that names no track was bound on receipt (_dispatchUnknown);
		// only the preset path, which skips that, reaches this fallback.
		const expected = command.stable_id ?? getDeckState(command.deck).stable_id;
		await separateLoadedDeck(command.deck, expected, () => getDeckState(command.deck));
	} else if (command.type === 'stem_gain') {
		engine.setStemGain(command.deck, command.stem, command.value);
		void captureStemCommand(
			command,
			deckCommandSource(command.deck) ?? 'app-command',
			pressT0Ms ?? performance.now()
		);
	} else if (command.type === 'slip') {
		await engine.setSlip(command.deck, command.enabled);
	} else if (command.type === 'key_sync') {
		await engine.setKeySync(command.deck, command.enabled, {
			...(command.mode === undefined ? {} : { mode: command.mode }),
			...(command.target === undefined ? {} : { target: command.target })
		});
	} else if (command.type === 'key_nudge') {
		await engine.nudgeKey(command.deck, command.semitones);
	} else if (command.type === 'key_shift') {
		await engine.setKeyShift(command.deck, command.semitones);
	} else if (command.type === 'grid_edit') {
		const st = getDeckState(command.deck);
		_assertGridEditTarget(command, st);
		// Checked again right before the write: the per-track queue can hold
		// this press behind another deck's edit of the same track.
		await runGridEditCommand(st.stable_id, st.anlz, command.edit, () =>
			_assertGridEditTarget(command, getDeckState(command.deck))
		);
	} else if (command.type === 'trim') {
		engine.setTrim(command.deck, command.value);
	} else if (command.type === 'eq') {
		// AGENT-30: a plan step with `at_context_sec` starts on that AudioContext time; anything else is a plain set now.
		if (command.at_context_sec !== undefined) engine.scheduleStep(command.deck, command.band, command.value, command.at_context_sec, command.step_id!);
		else engine.setEq(command.deck, command.band, command.value, pressT0Ms);
	} else if (command.type === 'filter') {
		engine.setFilter(command.deck, command.value, pressT0Ms);
	} else if (command.type === 'fader') {
		if (command.at_context_sec !== undefined) engine.scheduleStep(command.deck, 'fader', command.value, command.at_context_sec, command.step_id!);
		else engine.setFader(command.deck, command.value, pressT0Ms);
	} else if (command.type === 'assign') {
		engine.assignChannel(command.deck, command.assign);
	} else if (command.type === 'channel_cue') {
		engine.setChannelCue(command.deck, command.enabled);
	} else if (command.type === 'crossfader') {
		engine.setCrossfader(command.value, pressT0Ms);
	} else if (command.type === 'master_volume') {
		engine.setMaster(command.value);
	} else if (command.type === 'auto_gain_offset') {
		await engine.setAutoGainOffset(command.deck, command.offset_db, command.stable_id);
	} else if (command.type === 'master_mute') {
		// persist:false is the MCP safety rail's mute: silence this page now,
		// but never store it where another browser would start muted.
		setMasterMuted(command.muted, { persist: command.persist !== false });
	} else if (command.type === 'browser_related') {
		if (_browserAdapter === null) throw new Error('browser_related requires a mounted browser panel');
		_browserAdapter.setRelatedActive(command.active);
	} else if (command.type === 'browser_related_criteria') {
		const { type: _t, ...patch } = command;
		await patchRelatedCriteria(patch);
	} else if (command.type === 'browser_related_criteria_reset') {
		await resetRelatedCriteria();
	} else if (command.type === 'browser_select_playlist') {
		if (_browserAdapter === null)
			throw new Error('browser_select_playlist requires a mounted browser panel');
		try {
			await _browserAdapter.selectPlaylist(command.playlist_id, command.stable_id);
		} catch (error) {
			_activeBrowserPlaylist = null;
			throw error;
		}
		_activeBrowserPlaylist = command.playlist_id;
	} else if (command.type === 'preview_cue') {
		// The UI path gets the refusal as a toast. An agent gets it as a failed
		// step, so `POST /performance/headphones/preview` answers 400 with the
		// reason instead of 200 over a preview that never started.
		const outcome = await previewCueSeek(command.stable_id, command.ratio, {
			trackBpm: command.bpm ?? null
		});
		if (!outcome.ok) throw new Error(outcome.reason);
		if (outcome.warning !== null) pushToast(outcome.warning, 'warn');
	} else if (command.type === 'preview_stop') {
		stopPreviewCue();
	} else if (command.type === 'headphone_mix') {
		engine.setHeadphoneMix(command.value);
	} else if (command.type === 'headphone_level') {
		engine.setHeadphoneLevel(command.value);
	} else if (command.type === 'headphone_booth_level') {
		engine.setBoothLevel(command.value);
	} else if (command.type === 'head_delay_ms') {
		engine.setHeadDelayMs(command.value);
	} else if (command.type === 'headphone_outputs_refresh') {
		await engine.refreshHeadphoneOutputs();
	} else if (command.type === 'headphone_output_acquire') {
		await engine.acquireHeadphoneOutput();
	} else if (command.type === 'headphone_output_select') {
		await engine.selectHeadphoneOutput(command.device_id);
	} else if (command.type === 'headphone_master_select') {
		await engine.selectMasterOutput(command.device_id);
	} else if (command.type === 'headphone_booth_select') {
		await engine.selectBoothOutput(command.device_id);
	} else if (command.type === 'headphone_input_select') {
		await engine.selectAudioInput(command.device_id);
	} else if (command.type === 'output_mode') {
		engine.setHeadphoneOutputMode(command.mode);
	} else if (command.type === 'headphone_alignment_mode') {
		engine.setHeadphoneAlignmentMode(command.value);
	} else if (command.type === 'master_delay_ms') {
		engine.setMasterDelayMs(command.value);
	} else if (command.type === 'headphone_calibrate') {
		await startCueAlignment({ interactive: command.interactive ?? false });
	} else if (command.type === 'headphone_calibrate_abort') {
		abortCueAlignment();
	} else if (command.type === 'analysis_source') {
		await setAnalysisSource(command.feature, command.source);
	} else if (command.type === 'library_panels') {
		setLibraryPanelCollapsed(command.panel, command.collapsed);
	} else if (command.type === 'library_panels_toggle') {
		setLibraryPanelsCollapsed(command.collapsed ?? !isLibraryPanelsCollapsed());
	} else if (command.type === 'show_stems') {
		setShowStems(command.enabled);
	} else if (command.type === 'wave_eq_reflect') {
		setWaveEqReflect(command.enabled);
	} else if (command.type === 'mixer_stems') {
		setMixerStems(command.enabled);
	} else if (command.type === 'deck_autocue') {
		await setAutoCue(command.enabled);
	} else if (command.type === 'color_fx') {
		// COLORFX-02: the settings half, then the knob through the same setFilter
		// the FILTER dial uses, which drives the channel with the new effect.
		// COLORFX-8341: `next` resolves here, and a real switch returns the knob to
		// center (12 o'clock) unless the command names a value.
		const plan = planColorFxSwitch(
			colorFx[command.deck].effect,
			mixerState.channels[command.deck].filter,
			command
		);
		applyColorFxSettings({
			deck: command.deck,
			effect: plan.effect,
			echo_beats: command.echo_beats,
			value: plan.value
		});
		engine.setFilter(command.deck, plan.value, pressT0Ms);
	} else if (command.type === 'autoplay') {
		setAutoPlayEnabled(command.enabled);
	} else if (command.type === 'set_deck_number_art') {
		setDeckNumberArt(command.mode);
	} else if (command.type === 'set_waveform_overlay') {
		if (command.overlay === 'grid_lines') {
			setWaveGridLines(command.value);
		} else {
			const setOverlay = {
				sections: setShowSections,
				lyrics: setLyricsWaveformOverlay,
				lyrics_scrub: setLyricsHoverScrub,
				jog_radial: setJogRadialWaveform,
				vocal_lane: setShowVocalLane
			}[command.overlay];
			setOverlay(command.enabled);
		}
	} else if (command.type === 'set_waveform_design') {
		if (command.design !== undefined) setWaveformDesign(command.design);
		if (command.height !== undefined) setWaveHeight(command.height);
	} else if (command.type === 'set_wave_zoom') {
		setWaveZoom(command.zoom, command.deck);
	} else if (command.type === 'wave_zoom_step') {
		stepWaveZoomPref(command.direction, command.deck);
	} else if (command.type === 'set_wave_zoom_options') {
		setWaveZoomPlayhead(command.playhead);
		setWaveZoomLinked(command.linked);
	} else if (command.type === 'set_skin') {
		setUiSkin(command.ui_skin);
		setWavePalette(command.wave_palette);
		setWaveSplitMaster(command.wave_split_master);
	} else if (command.type === 'set_layout_preset') {
		setLayoutOverride(command.surface, command.preset);
	} else if (command.type === 'set_layout_size') {
		setLayoutSize(command.region, command.px);
	} else if (command.type === 'reset_layout') {
		resetLayout(command.region);
	} else if (command.type === 'safety_loop_save') {
		// Engine-side and synchronous: it captures the deck's currently
		// engaged loop, and throws when there is none to capture.
		engine.saveSafetyLoop(command.deck);
	} else if (command.type === 'safety_loop_arm') {
		engine.setSafetyLoopArmed(command.deck, command.armed);
	} else if (command.type === 'safety_loop_clear') {
		engine.clearSafetyLoop(command.deck);
	} else if (command.type === 'hot_cue_save') {
		const deckAnlz = getDeckState(command.deck).anlz;
		const stableId = _hotCueDriver.stableId(command.deck);
		if (stableId === null) throw new Error(`hot cue ${command.slot}: deck is not loaded`);
		const beatSyncMaxSnap =
			command.out_ms === undefined &&
			uiPrefs.beat_sync_max &&
			hasTrustedBeatGrid(deckAnlz) &&
			hasPhaseCapableBeatGrid(deckAnlz);
		const savedPositionMs = beatSyncMaxSnap
			? Math.round(
					quantizeToNearestDownbeat(
						getDeckState(command.deck).anlz?.beatgrid.beats ?? [],
						command.in_ms / 1000
					) * 1000
				)
			: command.in_ms;
		// prettier-ignore
		const result = await saveHotCue(
			stableId, command.slot, savedPositionMs, command.revision, command.comment, command.out_ms ?? null
		);
		if (result.reversal === undefined)
			throw new Error(`hot cue ${command.slot}: server omitted reversal token`);
		hotCueReversals[command.deck] = {
			slot: command.slot,
			revision: result.revision,
			reversal_id: result.reversal.reversal_id
		};
		await _hotCueDriver.refresh(command.deck);
	} else if (command.type === 'hot_cue_clear') {
		const stableId = _hotCueDriver.stableId(command.deck);
		if (stableId === null) throw new Error(`hot cue ${command.slot}: deck is not loaded`);
		const result = await clearHotCue(stableId, command.slot, command.revision);
		if (result.reversal === undefined)
			throw new Error(`hot cue ${command.slot}: server omitted reversal token`);
		hotCueReversals[command.deck] = {
			slot: command.slot,
			revision: result.revision,
			reversal_id: result.reversal.reversal_id
		};
		await _hotCueDriver.refresh(command.deck);
	} else if (command.type === 'hot_cue_restore') {
		const stableId = _hotCueDriver.stableId(command.deck);
		if (stableId === null) throw new Error(`hot cue ${command.slot}: deck is not loaded`);
		await restoreHotCue(stableId, command.slot, command.revision, command.reversal_id);
		hotCueReversals[command.deck] = null;
		await _hotCueDriver.refresh(command.deck);
	} else if (command.type === 'hot_cue_trigger') {
		const { cue, playing, loopEngaged, positionSec, beats } = _hotCueDriver.triggerState(
			command.deck,
			command.slot
		);
		if (cue === null) throw new Error(`hot cue ${command.slot}: nothing to trigger`);
		const anlz = getDeckState(command.deck).anlz ?? {
			beatgrid: { source: 'rekordbox', beats: [...beats], beat_count: beats.length, status: 'ok' }
		};
		const plan = planHotCueTrigger(
			uiPrefs.beat_sync_max && hasTrustedBeatGrid(anlz) && hasPhaseCapableBeatGrid(anlz),
			playing,
			loopEngaged || _loopHotCueRange(cue) !== null,
			positionSec,
			beats
		);
		if (plan.kind === 'immediate') {
			hotCueArmed[command.deck] = null;
			waveformSeekArmed[command.deck] = null;
			await _hotCueDriver.jump(command.deck, cue.in_ms, pressT0Ms);
			const loopCue = _loopHotCueRange(cue);
			if (loopCue !== null) await _hotCueDriver.engageLoop(command.deck, loopCue);
		} else {
			waveformSeekArmed[command.deck] = null;
			// Mark BEFORE the row can file: the eventual schedule reads this same
			// stamp via press-stamp.ts's claimArmedHotCuePress to distinguish an
			// armed (deferred-to-downbeat) wait from an immediate press row.
			if (pressT0Ms !== undefined) markArmedHotCuePress(pressT0Ms);
			// Same rule as waveform_seek above (#4011): the plan only decides
			// WHETHER to defer; the downbeat is resolved from the live playhead.
			const targetContextTime = await _hotCueDriver.arm(
				command.deck,
				cue.in_ms,
				(nowPositionSec) => nextDownbeatAtOrAfter(beats, nowPositionSec),
				pressT0Ms
			);
			hotCueArmed[command.deck] = {
				slot: command.slot,
				target_position_ms: cue.in_ms,
				target_context_time: targetContextTime
			};
		}
		noteRecentDeck(command.deck);
	} else if (command.type === 'auto_play_two_track') {
		setAutoPlayTwoDeck(command.enabled);
	} else if (command.type === 'set_perf_view') {
		setPerfView(command.view);
	} else if (command.type === 'show_pads') {
		setShowPads(command.enabled);
	} else if (command.type === 'key_nudge_step') {
		writeKeyNudgeStep(command.step);
	} else if (command.type === 'reset_deck_now') {
		await _resetDeckNow(command.deck, resetNowPrefs(command.parts));
	} else if (command.type === 'track_load_reset') {
		const { level, stems, eq, trim, filter } = command;
		writeTrackLoadReset({ level, stems, eq, trim, filter });
	} else if (command.type === 'auto_play_next_arm') {
		if (_autoPlayNextController === null) {
			throw new Error('auto_play_next_arm: no AutoPlay Next controller is mounted on this route');
		}
		await _autoPlayNextController.arm({
			intent: command.intent,
			outDeck: command.out_deck,
			inDeck: command.in_deck,
			fromMs: command.from_ms
		});
	} else if (command.type === 'auto_play_next_cancel') {
		if (_autoPlayNextController === null) {
			throw new Error(
				'auto_play_next_cancel: no AutoPlay Next controller is mounted on this route'
			);
		}
		await _autoPlayNextController.cancel();
	} else if (command.type === 'autoplay_verdict') {
		await postVerdict({ out_track_id: command.out, in_track_id: command.in }, command.verdict);
	} else if (command.type === 'auto_play_decide_now') {
		if (_autoPlayNextController === null) {
			throw new Error('auto_play_decide_now: no AutoPlay Next controller is mounted on this route');
		}
		await _autoPlayNextController.decideNow();
	} else if (command.type === 'auto_play_next_intent') {
		if (_autoPlayNextController === null) {
			throw new Error(
				'auto_play_next_intent: no AutoPlay Next controller is mounted on this route'
			);
		}
		_autoPlayNextController.setIntent(command.intent);
	} else if (command.type === 'auto_play_early') {
		setAutoPlayEarly(command);
	} else if (command.type === 'pins_show_other_users') {
		throw new Error('pins_show_other_users must be rejected at the dispatch boundary');
	} else if (command.type === 'tech_mode_toggle') {
		toggleTechMode();
	} else if (command.type === 'tech_mode_peek') {
		setPeeking(command.peeking);
	} else if (command.type === 'tech_mode_opt_reveal') {
		setOptReveal(command.revealed);
	} else if (command.type === 'tech_mode_eq_raised') {
		setEqRaised(command.raised);
	} else if (command.type === 'tech_mode_edge_hover') {
		setEdgeHovered(command.edge, command.hovered);
	} else if (command.type === 'track_info_toggle') {
		toggleTrackInfo(_trackInfoDecks());
	} else if (command.type === 'track_info_open') {
		if ('stable_id' in command) {
			openTrackInfoForRow(command.stable_id);
		} else {
			openTrackInfoForDeck(command.deck, _trackInfoDecks());
		}
	} else if (command.type === 'track_info_close') {
		closeTrackInfo();
	} else if (command.type === 'oj_toggle') {
		toggleOjSidebar();
	} else if (command.type === 'pairing_snapshot_open') {
		_pairingSnapshot = _openPairingSnapshot();
	} else if (command.type === 'pairing_snapshot_remove_eq_adjuster') {
		if (_pairingSnapshot === null) throw new Error('pairing snapshot is not open');
		const deck = _pairingSnapshot.decks.find((item) => item.deck_id === command.deck);
		if (deck === undefined) throw new Error(`CH${command.deck} is not in the pairing snapshot`);
		if (!deck.eq_adjusts.some((adjust) => adjust.band === command.band)) {
			throw new Error(`CH${command.deck} has no ${command.band} EQ adjustment`);
		}
		_pairingSnapshot = {
			..._pairingSnapshot,
			decks: _pairingSnapshot.decks.map((deck) =>
				deck.deck_id !== command.deck
					? deck
					: {
							...deck,
							eq_adjusts: deck.eq_adjusts.filter((adjust) => adjust.band !== command.band)
						}
			)
		};
	} else if (command.type === 'pairing_snapshot_save') {
		if (_pairingSnapshot === null) throw new Error('pairing snapshot is not open');
		const from = _pairingSnapshot.decks.find((deck) => deck.deck_id === command.from_deck);
		const to = _pairingSnapshot.decks.find((deck) => deck.deck_id === command.to_deck);
		if (from === undefined || to === undefined)
			throw new Error('selected decks are not in the pairing snapshot');
		const frozen = _pairingSnapshot;
		// W19: the pairing row is write-once (PAIR-04), so a recapture of a
		// saved pair changed nothing. Every Capture also appends the sync
		// snapshot Reload sync replays: the frozen positions, live tempo and master.
		const live = (deck: typeof from) => {
			const state = getDeckState(deck.deck_id);
			return {
				stable_id: deck.stable_id,
				position_ms: deck.position_ms,
				tempo_ratio: state.pitch,
				is_master: state.is_master
			};
		};
		await capturePairing(
			{
				from_stable_id: from.stable_id,
				to_stable_id: to.stable_id,
				snapshot: { ...frozen, decks: [from, to] }
			},
			live(from),
			live(to),
			frozen.beat_sync_max
		);
	} else if (command.type === 'feedback_mark') {
		throw new Error('feedback_mark must be captured at the dispatch boundary');
	} else if (command.type === 'cmdk_open') {
		await _cmdk(command.type).open(command.query ?? null);
	} else if (command.type === 'cmdk_close') {
		_cmdk(command.type).close();
	} else if (command.type === 'cmdk_pick') {
		await _cmdk(command.type).pick(
			command.index,
			command.deck ?? null,
			command.confirm === true,
			command.id ?? null,
			command.target ?? null,
			command.vocals_only === true,
			'agent-command'
		);
	} else if (command.type === 'cmdk_mode') {
		_cmdk(command.type).setMode(command.mode);
	} else if (command.type === 'window') {
		await applyWindowCommand(command.action, command.label);
	} else if (command.type === 'playlist_undo') {
		if (_playlistHistoryAdapter === null) {
			throw new Error('playlist_undo requires a mounted playlist history panel');
		}
		await _playlistHistoryAdapter.undo();
	} else if (command.type === 'playlist_redo') {
		if (_playlistHistoryAdapter === null) {
			throw new Error('playlist_redo requires a mounted playlist history panel');
		}
		await _playlistHistoryAdapter.redo();
	} else if (command.type === 'playlist_history_jump') {
		if (_playlistHistoryAdapter === null) {
			throw new Error('playlist_history_jump requires a mounted playlist history panel');
		}
		await _playlistHistoryAdapter.jump(command.command_id);
	} else if (command.type === 'rescue_resume') {
		const plans = command.decks.map((entry) => ({
			deck: entry.deck,
			positionSec: entry.position_ms / 1000
		}));
		await engine.rescueResumeTogether(plans, command.master_deck);
		_rescueRestoredDecks = command.decks.map((entry) => entry.deck);
	} else if (command.type === 'rescue_stop_all') {
		const decks =
			_rescueRestoredDecks.length > 0
				? _rescueRestoredDecks
				: DECK_IDS.filter((deckId) => getDeckState(deckId).playing);
		await engine.rescueStopAllTogether(decks);
		_rescueRestoredDecks = [];
	} else if (command.type === 'midi_takeover_mode') {
		setMidiTakeoverMode(command.mode);
	} else {
		const _exhaustive: never = command;
		throw new Error(`Unhandled performance command: ${JSON.stringify(_exhaustive)}`);
	}
}

/** How `_persistCommandError` already put an error on screen, so the UI
 * boundary can tell "reported" from "about to vanish" without a second toast.
 * `toasted` also reached console.error (an error toast writes its own log
 * row there); `banner_only` is a load whose caller suppressed the toast. */
const _persistedCommandErrors = new WeakMap<object, 'toasted' | 'banner_only'>();

/** Refused loads that never reach `engine.load` still need mirror + banner parity. */
export function recordDeckLoadRefusal(deck: DeckId, message: string): void {
	deckLoadErrors[deck] = message;
	performanceCommandStatus.deck_errors[deck] = message;
	pushToast(message, 'error', DECK_LOAD_ERROR_TOAST_MS);
}

function _persistCommandError(
	deck: DeckId | null,
	error: unknown,
	command?: PerformanceCommand
): void {
	const messageText = _errorMessage(error);
	performanceCommandStatus.last_error = messageText;
	if (deck !== null) performanceCommandStatus.deck_errors[deck] = messageText;
	if (command?.type === 'load' && deck !== null && deckLoadErrors[deck] == null) {
		deckLoadErrors[deck] = messageText;
	}
	const errorObject = typeof error === 'object' && error !== null ? error : null;
	if (
		(command !== undefined &&
			command.type === 'load' &&
			command.suppressCommandErrorToast === true) ||
		// A worded load failure already raised its own deck-load toast (CLOUDSYNC-33).
		deckFacingMessage(error) !== undefined
	) {
		if (errorObject !== null) _persistedCommandErrors.set(errorObject, 'banner_only');
		return;
	}
	if (errorObject !== null) _persistedCommandErrors.set(errorObject, 'toasted');
	if (command !== undefined && command.type === 'load') {
		reportDeckLoadCommandFailure(command.deck, messageText, error);
		return;
	}
	let subcontrol = '';
	if (command !== undefined && 'band' in command) subcontrol = command.band;
	else if (command !== undefined && 'stem' in command) subcontrol = command.stem;
	else if (command !== undefined && 'slot' in command) subcontrol = command.slot;
	const toastKey =
		command === undefined ? undefined : `performance:${deck}:${command.type}:${subcontrol}`;
	if (_isEmptyDeckTransportError(messageText)) {
		// A user/agent mistake, not an engine fault: warn, never a Sentry error.
		pushToast(messageText.replace(/^Error: /, ''), 'warn', undefined, error, {}, toastKey);
		return;
	}
	pushToast(`Performance command failed - ${messageText}`, 'error', undefined, error, {}, toastKey);
}

/**
 * Clear the visible error on one deck.
 *
 * The banner used to be clearable only as a side effect of the next command
 * that happened to touch the same deck, so an error from a step nothing would
 * retry (AutoPlay's master handover) stuck to the deck for the rest of the
 * set with no way to get rid of it. Operator-driven dismissal is the fix; the
 * underlying condition is unchanged and will re-report if it recurs.
 */
export function dismissPerformanceDeckError(deck: DeckId): void {
	performanceCommandStatus.deck_errors[deck] = null;
	const st = getDeckState(deck);
	st.sync_error = null;
	st.processor_error = null;
}

function _resetCommandStatus(): void {
	performanceCommandStatus.last_error = null;
	performanceCommandStatus.active = 0;
	performanceCommandStatus.queued = 0;
	for (const deck of DECK_IDS) {
		performanceCommandStatus.deck_errors[deck] = null;
		performanceCommandStatus.deck_pending[deck] = 0;
	}
}

function _commandSessionError(generation: number): ScopedCommandInvalidatedError {
	return new ScopedCommandInvalidatedError(
		`performance command session ${generation} was invalidated`
	);
}

function _assertCommandSession(generation: number): void {
	if (_activeCommandSession?.generation !== generation) {
		throw _commandSessionError(generation);
	}
}

function _commandSessionIsCurrent(generation: number): boolean {
	return _activeCommandSession?.generation === generation;
}

function _startCommandSession(): number {
	if (_activeCommandSession !== null) {
		throw new Error(
			`performance command session ${_activeCommandSession.generation} is already active`
		);
	}
	_commandGeneration += 1;
	_activeCommandSession = { generation: _commandGeneration };
	return _commandGeneration;
}

function _clearPerformanceArmedRecords(): void {
	for (const deckId of DECK_IDS) {
		waveformSeekArmed[deckId] = null;
		hotCueArmed[deckId] = null;
		quantizedLaunchArmed[deckId] = null;
	}
}

onPerformanceArmedContextReset(_clearPerformanceArmedRecords);

function _invalidateCommandSession(generation: number): void {
	_assertCommandSession(generation);
	_activeCommandSession = null;
	_commandStatusGeneration += 1;
	_commandScheduler.invalidateQueued(`performance command session ${generation} was invalidated`);
	// A running Audio I/O Test still opening its microphone gives up now
	// rather than holding the headphone scope into the next mount.
	for (const test of _audioIoTestAborts)
		test.abort(new Error('The performance view closed during the Test.'));
	_audioIoTestAborts.clear();
	_resetCommandStatus();
	// Armed records carry a target time on the route-owned AudioContext, which
	// dies with this session: a record surviving into the next mount would be
	// measured against an uninitialised or brand-new clock (#4011 review).
	_clearPerformanceArmedRecords();
}

/** The value of the last master_volume command this page executed, or null
 * if none has run. The route's teardown hard mute (`engine.setMaster(0)`) and
 * a preset's late-settlement mute write the mixer directly, so a live
 * `mixer.master` of 0 is not by itself an operator's choice; this is. */
let _operatorMasterVolume: number | null = null;

export function operatorMasterVolume(): number | null {
	return _operatorMasterVolume;
}

/** The generation of the live route command session, or null between
 * mounts. A session-scoped writer binds to the value it saw at install and
 * stops the moment it changes, so nothing it writes can describe a torn-down
 * route (hard-muted master, stopped decks). */
export function activePerformanceCommandSession(): number | null {
	return _activeCommandSession?.generation ?? null;
}

function _currentCommandSession(): number {
	const session = _activeCommandSession;
	if (session === null) throw _commandSessionError(_commandGeneration);
	return session.generation;
}

function _assertPresetId(id: string): void {
	if (typeof id !== 'string' || id.trim() === '') {
		throw new TypeError('performance preset transaction id must be a non-empty string');
	}
}

function _setPresetPhase(id: string, phase: PerformancePresetPhase): void {
	if (_presetClaim?.id !== id) {
		throw new Error(`performance preset ${id} does not own the lifecycle lock`);
	}
	performancePresetLifecycle.phase = phase;
}

function _releasePreset(id: string, phase: 'idle' | 'ready' | 'error', error: string | null): void {
	if (_presetClaim?.id !== id) {
		throw new Error(`performance preset ${id} cannot release an unowned lifecycle lock`);
	}
	_presetClaim = null;
	_drainHeldAutoGain();
	performancePresetLifecycle.phase = phase;
	performancePresetLifecycle.active = false;
	performancePresetLifecycle.error = error;
	if (phase === 'idle') performancePresetLifecycle.id = null;
}

async function _dispatchWithinPreset(requested: PerformanceCommand): Promise<PerformanceState> {
	const command = _quantizeDefaultOn(
		requested,
		`performance preset ${_presetClaim?.id ?? '(released)'}`
	);
	const deck = _commandDeck(command);
	if (deck !== null) performanceCommandStatus.deck_errors[deck] = null;
	try {
		await _executeAs(command, undefined, 'app-command');
	} catch (error) {
		_persistCommandError(deck, error, command);
		throw error;
	}
	return queryPerformanceState();
}

function _enqueuePresetPhase<T>(
	id: string,
	work: (driver: PerformancePresetTransactionDriver) => Promise<T>
): Promise<T> {
	const statusGeneration = _commandStatusGeneration;
	let started = false;
	performanceCommandStatus.queued += 1;
	for (const deck of DECK_IDS) performanceCommandStatus.deck_pending[deck] += 1;
	const run = async (): Promise<T> => {
		if (statusGeneration !== _commandStatusGeneration) {
			throw _commandSessionError(_commandGeneration);
		}
		started = true;
		performanceCommandStatus.queued -= 1;
		performanceCommandStatus.active += 1;
		performanceCommandStatus.last_error = null;
		for (const deck of DECK_IDS) performanceCommandStatus.deck_errors[deck] = null;
		try {
			return await work({
				dispatch: _dispatchWithinPreset,
				query: queryPerformanceState,
				setPhase: (phase) => _setPresetPhase(id, phase)
			});
		} finally {
			if (statusGeneration === _commandStatusGeneration) {
				performanceCommandStatus.active -= 1;
				for (const deck of DECK_IDS) performanceCommandStatus.deck_pending[deck] -= 1;
			}
		}
	};
	return _commandScheduler.run(PERFORMANCE_PRESET_COMMAND_SCOPES, run).finally(() => {
		if (!started && statusGeneration === _commandStatusGeneration) {
			performanceCommandStatus.queued -= 1;
			for (const deck of DECK_IDS) performanceCommandStatus.deck_pending[deck] -= 1;
		}
	});
}

/** Bounded poll shared by the two preset settle waits below. Races a
 * timer-backed deadline against each requestAnimationFrame wait
 * (discussion_r3919293437 P2 BLOCKING): Chromium suspends rAF callbacks
 * indefinitely on a hidden or backgrounded tab while setTimeout keeps firing,
 * so a bound checked only before awaiting rAF never actually expires and can
 * leave a preset transaction - including unmount stop cleanup - pending
 * forever. Returning on the deadline rather than throwing is deliberate: the
 * caller's own assertion is what reports WHICH condition never settled. */
async function _pollUntilSettled(settled: () => boolean, deadlineMs: number): Promise<void> {
	let timedOut = false;
	const timeout = new Promise<void>((resolve) => {
		setTimeout(() => {
			timedOut = true;
			resolve();
		}, deadlineMs);
	});
	while (!timedOut) {
		if (settled()) return;
		await Promise.race([
			timeout,
			new Promise<void>((resolve) => {
				requestAnimationFrame(() => resolve());
			})
		]);
	}
}

/** A settlement's narrow [_deck] claim can detach (its own command() resolves)
 * before `widen` has even been called, and `widen`'s wide claim only
 * registers itself in the scheduler's tail map at THAT later point - if a
 * preset START/STOP claim was submitted in between, it already took over the
 * tail map for every scope the wide claim needs, so the wide claim ends up
 * waiting on the PRESET's tail while the preset itself only ever waited on
 * the narrow claim. The preset's own claim releases its tail (and lets the
 * wide claim start) BEFORE `_enqueuePresetPhase`'s await here returns, so a
 * single synchronous idleness check right after it can read the wide claim
 * as still queued/active every time - not a race, a guaranteed ordering
 * (discussion_r3919212909 P1 BLOCKING). Poll briefly for the queue to
 * actually drain, mirroring unload()'s own bounded rAF poll for a scheduler-
 * adjacent condition (audio-engine.svelte.ts), rather than reordering how
 * `widen` registers itself - that ordering is the surface responsible for
 * the last five P1s on this file's sibling. If it is still not idle once the
 * deadline passes, `_assertPresetSettled` below still throws loudly rather
 * than silently declaring ready. */
async function _awaitCommandQueueIdle(deadlineMs = 2000): Promise<void> {
	await _pollUntilSettled(() => {
		const state = queryPerformanceState();
		return !state.command_pending && state.command_queued === 0;
	}, deadlineMs);
}

/** Decks whose latest acknowledged schedule revision has not yet crossed the
 * output presentation clock - the same revision pair
 * `assertPerformancePresetPresented` reads, generalized to every deck.
 *
 * Reported for all four rather than the preset's own, because the work that
 * can install a late revision is a cross-deck Beat Sync reconciliation, and it
 * reschedules whichever followers the sync master has. */
export function performanceDecksAwaitingPresentation(state: PerformanceState): DeckId[] {
	return DECK_IDS.filter(
		(deck) =>
			state.decks[deck].transport_clock.desired_revision !==
			state.decks[deck].transport_clock.presented_revision
	);
}

/** Draining the command queue is not the same guarantee as reaching the audio
 * output. A deferred beatgrid settlement that widened only AFTER a preset
 * START claim was submitted sits BEHIND that preset in the tail map (see
 * `installScopedSyncRunner`), so it runs once START's own
 * `waitForPerformancePresetPresented` has already returned, and the cross-deck
 * phase-lock it performs acknowledges a fresh schedule revision. Waiting for
 * the queue makes that reconciliation FINISH; it does not make its revision
 * audible (discussion_r3919293425 P1 BLOCKING). AGENTS.md's preset contract
 * publishes ready only "after all decks are audible with matching
 * desired/presented revisions and an idle command queue", so wait for the
 * revision half too before asserting it. Bounded like the queue wait, but at
 * the presentation timeout performance-preset.ts already uses for the same
 * physical event: a phase-locked reschedule starts at the master's next
 * aligned boundary, which is a musical bar away, not a frame.
 *
 * Restoring the settlement's queue POSITION instead would mean claiming the
 * wide scopes at submission time - the design r3913693383 P1 BLOCKING already
 * rejected as an unacceptable latency and independence cost, and the exact
 * opposite of the narrowing r3919411682 asks for. The postcondition is the
 * surface that can hold both. */
async function _awaitPresentedScheduleRevisions(deadlineMs = 30_000): Promise<void> {
	await _pollUntilSettled(
		() => performanceDecksAwaitingPresentation(queryPerformanceState()).length === 0,
		deadlineMs
	);
}

/** The one readiness postcondition both preset phases publish against, read
 * from a single state snapshot so the two halves cannot disagree. */
function _assertPresetSettled(id: string, phase: 'start' | 'stop'): void {
	const state = queryPerformanceState();
	if (state.command_pending || state.command_queued !== 0) {
		throw new Error(
			`performance preset ${id} ${phase} returned before the command queue became idle`
		);
	}
	const awaiting = performanceDecksAwaitingPresentation(state);
	if (awaiting.length > 0) {
		throw new Error(
			`performance preset ${id} ${phase} returned with deck ${awaiting.join(', ')} holding a schedule ` +
				`revision that has not reached the audio output`
		);
	}
}

/** The compensating action a preset phase runs when its POST-work settle wait
 * fails, so a late failure cannot leave the decks playing.
 *
 * `startPerformancePreset` rolls its own failures back - hard mute, then stop
 * every deck in reverse order - but that rollback boundary closes when it
 * returns, and the two settle waits below deliberately run AFTER it, outside
 * the all-scope claim (holding the claim while waiting for the widened
 * reconciliation that needs those very scopes is the r3913492572 deadlock).
 * A timeout there therefore used to throw with playback already unmuted, and
 * the route's `_start` catch only records `routeError` - decks kept playing
 * while the lifecycle said error (discussion_r3919692501 P1 BLOCKING).
 *
 * So the transaction re-creates that boundary explicitly, in the order the
 * teardown coordinator uses: mute is the hard safety edge and is taken
 * IMMEDIATELY and un-queued, because anything queued waits behind the very
 * work that just failed to settle; the reverse-order stop then runs through a
 * fresh all-scope claim, which serializes it behind that work rather than
 * racing it. Both failures are reported together with the settle failure that
 * triggered them - none is swallowed. */
async function _compensateLateSettlement(
	id: string,
	settleError: unknown,
	compensate: (driver: PerformancePresetTransactionDriver) => Promise<unknown>
): Promise<never> {
	const rollbackErrors: unknown[] = [];
	try {
		engine.setMaster(MUTED_MASTER_VOLUME);
		mixerState.master = MUTED_MASTER_VOLUME;
	} catch (muteError) {
		rollbackErrors.push(muteError);
	}
	try {
		await _enqueuePresetPhase(id, compensate);
	} catch (stopError) {
		rollbackErrors.push(stopError);
	}
	const failure =
		rollbackErrors.length === 0
			? settleError
			: new AggregateError(
					[settleError, ...rollbackErrors],
					`performance preset ${id} did not settle and its mute/full-stop rollback failed`,
					{ cause: settleError }
				);
	_recordPresetFailure(id, failure);
	throw failure;
}

function _recordPresetFailure(id: string, error: unknown): void {
	const message = _errorMessage(error);
	if (performanceCommandStatus.last_error !== message) _persistCommandError(null, error);
	_releasePreset(id, 'error', message);
}

export async function preparePerformancePresetTransaction<T>(
	id: string,
	work: (driver: PerformancePresetTransactionDriver) => Promise<T>
): Promise<T> {
	_assertPresetId(id);
	if (_presetClaim !== null) {
		throw new Error(
			`performance preset ${_presetClaim.id} already owns the lifecycle lock at ` +
				`${performancePresetLifecycle.phase}`
		);
	}
	_presetClaim = { id };
	performancePresetLifecycle.id = id;
	performancePresetLifecycle.phase = 'queued';
	performancePresetLifecycle.active = true;
	performancePresetLifecycle.error = null;
	try {
		const result = await _enqueuePresetPhase(id, work);
		performancePresetLifecycle.phase = 'awaiting_audio';
		return result;
	} catch (error) {
		_recordPresetFailure(id, error);
		throw error;
	}
}

export async function startPerformancePresetTransaction<T>(
	id: string,
	work: (driver: PerformancePresetTransactionDriver) => Promise<T>,
	compensate: (driver: PerformancePresetTransactionDriver) => Promise<unknown>
): Promise<T> {
	_assertPresetId(id);
	if (_presetClaim?.id !== id || performancePresetLifecycle.phase !== 'awaiting_audio') {
		throw new Error(`performance preset ${id} is not prepared and awaiting audio activation`);
	}
	performancePresetLifecycle.phase = 'starting';
	let result: T;
	try {
		result = await _enqueuePresetPhase(id, work);
	} catch (error) {
		_recordPresetFailure(id, error);
		throw error;
	}
	try {
		await _awaitCommandQueueIdle();
		await _awaitPresentedScheduleRevisions();
		_assertPresetSettled(id, 'start');
	} catch (settleError) {
		await _compensateLateSettlement(id, settleError, compensate);
	}
	_releasePreset(id, 'ready', null);
	return result;
}

export async function stopPerformancePresetTransaction<T>(
	id: string,
	work: (driver: PerformancePresetTransactionDriver) => Promise<T>,
	compensate: (driver: PerformancePresetTransactionDriver) => Promise<unknown>
): Promise<T> {
	_assertPresetId(id);
	if (_presetClaim !== null) {
		throw new Error(
			`performance preset ${_presetClaim.id} already owns the lifecycle lock at ` +
				`${performancePresetLifecycle.phase}`
		);
	}
	if (performancePresetLifecycle.id !== id || performancePresetLifecycle.phase !== 'ready') {
		throw new Error(`performance preset ${id} is not ready for stop cleanup`);
	}
	_presetClaim = { id };
	performancePresetLifecycle.phase = 'queued';
	performancePresetLifecycle.active = true;
	performancePresetLifecycle.error = null;
	let result: T;
	try {
		result = await _enqueuePresetPhase(id, work);
	} catch (error) {
		_recordPresetFailure(id, error);
		throw error;
	}
	try {
		await _awaitCommandQueueIdle();
		await _awaitPresentedScheduleRevisions();
		_assertPresetSettled(id, 'stop');
	} catch (settleError) {
		await _compensateLateSettlement(id, settleError, compensate);
	}
	_releasePreset(id, 'idle', null);
	return result;
}

export function abortPreparedPerformancePreset(id: string, reason: string): void {
	_assertPresetId(id);
	if (_presetClaim?.id !== id || performancePresetLifecycle.phase !== 'awaiting_audio') return;
	const error = new Error(`performance preset ${id} aborted: ${reason}`);
	_persistCommandError(null, error);
	_releasePreset(id, 'error', _errorMessage(error));
}

/**
 * IOPIN-19: run an Audio I/O Test (panel or agent) under the dispatcher's own
 * ownership. Refused with no live route session or while a preset owns the
 * lifecycle; otherwise `work` runs inside the `headphone` scope, so a preset
 * (whose phases take that scope) and every headphone command wait for it, and
 * `stillCurrent` lets it stop once the route session tears down mid-Test.
 */
/** Each running Audio I/O Test's abort, fired when its route session ends. */
const _audioIoTestAborts = new Set<AbortController>();

export async function runAudioIoTest<T>(
	work: (stillCurrent: () => boolean, signal: AbortSignal) => Promise<T>
): Promise<T> {
	const refuse = (): void => {
		if (_activeCommandSession === null) {
			throw new Error(
				'The performance view is not running, so there is nothing to test. Open it, then test again.'
			);
		}
		if (_presetClaim !== null) {
			throw new Error(
				`Performance preset ${_presetClaim.id} owns the audio at ${performancePresetLifecycle.phase}. Test again once it finishes.`
			);
		}
	};
	refuse();
	const generation = _currentCommandSession();
	return _commandScheduler.run(['headphone'], async () => {
		refuse();
		_assertCommandSession(generation);
		const abort = new AbortController();
		_audioIoTestAborts.add(abort);
		try {
			const result = await work(() => _commandSessionIsCurrent(generation), abort.signal);
			_assertCommandSession(generation);
			return result;
		} finally {
			_audioIoTestAborts.delete(abort);
		}
	});
}

// ------------------------------------------------- Q1 quantize default on ---

/** Frames that are the dispatcher itself, never the caller worth naming. */
const _DISPATCH_FRAMES =
	/dispatchPerformanceCommand|runPerformanceCommandFromUi|_dispatchUnknown|dispatchCallerFromStack/;

/**
 * Q1-DEFAULT-ON: the first stack frame outside the dispatcher, so the WARN for
 * a reverted off names who sent it (`restoreDeckConfigFromSnapshot`, an agent
 * order's `_one`, ...). Read synchronously at dispatch entry, before any
 * await can detach the stack from its caller.
 */
export function dispatchCallerFromStack(stack: string | undefined): string {
	const frames = (stack ?? '')
		.split('\n')
		.slice(1)
		.map((line) => line.trim())
		.filter((line) => line !== '');
	return frames.find((frame) => !_DISPATCH_FRAMES.test(frame)) ?? 'unknown caller';
}

/**
 * Q1-DEFAULT-ON (the maintainer, B4): Quantize is on unless a USER turned it off. An
 * app-internal off without `by_user: true` is not applied (the deck keeps its
 * state) before it can reach either
 * engine, and the WARN names the caller. Agent commands never get here with
 * one: the order bus refuses them (422, agent-orders.ts). Paths:
 * `_dispatchUnknown` (UI, IPC bridge, agent order bus, session and rescue
 * restore) and `_dispatchWithinPreset`.
 */
function _quantizeDefaultOn(command: PerformanceCommand, caller: string): PerformanceCommand {
	if (command.type !== 'quantize' || command.enabled || command.by_user === true) return command;
	// Not applied: the deck keeps what it has, on by default or a user's own
	// off for this track (an app-internal off must not turn a user off on).
	const keep = getDeckState(command.deck).quantize_enabled;
	console.warn(
		`[quantize] CH${command.deck}: Quantize off without user provenance not applied, Quantize stays ${keep ? 'on' : 'off'} (Q1-DEFAULT-ON); caller: ${caller}`
	);
	return { type: 'quantize', deck: command.deck, enabled: keep };
}

/** True when a load on `deck` would be accepted right now: the Web Audio
 * engine's up-front refusal (deck playing, audible or mid-transport) is the
 * same one `_execute` repeats under its claim. Rust mode refuses nothing here. */
function _deckAcceptsLoadNow(deck: DeckId): boolean {
	initRustMode();
	if (rustMode.enabled) return true;
	try {
		assertDeckLoadAllowed(deck);
		return true;
	} catch {
		return false;
	}
}

/**
 * A user deck load stops a sounding library preview; AutoPlay's own load does
 * not (pin 104cf3b626fd). A refused master load, or a load the engine would
 * refuse, is not a load, so the preview keeps going.
 */
function _loadStopsPreview(command: PerformanceCommand, source: CommandSource): boolean {
	return (
		command.type === 'load' &&
		source !== 'autoplay-handoff' &&
		!(command.refuseIfMaster === true && getDeckState(command.deck).is_master) &&
		_deckAcceptsLoadNow(command.deck)
	);
}

/**
 * The library's Load button and row double-click: silence the headphone
 * preview in the same turn as the gesture, before the load command is
 * queued, so a strip click still waiting on the audio graph cannot restart
 * it afterwards. Only when the deck would accept the load now.
 */
export function silencePreviewForUserLoad(deck: DeckId): void {
	if (_deckAcceptsLoadNow(deck)) silencePreviewCue();
}

/**
 * PLAY-18: run one command with its source registered on the decks it can
 * stop, so the engine logs any stop it causes with that cause. A deckless
 * command registers nowhere, except the one that stops every deck.
 */
function _executeAs(
	command: PerformanceCommand,
	pressT0Ms: number | undefined,
	source: CommandSource
): Promise<void> {
	if (replayDispatchTakenOver(command, source)) return Promise.resolve();
	const deck = _commandDeck(command);
	const decks =
		deck !== null
			? [deck]
			: command.type === 'rescue_stop_all'
				? allDecks()
				: command.type === 'deck_move'
					? [command.from, command.to]
					: [];
	return withDeckCommandSource(decks, source, () => _execute(command, pressT0Ms));
}

/**
 * PLAY-18 (CORE, Tue 6 Oct 2026): only a USER turns AutoPlay off, the same
 * rule as Quantize. An agent COMMAND without `by_user` is refused at the order
 * bus (422, agent-orders.ts); an app-internal off reaching here (the IPC
 * bridge) is not applied, AutoPlay keeps its state, and the WARN names the caller.
 */
function _autoPlayOffNeedsUser(command: PerformanceCommand, caller: string): PerformanceCommand {
	if (command.type !== 'autoplay' || command.enabled || command.by_user === true) return command;
	const keep = uiPrefs.auto_play_enabled;
	console.warn(
		`[autoplay] AutoPlay off without user provenance not applied, AutoPlay stays ${keep ? 'on' : 'off'} (PLAY-18); caller: ${caller}`
	);
	return { type: 'autoplay', enabled: keep };
}

/**
 * Q1-DEFAULT-ON: every load starts with Quantize on. A user's off holds for
 * the track it was made on and ends here, on the next successful load.
 */
async function _quantizeOnForNewTrack(deck: DeckId): Promise<void> {
	if (getDeckState(deck).quantize_enabled) return;
	const command: PerformanceCommand = { type: 'quantize', deck, enabled: true };
	if (!(await executeInRustEngine(command, pushToast))) engine.setQuantize(deck, true);
	console.info(
		`[quantize] CH${deck}: Quantize back on for the new track (a user off holds for one track)`
	);
}

/** DECKCTL-6235: with AU on, park a fresh Web Audio load at its first memory
 * cue (else its first beat). Rust engine mode does not run this yet. */
function _autoCueNewTrack(deck: DeckId): void {
	if (!uiPrefs.deck_autocue) return;
	const st = getDeckState(deck);
	const target = autoCueTargetMs(st.anlz, st.duration_ms);
	if (target === null) return;
	engine.parkAtCue(deck, target);
	console.info(`[deck-autocue] CH${deck}: parked at ${Math.round(target)} ms`);
}

async function _dispatchUnknown(
	message: unknown,
	commandGeneration: number,
	pressT0Ms: number | undefined,
	caller: string,
	source: CommandSource,
	hold?: CoordinatedHold
): Promise<PerformanceState> {
	_assertCommandSession(commandGeneration);
	let command: PerformanceCommand;
	try {
		command = _parseCommand(message);
	} catch (error) {
		_persistCommandError(null, error);
		throw error;
	}
	command = _autoPlayOffNeedsUser(_quantizeDefaultOn(command, caller), caller);
	// STEM-77: a stem_separate that names no track (the CLI verb, an agent)
	// is bound HERE, on receipt and before any await, to the track the deck
	// holds now. Read later, after the engine routing, it could name a track a
	// load put on the deck meanwhile and enqueue a paid separation of that one
	// (Codex on #6819). A deck holding nothing refuses here for the same
	// reason: a load landing before the command runs is not what was asked.
	if (command.type === 'stem_separate' && command.stable_id === undefined) {
		const held = getDeckState(command.deck).stable_id;
		if (!held) {
			const error = new Error(`stem_separate: deck ${command.deck} has no track loaded`);
			_persistCommandError(command.deck, error, command);
			throw error;
		}
		command = { ...command, stable_id: held };
	}
	const deck = _commandDeck(command);
	_bindGridEditTarget(command);
	if (command.type === 'grid_edit') {
		const next = command;
		const merged = _gridNudges.merge(next, (queued) => _sameGridEditTarget(queued, next));
		if (merged !== null) return merged;
	}
	if (command.type === 'load') {
		onDeckLoadStart(command.deck);
	}
	if (_presetClaim !== null) {
		const error = new Error(
			`performance preset ${_presetClaim.id} owns controls at ${performancePresetLifecycle.phase}; ` +
				`command ${command.type} rejected`
		);
		_persistCommandError(deck, error);
		throw error;
	}
	if (
		rescueRestoreLocksControls() &&
		(command.type === 'play' ||
			command.type === 'load' ||
			command.type === 'unload' ||
			command.type === 'cue')
	) {
		const error = new Error(
			`rescue restore owns controls at ${rescueRestoreStatus.phase}; command ${command.type} rejected`
		);
		_persistCommandError(deck, error);
		throw error;
	}
	if (command.type === 'feedback_mark') {
		try {
			const mark = _feedbackMark(command.vote);
			await recordPerformanceFeedback(mark);
			return _completeCommand(command);
		} catch (error) {
			_persistCommandError(null, error);
			throw error;
		}
	}
	if (command.type === 'pins_show_other_users') {
		const error = new Error(
			'pins_show_other_users: not_implemented - community feature, no cloudsync channel yet'
		);
		_persistCommandError(null, error);
		throw error;
	}
	// Before the deck queue. A user load stops the headphone preview now,
	// not after AutoPlay's commands in front of it finish. AutoPlay loads
	// are ignored (pin 104cf3b626fd).
	if (_loadStopsPreview(command, source)) silencePreviewCue();
	const queueScopes = performanceCommandQueueScopes(command);
	const scopes = scopesOutsideHold(command.type, queueScopes, hold);
	if (command.type === 'fader') _deckFaderWrites[command.deck] += 1;
	if (command.type === 'beat_sync') _deckBeatSyncDispatches[command.deck] += 1;
	if (
		deck !== null &&
		command.type !== 'load' &&
		command.type !== 'unload' &&
		command.type !== 'beat_sync' &&
		queueScopes !== null &&
		queueScopes.includes(deck)
	) {
		_deckCommandDispatches[deck] += 1;
	}
	if (command.type === 'headphone_output_acquire') {
		performanceCommandStatus.active += 1;
		performanceCommandStatus.last_error = null;
		try {
			const acquired = _commandScheduler.runImmediatelyIfIdle('headphone', async () => {
				_assertCommandSession(commandGeneration);
				await _executeAs(command, pressT0Ms, source);
			});
			await acquired;
			_assertCommandSession(commandGeneration);
			return _completeCommand(command);
		} catch (error) {
			if (_commandSessionIsCurrent(commandGeneration)) _persistCommandError(null, error, command);
			throw error;
		} finally {
			if (_commandSessionIsCurrent(commandGeneration)) performanceCommandStatus.active -= 1;
		}
	}
	if (scopes === null) {
		performanceCommandStatus.last_error = null;
		if (deck !== null) performanceCommandStatus.deck_errors[deck] = null;
		try {
			_assertCommandSession(commandGeneration);
			await _executeAs(command, pressT0Ms, source);
			_assertCommandSession(commandGeneration);
			return _completeCommand(command);
		} catch (error) {
			if (_commandSessionIsCurrent(commandGeneration)) _persistCommandError(deck, error, command);
			throw error;
		}
	}
	const tempoTicket = _tempoCoalescer.mark(command.type === 'tempo', deck, scopes, 'sync');
	performanceCommandStatus.queued += 1;
	if (deck !== null) performanceCommandStatus.deck_pending[deck] += 1;
	if (deck !== null && (command.type === 'load' || command.type === 'unload'))
		_deckLoadDispatches[deck] += 1;
	let started = false;

	const run = async (): Promise<PerformanceState> => {
		_assertCommandSession(commandGeneration);
		started = true;
		if (command.type === 'grid_edit') _gridNudges.started(command);
		performanceCommandStatus.queued -= 1;
		performanceCommandStatus.active += 1;
		performanceCommandStatus.last_error = null;
		if (deck !== null) performanceCommandStatus.deck_errors[deck] = null;
		try {
			// Q1: this body starts only AFTER the scope wait above, which is
			// exactly the gap press_to_schedule_ms exists to expose.
			if (_tempoCoalescer.runs(deck, tempoTicket)) {
				await _executeAs(command, pressT0Ms, source);
			}
			_assertCommandSession(commandGeneration);
			return _completeCommand(command);
		} catch (error) {
			if (_commandSessionIsCurrent(commandGeneration)) _persistCommandError(deck, error, command);
			throw error;
		} finally {
			if (_commandSessionIsCurrent(commandGeneration)) {
				performanceCommandStatus.active -= 1;
				if (deck !== null) performanceCommandStatus.deck_pending[deck] -= 1;
			}
		}
	};

	const settled = _commandScheduler.run(scopes, run).finally(() => {
		if (!started && _commandSessionIsCurrent(commandGeneration)) {
			performanceCommandStatus.queued -= 1;
			if (deck !== null) performanceCommandStatus.deck_pending[deck] -= 1;
		}
	});
	if (command.type === 'grid_edit') _gridNudges.queued(command, settled);
	if (command.type !== 'load') return settled;
	// DECKUX-37: a load under Beat Sync Max queues that deck's own beat_sync
	// command AFTER the load releases its scope (inside it, it would deadlock).
	const loadedDeck = command.deck;
	return settled.then(async () => {
		await enableBeatSyncAfterLoad(loadedDeck);
		return queryPerformanceState();
	});
}

/**
 * `source` (PLAY-18) says who is asking, so a stop this command causes is
 * logged with its cause: AutoPlay passes `autoplay-handoff`, the agent order
 * executor `agent-command`, the UI boundary `user-ui`. Anything else is an
 * `app-command`. `replay` (SETREC-12067) is the replay player; a replay command for a
 * control someone has taken over is dropped here, and any other source touching a
 * control marks it live.
 */
export async function dispatchPerformanceCommand(
	command: PerformanceCommand,
	pressT0Ms?: number,
	source: CommandSource = 'app-command',
	hold?: CoordinatedHold
): Promise<PerformanceState> {
	if (replayDispatchGate(command, source) === 'drop') return queryPerformanceState();
	return _dispatchUnknown(
		command,
		_currentCommandSession(),
		pressT0Ms,
		dispatchCallerFromStack(new Error().stack),
		source,
		hold
	);
}

/**
 * CMDK-19 (AGENTS.md: operations that coordinate master and followers claim
 * the shared sync scope): run `work` holding these decks plus 'sync' for its
 * whole lifetime, so no seek, tempo or play from MIDI, IPC or AutoPlay reaches
 * them between its steps. `work` sends its own commands through the run
 * function it is given; they run inside the hold, through the full dispatcher.
 */
export async function runCoordinatedFromUi(
	decks: readonly DeckId[],
	work: (run: (command: PerformanceCommand) => Promise<PerformanceUiCommandResult>) => Promise<void>
): Promise<void> {
	const hold: CoordinatedHold = { scopes: new Set<CommandScope>([...decks, 'sync']), open: true };
	try {
		await _commandScheduler.run([...hold.scopes], () =>
			work((command) => runPerformanceCommandFromUi(command, performance.now(), hold))
		);
	} finally {
		hold.open = false;
	}
}

/**
 * UI event boundary: await the same fail-fast dispatcher and turn its
 * rejection into a result, never into silence. A failure the dispatcher did
 * not persist (it skips its report once the session is no longer current) is
 * persisted here, so no path out of this function drops an error unseen.
 *
 * Q1 / S2: this is where the press clock starts. The default is taken on ENTRY,
 * before `_dispatchUnknown` can park the command behind another scope's tail,
 * so `press_to_schedule_ms` contains that wait rather than starting after it.
 * A caller holding the originating DOM event should pass `event.timeStamp`,
 * which is already on the `performance.now()` epoch and is earlier still.
 */
export async function runPerformanceCommandFromUi(
	command: PerformanceCommand,
	pressT0Ms: number = performance.now(),
	hold?: CoordinatedHold,
	source: 'user-ui' | 'agent-command' = 'user-ui'
): Promise<PerformanceUiCommandResult> {
	try {
		await dispatchPerformanceCommand(command, pressT0Ms, source, hold);
		return { ok: true };
	} catch (error) {
		if (error instanceof ScopedCommandInvalidatedError) {
			console.debug(
				`[performance-ipc] ${command.type} not run, no command session: ${error.message}`
			);
			if (command.type === 'load') {
				recordDeckLoadRefusal(command.deck, _errorMessage(error));
			}
			return { ok: false, reason: 'no_session', error };
		}
		const reported =
			typeof error === 'object' && error !== null ? _persistedCommandErrors.get(error) : undefined;
		if (reported === undefined) {
			// Toast plus its console.error row: the same channel as every
			// other command failure.
			_persistCommandError(null, error);
		} else if (reported === 'banner_only') {
			console.error(`[performance-ipc] ${command.type} failed`, error);
		} else if (reported === 'toasted') {
			// Already on screen and in the console; a second report would
			// only double the toast and the forwarded log row.
		}
		return { ok: false, reason: 'failed', error };
	}
}

/** Toast ids arrive over an untyped bridge, so they are validated like every
 * other IPC input rather than trusted into a store lookup. */
function _toastId(id: unknown): string {
	if (typeof id !== 'string' || id === '') {
		throw new TypeError('toast id must be a non-empty string');
	}
	return id;
}

function _captureUnknown(deck: unknown): DeckAudioSnapshot {
	return copyDeckAudioSnapshot(engine.captureDeckAudio(_deck(deck)));
}

async function applyWindowCommand(action: WindowAction, label: WindowLabel): Promise<void> {
	await controlShellWindow(action, label);
	// Only a bridge call that resolved changes the set; a refusal throws above.
	if (action === 'open') {
		if (!_openShellWindows.includes(label)) _openShellWindows = [..._openShellWindows, label];
	} else if (action === 'close') {
		_openShellWindows = _openShellWindows.filter((open) => open !== label);
	}
}

/** Test hook: parse one performance command message. */
export function parsePerformanceCommandForTest(message: unknown): PerformanceCommand {
	return _parseCommand(message);
}

/**
 * Installs whose page has unmounted but whose cleanup still waits on the plan
 * runner. A remount may take them over; a live install still refuses a second.
 */
const _retiringPerformanceIpcs = new WeakSet<object>();
/** Installs a remount has taken over, so their late cleanup is a no-op. */
const _supersededPerformanceIpcs = new WeakSet<object>();
/** Each install's Beat Sync Max enabler cleanup, run once by whichever comes first. */
const _beatSyncMaxEnablerUninstalls = new WeakMap<object, () => void>();

/** Called on unmount, before the deferred cleanup: the install may now be taken over. */
export function retirePerformanceBrowserIpc(): void {
	for (const host of _performanceIpcHosts()) {
		if (host.musicDjToolsPerformance !== undefined) {
			_retiringPerformanceIpcs.add(host.musicDjToolsPerformance);
		}
	}
}

let _mountGeneration = 0;

/** Called synchronously at mount, before any await, so a retiring page sees a newer mount even while its hydration is pending. */
export function claimPerformanceMount(): number {
	_mountGeneration += 1;
	return _mountGeneration;
}

/** The generation of the newest mount; a page whose own claim is still current owns the engine. */
export function currentPerformanceMount(): number {
	return _mountGeneration;
}

export function installPerformanceBrowserIpc(): () => void {
	const hosts = _performanceIpcHosts();
	for (const host of hosts) {
		const existing = host.musicDjToolsPerformance;
		if (existing !== undefined && !_retiringPerformanceIpcs.has(existing)) {
			throw new Error('performance IPC is already installed');
		}
	}
	let tookOver = false;
	for (const host of hosts) {
		const existing = host.musicDjToolsPerformance;
		if (existing !== undefined) {
			_supersededPerformanceIpcs.add(existing);
			_beatSyncMaxEnablerUninstalls.get(existing)?.();
			_beatSyncMaxEnablerUninstalls.delete(existing);
			tookOver = true;
		}
	}
	// The retiring session ends here, so the new one can start its own.
	if (tookOver && _activeCommandSession !== null) {
		_invalidateCommandSession(_activeCommandSession.generation);
	}
	const commandGeneration = _startCommandSession();
	const ipc: PerformanceBrowserIpc = Object.freeze({
		version: 1 as const,
		dispatch: (message: unknown, pressT0Ms?: number) =>
			_dispatchUnknown(
				message,
				commandGeneration,
				_validatedPressStamp(pressT0Ms),
				'window.musicDjToolsPerformance.dispatch (IPC bridge)',
				'agent-command'
			),
		query: () => {
			_assertCommandSession(commandGeneration);
			return queryPerformanceState();
		},
		capture: (deck: unknown) => {
			_assertCommandSession(commandGeneration);
			return _captureUnknown(deck);
		},
		dismissDeckError: (deck: unknown) => {
			_assertCommandSession(commandGeneration);
			dismissPerformanceDeckError(_deck(deck));
			return queryPerformanceState();
		},
		toasts: () =>
			toasts.map((toast) => ({
				id: toast.logId,
				kind: toast.kind,
				message: toast.message,
				headline: toast.headline,
				detail: toast.detail,
				classification: toast.classification,
				settings_summary: toast.settingsSummary,
				exiting: toast.exiting === true,
				expanded: toast.expanded === true,
				count: toast.count,
				created_at: toast.createdAt,
				timer_armed: toastTimerArmed(toast.logId)
			})),
		dismissToast: (id: unknown) => dismissToast(_toastId(id)),
		holdToast: (id: unknown) => holdToast(_toastId(id)),
		releaseToast: (id: unknown) => releaseToast(_toastId(id)),
		copyToast: (id: unknown) => copyToast(_toastId(id)),
		drillGraphRebuild: async (input: unknown) => {
			_assertCommandSession(commandGeneration);
			const failures = (input as { fail_add_module?: unknown } | null)?.fail_add_module;
			if (typeof failures !== 'number' || !Number.isInteger(failures) || failures < 0) {
				throw new Error(
					`drillGraphRebuild: fail_add_module must be an integer >= 0, got ${String(failures)}`
				);
			}
			let error: string | null = null;
			await drillGraphRebuild({ fail_add_module: failures }).catch((cause: unknown) => {
				error = cause instanceof Error ? cause.message : String(cause);
			});
			return { reattaching: reattachingDecks(), error, state: queryPerformanceState() };
		},
		reattachingDecks: () => reattachingDecks()
	});
	for (const host of hosts) {
		host.musicDjToolsPerformance = ipc;
	}
	const uninstallBeatSyncMaxEnabler = installBeatSyncMaxEnabler({
		readDecks: () =>
			Object.fromEntries(DECK_IDS.map((id) => [id, getDeckState(id)])) as Record<DeckId, DeckState>,
		readBeatSyncMax: () => uiPrefs.beat_sync_max,
		// The deck SYNC button's own path (DECKUX-37 rule 4).
		dispatch: (command) => runPerformanceCommandFromUi(command)
	});
	_beatSyncMaxEnablerUninstalls.set(ipc, uninstallBeatSyncMaxEnabler);
	return () => {
		if (_beatSyncMaxEnablerUninstalls.delete(ipc)) uninstallBeatSyncMaxEnabler();
		for (const host of hosts) {
			if (host.musicDjToolsPerformance !== ipc) {
				// A remount took this install over: its session is already the new one's.
				if (_supersededPerformanceIpcs.has(ipc)) return;
				throw new Error('performance IPC ownership changed before cleanup');
			}
		}
		_invalidateCommandSession(commandGeneration);
		for (const host of hosts) {
			if (host.musicDjToolsPerformance === ipc) {
				delete host.musicDjToolsPerformance;
			}
		}
	};
}

/** Every deck as the TOPVIEW-05 track info rules read it (track-info-state.svelte.ts). */
function _trackInfoDecks(): TrackInfoDeck[] {
	return DECK_IDS.map((id) => {
		const deck = getDeckState(id);
		return {
			id,
			loaded: deck.stable_id !== null,
			isMaster: deck.is_master,
			stableId: deck.stable_id
		};
	});
}

/**
 * Re-exported for Trackify (PERFMODE-15): performance-ipc.svelte.ts is
 * already a stores.svelte importer, so routing pushToast through here keeps
 * the frontend.max_fan_in count on stores.svelte from growing when a new
 * consumer needs it ((private path) precedent).
 */
export { pushToast } from '$lib/stores.svelte';
