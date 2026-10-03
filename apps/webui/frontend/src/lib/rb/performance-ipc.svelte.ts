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

import { assertHeadDelayMs } from '$lib/player/constants';
import {
	copyToast,
	dismissToast,
	holdToast,
	pushToast,
	releaseToast,
	toasts,
	toastTimerArmed
} from '$lib/stores.svelte';
import { clearHotCue, restoreHotCue, saveHotCue } from '$lib/rb/api-rb';
import {
	analysisSourceState,
	installAnalysisSourceRefreshRunner,
	setAnalysisSource,
	type AnalysisSource,
	type AnalysisSourceFeature
} from '$lib/rb/analysis-source.svelte';
import { createPairing } from '$lib/api';
import { hasTrustedBeatGrid } from '$lib/player/grid-features';
import { nextDownbeatAtOrAfter, planHotCueTrigger, quantizeToNearestDownbeat, type ArmAtPosition } from '$lib/rb/beat-sync-math';
import { planWaveformSeek, type WaveformSeekSnap } from '$lib/rb/plan-waveform-seek';
import type { AnlzBeat } from '$lib/rb/anlz-types';
import { bootScheduler } from '$lib/rb/boot-scheduler';
import {
	DECK_IDS,
	deckEffectiveBpm,
	deckTransportClock,
	engine,
	getDeckState,
	getMasterMode,
	getMasterReason,
	installScopedSyncRunner,
	isMasterMuted,
	keySyncPreview,
	mixerState,
	pitchRanges,
	setMasterMuted,
	type DeckTransportClock,
	type PitchRange
} from '$lib/rb/audio-engine.svelte';
import { executeInRustEngine } from '$lib/audio-engine/rust-mode.svelte';
import { deckFacingMessage } from '$lib/rb/deck-load-context';
import type { MasterMode, MasterReason } from '$lib/rb/audio-engine-types';
import { readTransition } from './transition-read.svelte';
import type { TransitionStatus } from './transition-classifier';
import {
	assertLoopGridBase,
	loopIntervalChoices,
	loopIntervalView,
	setLoopIntervalBase,
	setLoopIntervalMode
} from '$lib/rb/loop-interval-view.svelte';
import {
	ScopedCommandInvalidatedError,
	ScopedCommandScheduler
} from '$lib/rb/performance-command-scheduler';
import type { WidenScope } from '$lib/player/scoped-sync-runner';
import {
	markArmedHotCuePress,
	pendingLoadPlayState,
	setPendingLoadPlayIntent,
	type DeckId
} from '$lib/rb/deck-slots';
import {
	setLibraryPanelCollapsed,
	setShowStems,
	setWaveformDesign,
	type LibraryPanel
} from '$lib/rb/prefs.svelte';
import { parseWaveformDesign, type WaveformDesign } from '$lib/rb/waveform-design';
import { copyDeckAudioSnapshot } from '$lib/rb/deck-audio-snapshot';
import type {
	DeckAudioSnapshot,
	DeckState,
	LoopState,
	QuantizeGrid,
	SafetyLoopSlot,
	SyncMode
} from '$lib/rb/deck-state-types';
import type { HotCue, HotCueSlot } from '$lib/rb/hot-cue-types';
import type {
	CrossfaderAssign,
	EqBand,
	HeadphoneAlignmentMode,
	HeadphoneOutputMode,
	HeadphoneState,
	MixerChannelState
} from '$lib/rb/mixer-types';
import type { StemControl, StemDeckState } from '$lib/rb/stem-types';
import { assertHeadphoneOutputMode } from '$lib/player/headphones';
import { assertHeadphoneAlignmentMode, assertMasterDelayMs } from '$lib/player/constants';
import { abortCueAlignment, startCueAlignment } from '$lib/rb/cue-align-session.svelte';
import type { SortKey } from '$lib/components/rb/browser/browser-sort-ipc';
import { MUTED_MASTER_VOLUME, type PerformancePresetPhase } from '$lib/rb/performance-preset-constants';
import { rescueRestoreStatus } from '$lib/rb/performance-rescue-restore.svelte';
import { onDeckLoadStart } from '$lib/rb/mixer-selection.svelte';
import { uiPrefs } from '$lib/rb/prefs.svelte';
export { uiPrefs };
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
} from '$lib/rb/vibe.svelte';
import {
	previewCacheStats,
	previewCue,
	previewCueSeek,
	stopPreviewCue
} from '$lib/player/preview-cue.svelte';

/** HTTP-mirrored headphone controls (CUEOUT-04). Acquire stays on
 *  PerformanceCommand only: it needs a visible user gesture. */
export type HeadphoneCommand =
	| { type: 'channel_cue'; deck: DeckId; enabled: boolean }
	| { type: 'headphone_mix'; value: number }
	| { type: 'headphone_level'; value: number }
	| { type: 'head_delay_ms'; value: number }
	| { type: 'headphone_outputs_refresh' }
	| { type: 'headphone_output_select'; device_id: string }
	| { type: 'headphone_master_select'; device_id: string }
	| { type: 'headphone_input_select'; device_id: string }
	| { type: 'output_mode'; mode: HeadphoneOutputMode }
	| { type: 'headphone_alignment_mode'; value: HeadphoneAlignmentMode }
	| { type: 'master_delay_ms'; value: number }
	| { type: 'headphone_calibrate'; interactive?: boolean }
	| { type: 'headphone_calibrate_abort' }
	// CUEOUT-15: the library preview is a cue-bus voice, so it mirrors here
	// with the rest of the cue controls rather than beside the deck transport.
	| { type: 'preview_cue'; stable_id: string; ratio: number; bpm?: number }
	| { type: 'preview_stop' };

export type PerformanceCommand =
	// refuseIfMaster: opt-in, checked live inside _execute rather than at the
	// UI dispatch boundary - see the 'load'/'unload' branches of _execute.
	// Deliberately NOT the default: a standalone eject (Deck.svelte's Unload
	// button, Quick Draw's unload action) must still be able to unload the
	// live master with no other deck to reassign to (r3920297846) - only a
	// destructive REPLACE (BrowserPanel's _loadOntoDeck) opts in.
	| {
			type: 'load';
			deck: DeckId;
			stable_id: string;
			refuseIfMaster?: boolean;
			stems?: boolean;
			// Caller shows its own failure toast (Trackify skip): mutes this dispatcher's
			// toast AND the engine's (#4036); deck_errors and the server report remain.
			suppressCommandErrorToast?: boolean;
	  }
	| { type: 'load_play_intent'; deck: DeckId; generation: number; desired_play: boolean }
	| { type: 'unload'; deck: DeckId; refuseIfMaster?: boolean }
	| {
			type: 'play';
			deck: DeckId;
			playing: boolean;
			quantize?: boolean;
			start_at_context_sec?: number;
	  }
	| { type: 'cue'; deck: DeckId }
	| { type: 'seek'; deck: DeckId; position_ms: number }
	| { type: 'waveform_seek'; deck: DeckId; position_ms: number; snap: WaveformSeekSnap }
	| { type: 'set_waveform_design'; design: WaveformDesign }
	| { type: 'loop'; deck: DeckId; loop: { in_ms: number; out_ms: number } | null }
	| { type: 'beat_loop'; deck: DeckId; beats: number; start_ms?: number }
	| { type: 'beat_jump'; deck: DeckId; beats: number }
	| { type: 'loop_interval_mode'; deck: DeckId; enabled: boolean }
	| { type: 'loop_interval_base'; deck: DeckId; base: number }
	| { type: 'tempo'; deck: DeckId; ratio: number }
	| { type: 'pitch_range'; deck: DeckId; range: PitchRange }
	| { type: 'quantize'; deck: DeckId; enabled: boolean }
	// Pin a67bafbfc4b0: the quantize GRID, separate from the on/off toggle
	// above. 1/4/8 are real and change engine.setQuantizeGrid; 'phase'
	// (match to the detected phase length) is explicitly NOT implemented -
	// _dispatchUnknown rejects it before it can queue, same shape as
	// auto_play_two_track's not_implemented rejection.
	| { type: 'quantize_grid'; deck: DeckId; beats: 1 | 4 | 8 | 'phase' }
	| { type: 'beat_sync'; deck: DeckId; enabled: boolean }
	| { type: 'sync_mode'; deck: DeckId; mode: SyncMode }
	| { type: 'master'; deck: DeckId; lock?: boolean }
	| { type: 'master_tempo'; deck: DeckId; enabled: boolean }
	| { type: 'stem_mute'; deck: DeckId; stem: StemControl; muted: boolean }
	| { type: 'stem_solo'; deck: DeckId; stem: StemControl; solo: boolean }
	| { type: 'stem_eq_mode'; deck: DeckId; enabled: boolean }
	| { type: 'stem_gain'; deck: DeckId; stem: StemControl; value: number }
	| { type: 'slip'; deck: DeckId; enabled: boolean }
	| { type: 'key_sync'; deck: DeckId; enabled: boolean }
	| { type: 'key_nudge'; deck: DeckId; semitones: -1 | 1 }
	| { type: 'trim'; deck: DeckId; value: number }
	| { type: 'eq'; deck: DeckId; band: EqBand; value: number }
	| { type: 'filter'; deck: DeckId; value: number }
	| { type: 'fader'; deck: DeckId; value: number }
	| { type: 'assign'; deck: DeckId; assign: CrossfaderAssign }
	| { type: 'channel_cue'; deck: DeckId; enabled: boolean }
	| { type: 'crossfader'; value: number }
	| { type: 'master_volume'; value: number }
	| { type: 'headphone_mix'; value: number }
	| { type: 'headphone_level'; value: number }
	| { type: 'head_delay_ms'; value: number }
	| { type: 'master_mute'; muted: boolean; persist?: boolean }
	| { type: 'browser_select_playlist'; playlist_id: string }
	| { type: 'headphone_outputs_refresh' }
	| { type: 'headphone_output_acquire' }
	| { type: 'headphone_output_select'; device_id: string }
	| { type: 'headphone_master_select'; device_id: string }
	| { type: 'headphone_input_select'; device_id: string }
	| { type: 'preview_cue'; stable_id: string; ratio: number; bpm?: number }
	| { type: 'preview_stop' }
	| { type: 'output_mode'; mode: HeadphoneOutputMode }
	| { type: 'headphone_alignment_mode'; value: HeadphoneAlignmentMode }
	| { type: 'master_delay_ms'; value: number }
	| { type: 'headphone_calibrate'; interactive?: boolean }
	| { type: 'headphone_calibrate_abort' }
	| { type: 'analysis_source'; feature: AnalysisSourceFeature; source: AnalysisSource }
	/** UI contract only: no automatic second-track selection or mixing exists yet. */
	| { type: 'auto_play_two_track' }
	/** Pin fc60002b81a8: early next-track transition trigger, the ">|" split
	 * of the AutoPlay button. Real (not stubbed) - see auto-play-next.ts /
	 * auto-play-next.svelte.ts for the approximate loop/duck/cut it arms. */
	| { type: 'auto_play_next_arm' }
	| { type: 'auto_play_next_cancel' }
	/** UI contract only: the "show other users' pins" toggle (pin 88e3abec02a0)
	 * is stubbed - community comment-pin sync has no cloudsync channel yet. */
	| { type: 'pins_show_other_users' }
	| { type: 'library_panels'; panel: LibraryPanel; collapsed: boolean }
	| { type: 'show_stems'; enabled: boolean }
	| { type: 'feedback_mark'; vote: 'bad' | 'good' | 'great' }
	| { type: 'safety_loop_save'; deck: DeckId }
	| { type: 'safety_loop_arm'; deck: DeckId; armed: boolean }
	| { type: 'safety_loop_clear'; deck: DeckId }
	| { type: 'hot_cue_save'; deck: DeckId; slot: HotCueSlot; in_ms: number; revision: string; comment?: string | null }
	| { type: 'hot_cue_clear'; deck: DeckId; slot: HotCueSlot; revision: string }
	| { type: 'hot_cue_restore'; deck: DeckId; slot: HotCueSlot; revision: string; reversal_id: string }
	/** #884: the pad-press/MIDI trigger entry point, distinct from hot_cue_save
	 * (which persists a NEW position). BeatSyncMax may arm rather than jump
	 * immediately - see the module docstring and `hot_cue_armed` in query(). */
	| { type: 'hot_cue_trigger'; deck: DeckId; slot: HotCueSlot }
	// LIBUX-05 "technically-working mode": UI-only overlay state, no engine
	// write to serialize, but still a real UI action - every one of these has
	// a cmd+R / Opt / cmd+E keyboard equivalent in technically-working-hotkeys.ts,
	// so agent-native parity requires the same commands here.
	| { type: 'tech_mode_toggle' }
	| { type: 'tech_mode_peek'; peeking: boolean }
	| { type: 'tech_mode_opt_reveal'; revealed: boolean }
	| { type: 'tech_mode_eq_raised'; raised: boolean }
	| { type: 'tech_mode_edge_hover'; edge: EdgeRegion; hovered: boolean }
	| { type: 'pairing_snapshot_open' }
	| { type: 'pairing_snapshot_remove_eq_adjuster'; deck: DeckId; band: EqBand }
	| { type: 'pairing_snapshot_save'; from_deck: DeckId; to_deck: DeckId }
	| { type: 'playlist_undo' }
	| { type: 'playlist_redo' }
	| { type: 'rescue_resume'; decks: Array<{ deck: DeckId; position_ms: number }> }
	| { type: 'rescue_stop_all' };

export interface PerformanceDeckSnapshot {
	deck_id: DeckId;
	stable_id: string | null;
	/** Resolved source path cached at load time (RESCUE-01). */
	source_path: string | null;
	/** Track.has_rb_mapping carried onto the deck (#736); a browser/CLI agent
	 * driving hot_cue_save checks this before dispatching, the same signal
	 * HotCueBank reads to go inert-with-tooltip. */
	has_rb_mapping: boolean;
	title: string | null;
	artist: string | null;
	bpm: number | null;
	key: string | null;
	key_shift_semitones: number;
	/** Exact listener-facing target for KEY SYNC. Null means an elected,
	 * parseable master/key pair is not available yet. */
	key_sync_preview: {
		master_deck: DeckId;
		target_manual_shift_semitones: number;
		delta_semitones: number;
	} | null;
	effective_bpm: number | null;
	duration_ms: number | null;
	position_ms: number;
	playing: boolean;
	audible: boolean;
	transport_pending: boolean;
	transport_clock: DeckTransportClock;
	cue_ms: number | null;
	pitch: number;
	pitch_range: PitchRange;
	quantize_enabled: boolean;
	quantize_grid_beats: QuantizeGrid;
	beat_sync_enabled: boolean;
	key_sync_enabled: boolean;
	master_tempo_enabled: boolean;
	slip_enabled: boolean;
	slip_active: boolean;
	slip_position_ms: number | null;
	is_master: boolean;
	sync_mode: SyncMode;
	sync_error: string | null;
	processor_error: string | null;
	/** Last successful load wall ms; null until a load completes (KPI). */
	last_load_latency_ms: number | null;
	/** Monotonic successful-load commit number for durable reload observation. */
	load_generation: number;
	/** Last load stage map (ms); null until a load. CLI/IPC feedback. */
	last_load_stages: Record<string, number> | null;
	stems: StemDeckState;
	loop: LoopState | null;
	/** The saved SAFE slot is engine truth, so agents can verify a resize did
	 * not leave a stale snapshot that out-crossing engage could restore. */
	safety_loop: SafetyLoopSlot | null;
	/** Loop cluster view state, so an agent that can drive the interval grid
	 * can also read back which mode and window it landed on. */
	loop_interval: { grid_mode: boolean; grid_base: number; choices: number[] };
	beatgrid: Array<{ n: number; bpm: number; time_ms: number }>;
	beatgrid_ms: number[];
	/** Real AnlzPhrase boundaries lifted for agent phrase-time planning. */
	phrases: Array<{ start_ms: number; end_ms: number; kind: number; mood: number }>;
	hot_cue_slots: Array<{ slot: HotCueSlot; cue: HotCue | null; revision: string }>;
	hot_cue_reversal: { slot: HotCueSlot; revision: string; reversal_id: string } | null;
	/** #884: a hot_cue_trigger currently waiting for this deck's own next
	 * downbeat (BeatSyncMax, playing, unlooped); null when nothing is armed
	 * or once the deferred jump has landed. */
	hot_cue_armed: { slot: HotCueSlot; target_position_ms: number; remaining_ms: number } | null;
	/** DECKUX-21: deferred waveform seek waiting for the next downbeat. */
	waveform_seek_armed: { target_position_ms: number; remaining_ms: number } | null;
	/** LATENCY-02: QUANTIZED LAUNCH armed countdown; null once launched or cleared. */
	quantized_launch_armed: { remaining_ms: number; launch_at_context_sec: number } | null;
	command_error: string | null;
	command_pending: boolean;
	/** Last command observed for this deck, including MIDI and UI sources. */
	last_command_id: string | null;
}

export interface PerformanceState {
	version: 1;
	master_deck: DeckId | null;
	master_mode: MasterMode;
	master_reason: MasterReason;
	/** TRANS-01: dual-deck blend the TopBar pill also reads via readTransition(). */
	transition: TransitionStatus;
	command_pending: boolean;
	command_queued: number;
	load_play_intent: Record<DeckId, { generation: number; desired_play: boolean } | null>;
	decks: Record<DeckId, PerformanceDeckSnapshot>;
	mixer: {
		crossfader: number;
		master: number;
		channels: Record<DeckId, MixerChannelState>;
		headphones: HeadphoneState;
	};
	master: { muted: boolean };
	browser: {
		active_playlist: string | null;
		search: string | null;
		sort: { key: SortKey; direction: 'asc' | 'desc' } | null;
		selected_row: string | null;
	};
	history: Array<{ id: string; type: PerformanceCommand['type'] }>;
	preset: PerformancePresetLifecycleSnapshot;
	rescue_restore: {
		phase: typeof rescueRestoreStatus.phase;
		playing_deck_ids: DeckId[];
		per_deck: Record<DeckId, 'pending' | 'decoded' | 'failed'>;
		started_at_ms: number;
	};
	waveform_stutter: ReturnType<typeof waveformStutterSnapshot>;
	library_panels: { next_collapsed: boolean; recommended_collapsed: boolean };
	/** PARITY-02: the effective rbx-vs-own selection per feature, keyed the
	 * same way the `analysis_source` command names it. An agent driving this
	 * daemon has to be able to READ the state it can write, including a
	 * selection some other client PUT directly or one that survived a reload -
	 * the highlighted RBX/OWN control was the only place it appeared
	 * (discussion_r3968214027 P1 BLOCKING). Empty until the first
	 * loadAnalysisSource lands, which is "not asked yet", never a default. */
	analysis_source: Record<string, AnalysisSource>;
	/** PARITY-02: the source the loaded DECKS are actually on, which lags
	 * `analysis_source` by up to one poll and, on a switch whose deck refresh
	 * keeps failing, may never catch up. An agent that PUT a source and wants to
	 * know whether the decks followed can only read that here: the two fields
	 * disagreeing IS the split (discussion_r3970117737, discussion_r3970117741).
	 * Empty until the first refresh lands, which is "not asked yet", never a
	 * default. */
	analysis_source_decks: Record<string, AnalysisSource>;
	feedback_marks: ReturnType<typeof performanceFeedbackSummary>;
	/** CUEOUT-15: the library preview voice. An agent that can START a preview
	 * (`preview_cue`) has to be able to read whether one is running and where
	 * it is, or the only way to find out is to look at the screen. `stable_id`
	 * null means nothing is previewing; it is never a deck. */
	preview: {
		stable_id: string | null;
		playing: boolean;
		position_ms: number;
		duration_ms: number | null;
		route: 'cue' | 'main_practice' | 'split_right' | null;
		/** CUEOUT-15 R6: 1, or the tempo-match rate against the master deck. */
		rate: number;
		/** Decoded preview audio held right now, and the cap it is held under.
		 * Readable here because "how much memory is the preview holding" is a
		 * question an agent has to be able to answer without a profiler. */
		cache_tracks: number;
		cache_bytes: number;
		cache_budget_bytes: number;
	};
	last_error: string | null;
	pairing_snapshot: PairingSnapshot | null;
	technically_working: {
		active: boolean;
		peeking: boolean;
		opt_reveal_active: boolean;
		eq_raised: boolean;
		hovered_edges: EdgeRegion[];
	};
	ui: {
		show_stems: boolean;
		waveform_design: WaveformDesign;
	};
}

export interface PairingSnapshot {
	version: 1;
	beat_sync_max: boolean;
	decks: Array<{
		deck_id: DeckId;
		stable_id: string;
		title: string;
		position_ms: number;
		timestamp: { unit: 'beats' | 'time'; value: number };
		eq_adjusts: Array<{ band: EqBand; value: number }>;
	}>;
}

export interface PerformanceBrowserPaneSnapshot {
	search: string | null;
	sort: { key: SortKey; direction: 'asc' | 'desc' } | null;
	selected_row: string | null;
}

/** BrowserPanel owns playlist loading, while this module owns the public
 * command protocol. Registering the narrow adapter keeps both boundaries
 * explicit and makes a missing mounted browser fail loudly for an agent. */
export interface PerformanceBrowserAdapter {
	selectPlaylist(playlistId: string): Promise<void>;
	readSnapshot(): PerformanceBrowserPaneSnapshot;
}

/** Pin fc60002b81a8: same decoupling shape as PerformanceBrowserAdapter above
 * - auto-play-next.svelte.ts owns the real orchestration and already
 * imports dispatchPerformanceCommand from this module, so this module
 * registers rather than statically imports it back (a static import both
 * ways would be a real cycle, not just a slack-ratchet number). A missing
 * registration fails loudly, same rationale as a missing browser adapter. */
export interface AutoPlayNextController {
	arm(): Promise<boolean>;
	cancel(): Promise<void>;
}

let _autoPlayNextController: AutoPlayNextController | null = null;

export function registerAutoPlayNextController(controller: AutoPlayNextController): () => void {
	_autoPlayNextController = controller;
	return () => {
		if (_autoPlayNextController === controller) _autoPlayNextController = null;
	};
}

export interface PlaylistHistoryAdapter {
	undo(): Promise<void>;
	redo(): Promise<void>;
}

let _playlistHistoryAdapter: PlaylistHistoryAdapter | null = null;

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
	if (_browserAdapter !== null) throw new Error('performance browser adapter is already registered');
	_browserAdapter = adapter;
	return () => {
		if (_browserAdapter !== adapter) throw new Error('performance browser adapter ownership changed');
		_browserAdapter = null;
	};
}

const _EMPTY_BROWSER_PANE_SNAPSHOT: PerformanceBrowserPaneSnapshot = {
	search: null,
	sort: null,
	selected_row: null
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
		selected_row: snapshot.selected_row
	};
}

function _recordPerformanceCommand(command: PerformanceCommand): void {
	const event = { id: `pc-${++_commandSequence}`, type: command.type };
	_commandHistory = [..._commandHistory.slice(-199), event];
	const deck = _commandDeck(command);
	if (deck !== null) _deckCommandIds[deck] = event.id;
}

export type PerformancePresetLifecyclePhase =
	| 'idle'
	| 'queued'
	| PerformancePresetPhase
	| 'ready'
	| 'error';

export interface PerformancePresetLifecycleSnapshot {
	id: string | null;
	phase: PerformancePresetLifecyclePhase;
	active: boolean;
	error: string | null;
}

export interface PerformancePresetTransactionDriver {
	dispatch(command: PerformanceCommand): Promise<PerformanceState>;
	query(): PerformanceState;
	setPhase(phase: PerformancePresetPhase): void;
}

export interface PerformanceBrowserIpc {
	readonly version: 1;
	/**
	 * Q1 agent-native parity: `pressT0Ms` is the same optional input stamp the
	 * UI boundary takes, on the `performance.now()` epoch. A browser agent
	 * driving a transport command passes its own stamp and gets the same
	 * `press_to_schedule_ms` / `input_to_audible_ms` stages a human press
	 * produces; omitting it (autoplay, preset restore, any command with no
	 * input behind it) leaves those stages off the row rather than reporting a
	 * press that never happened.
	 */
	dispatch(message: unknown, pressT0Ms?: number): Promise<PerformanceState>;
	query(): PerformanceState;
	capture(deck: unknown): DeckAudioSnapshot;
	/** Agent-native twin of the deck error banner's dismiss control. */
	dismissDeckError(deck: unknown): PerformanceState;
	/**
	 * AGENT-NATIVE PARITY for the toast tray. Every pointer interaction a human
	 * has (hover to hold, x to dismiss, click to copy) has an equal here, so a
	 * browser agent can drive and assert the same flows without synthesizing
	 * pointer events.
	 *
	 * Deliberately NOT behind the command-session gate the transport methods
	 * use: a toast is app-wide state that outlives any one performance command
	 * session, and refusing to dismiss a stuck error because a preset
	 * transaction moved on would reproduce the bug this feature fixes.
	 */
	toasts(): readonly ToastIpcRow[];
	dismissToast(id: unknown): boolean;
	holdToast(id: unknown): boolean;
	releaseToast(id: unknown): boolean;
	copyToast(id: unknown): Promise<string>;
}

/** One toast as an agent sees it. `id` is the same correlation id printed on
 * screen, copied to the clipboard and written into the perf-event ring row. */
export interface ToastIpcRow {
	id: string;
	kind: 'info' | 'warn' | 'error';
	message: string;
	headline: string;
	detail?: string | undefined;
	classification?: string | undefined;
	settings_summary?: string | undefined;
	exiting?: boolean;
	expanded: boolean;
	count: number;
	created_at: string;
	/** False while a pointer (or holdToast) is holding it open. */
	timer_armed: boolean;
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

const hotCueReversals: Record<DeckId, { slot: HotCueSlot; revision: string; reversal_id: string } | null> =
	$state({ 1: null, 2: null, 3: null, 4: null });

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

export interface PerformanceQuantizedLaunchDriver {
	arm(deck: DeckId, pressT0Ms?: number): Promise<number>;
	clear(deck: DeckId): void;
	contextTimeNowSec(): number;
}

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

export interface PerformanceHotCueDriver {
	stableId(deck: DeckId): string | null;
	refresh(deck: DeckId): Promise<void>;
	/** #884: everything planHotCueTrigger needs for one slot, in one read so
	 * the test seam can stand in for the engine without a real audio graph. */
	triggerState(
		deck: DeckId,
		slot: HotCueSlot
	): {
		cue: HotCue | null;
		playing: boolean;
		loopEngaged: boolean;
		positionSec: number;
		beats: readonly AnlzBeat[];
	};
	/** Immediate jump - the same path an unquantized click always took.
	 * pressT0Ms is Q1's operator-felt press stamp. */
	jump(deck: DeckId, positionMs: number, pressT0Ms?: number): Promise<void>;
	/** Defer the jump to the deck's own next downbeat; returns the absolute
	 * AudioContext time the schedule lands at. pressT0Ms is Q1's
	 * operator-felt press stamp. A resolver `armAt` is called with the
	 * engine's live position inside the scheduling transaction. */
	arm(deck: DeckId, positionMs: number, armAt: ArmAtPosition, pressT0Ms?: number): Promise<number>;
	contextTimeNowSec(): number;
}

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
	arm: (deck, positionMs, armAt, pressT0Ms) => engine.armHotCueTrigger(deck, positionMs, armAt, pressT0Ms),
	contextTimeNowSec: () => engine.contextTimeNowSec()
};
let _hotCueDriver: PerformanceHotCueDriver = _defaultHotCueDriver;

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
type PersistenceScope = `persistence-${DeckId}`;
type CommandScope = DeckId | PersistenceScope | 'sync' | 'headphone';
const _commandScheduler = new ScopedCommandScheduler<CommandScope>();
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
installAnalysisSourceRefreshRunner((work) => _commandScheduler.run([...DECK_IDS, 'sync'], work));
let _commandGeneration = 0;
let _commandStatusGeneration = 0;
let _activeCommandSession: { generation: number } | null = null;
export const PERFORMANCE_PRESET_COMMAND_SCOPES: readonly CommandScope[] = [
	...DECK_IDS,
	...DECK_IDS.map(_persistenceScope),
	'sync',
	'headphone'
];

export const PERFORMANCE_RESCUE_COMMAND_SCOPES: readonly CommandScope[] = PERFORMANCE_PRESET_COMMAND_SCOPES;

let _rescueRestoredDecks: DeckId[] = [];

export function rescueRestoreLocksControls(): boolean {
	return (
		rescueRestoreStatus.phase === 'restoring' ||
		rescueRestoreStatus.phase === 'resuming'
	);
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

type UnknownRecord = Record<string, unknown>;

// ---------------------------------------------------------------- validation

function _record(message: unknown): UnknownRecord {
	if (typeof message !== 'object' || message === null || Array.isArray(message)) {
		throw new TypeError('performance command must be a plain object');
	}
	return message as UnknownRecord;
}

function _exactKeys(record: UnknownRecord, keys: readonly string[]): void {
	const allowed = new Set(keys);
	const unexpected = Object.keys(record).filter((key) => !allowed.has(key));
	if (unexpected.length > 0) {
		throw new TypeError(`performance command has unexpected fields: ${unexpected.join(', ')}`);
	}
}

function _deck(value: unknown): DeckId {
	if (value !== 1 && value !== 2 && value !== 3 && value !== 4) {
		throw new RangeError(`deck must be one of 1, 2, 3, 4; got ${String(value)}`);
	}
	return value;
}

function _boolean(name: string, value: unknown): boolean {
	if (typeof value !== 'boolean') throw new TypeError(`${name} must be boolean`);
	return value;
}

function _edge(value: unknown): EdgeRegion {
	if (value !== 'top' && value !== 'bottom' && value !== 'left' && value !== 'right') {
		throw new RangeError(`edge must be top, bottom, left, or right; got ${String(value)}`);
	}
	return value;
}

function _finite(name: string, value: unknown): number {
	if (typeof value !== 'number' || !Number.isFinite(value)) {
		throw new TypeError(`${name} must be a finite number`);
	}
	return value;
}

function _unit(name: string, value: unknown): number {
	const parsed = _finite(name, value);
	if (parsed < 0 || parsed > 1) throw new RangeError(`${name} must be within 0..1`);
	return parsed;
}

function _generation(value: unknown): number {
	if (typeof value !== 'number' || !Number.isSafeInteger(value) || value <= 0) {
		throw new TypeError('generation must be a positive safe integer');
	}
	return value;
}

function _stem(value: unknown): StemControl {
	if (value !== 'vocal' && value !== 'instrumental' && value !== 'drums') {
		throw new TypeError(`stem must be vocal, instrumental, or drums; got ${String(value)}`);
	}
	return value;
}

function _hotCueSlot(value: unknown): HotCueSlot {
	if (value !== 'A' && value !== 'B' && value !== 'C' && value !== 'D' &&
		value !== 'E' && value !== 'F' && value !== 'G' && value !== 'H') {
		throw new TypeError(`hot-cue slot must be A through H; got ${String(value)}`);
	}
	return value;
}

function _revision(name: string, value: unknown): string {
	if (typeof value !== 'string' || value.length === 0) {
		throw new TypeError(`${name} must be a non-empty revision`);
	}
	return value;
}

function _optionalStringOrNull(name: string, value: unknown): string | null | undefined {
	if (value === undefined || value === null) return value;
	if (typeof value !== 'string') throw new TypeError(`${name} must be a string or null`);
	return value;
}

/**
 * Q1: the press stamp crosses the browser IPC boundary like any other input,
 * so it gets the same fail-fast validation every command field gets. A string
 * or a NaN here would silently disable the two press stages on a row that
 * still looks complete, which is worse than a rejected command.
 */
function _validatedPressStamp(pressT0Ms: unknown): number | undefined {
	if (pressT0Ms === undefined) return undefined;
	if (typeof pressT0Ms !== 'number' || !Number.isFinite(pressT0Ms) || pressT0Ms < 0) {
		throw new TypeError(
			`press stamp must be a finite non-negative performance.now() reading, got ` +
				`${String(pressT0Ms)}`
		);
	}
	return pressT0Ms;
}

function _parseCommand(message: unknown): PerformanceCommand {
	const record = _record(message);
	if (typeof record.type !== 'string') throw new TypeError('performance command type must be string');
	const type = record.type;
	if (type === 'crossfader' || type === 'master_volume' || type === 'headphone_mix' || type === 'headphone_level') {
		_exactKeys(record, ['type', 'value']);
		return { type, value: _unit('value', record.value) };
	}
	if (type === 'master_mute') {
		if (record.persist === undefined) {
			_exactKeys(record, ['type', 'muted']);
			return { type, muted: _boolean('muted', record.muted) };
		}
		_exactKeys(record, ['type', 'muted', 'persist']);
		return { type, muted: _boolean('muted', record.muted), persist: _boolean('persist', record.persist) };
	}
	if (type === 'preview_cue') {
		// CUEOUT-15. `ratio` is validated here rather than clamped, because a
		// caller that sent 1.6 meant something this cannot guess; the pointer
		// path clamps because a pixel outside the strip DOES have an obvious
		// intent.
		_exactKeys(record, ['type', 'stable_id', 'ratio', 'bpm']);
		if (typeof record.stable_id !== 'string' || record.stable_id.trim() === '') {
			throw new TypeError('stable_id must be a non-empty string');
		}
		// `bpm` is the caller's own copy of the track BPM, for the CUEOUT-15 R6
		// tempo match. Optional: the pointer path always has it from the row,
		// and an agent that does not send one gets a preview at its own tempo
		// rather than a metadata request it did not ask for.
		if (record.bpm !== undefined && (typeof record.bpm !== 'number' || !(record.bpm > 0))) {
			throw new TypeError('bpm must be a positive number when given');
		}
		return {
			type,
			stable_id: record.stable_id,
			ratio: _unit('ratio', record.ratio),
			...(record.bpm === undefined ? {} : { bpm: record.bpm })
		};
	}
	if (type === 'preview_stop') {
		_exactKeys(record, ['type']);
		return { type };
	}
	if (type === 'browser_select_playlist') {
		_exactKeys(record, ['type', 'playlist_id']);
		if (typeof record.playlist_id !== 'string' || record.playlist_id.trim() === '') {
			throw new TypeError('playlist_id must be a non-empty string');
		}
		return { type, playlist_id: record.playlist_id };
	}
	if (type === 'headphone_outputs_refresh') {
		_exactKeys(record, ['type']);
		return { type };
	}
	if (type === 'headphone_output_acquire') {
		_exactKeys(record, ['type']);
		return { type };
	}
	if (type === 'headphone_output_select') {
		_exactKeys(record, ['type', 'device_id']);
		if (typeof record.device_id !== 'string' || record.device_id.trim() === '') {
			throw new TypeError('device_id must be a non-empty string');
		}
		return { type, device_id: record.device_id };
	}
	if (type === 'headphone_master_select') {
		_exactKeys(record, ['type', 'device_id']);
		if (typeof record.device_id !== 'string' || record.device_id.trim() === '') {
			throw new TypeError('device_id must be a non-empty string');
		}
		return { type, device_id: record.device_id };
	}
	if (type === 'headphone_input_select') {
		_exactKeys(record, ['type', 'device_id']);
		if (typeof record.device_id !== 'string' || record.device_id.trim() === '') {
			throw new TypeError('device_id must be a non-empty string');
		}
		return { type, device_id: record.device_id };
	}
	if (type === 'output_mode') {
		_exactKeys(record, ['type', 'mode']);
		assertHeadphoneOutputMode(record.mode);
		return { type, mode: record.mode };
	}
	if (type === 'head_delay_ms') {
		_exactKeys(record, ['type', 'value']);
		assertHeadDelayMs(record.value);
		return { type, value: record.value };
	}
	if (type === 'headphone_alignment_mode') {
		_exactKeys(record, ['type', 'value']);
		assertHeadphoneAlignmentMode(record.value);
		return { type, value: record.value };
	}
	if (type === 'master_delay_ms') {
		_exactKeys(record, ['type', 'value']);
		assertMasterDelayMs(record.value);
		return { type, value: record.value };
	}
	if (type === 'headphone_calibrate') {
		// `interactive` is the modal's flag (wait for the ear-cup step); HTTP and
		// CLI callers omit it and get the headless run.
		_exactKeys(record, ['type', 'interactive']);
		if (record.interactive === undefined) return { type };
		if (typeof record.interactive !== 'boolean') {
			throw new TypeError(`headphone_calibrate interactive must be boolean; got ${String(record.interactive)}`);
		}
		return { type, interactive: record.interactive };
	}
	if (type === 'headphone_calibrate_abort') {
		_exactKeys(record, ['type']);
		return { type };
	}
	if (type === 'analysis_source') {
		_exactKeys(record, ['type', 'feature', 'source']);
		if (record.feature !== 'beatgrid') throw new TypeError(`analysis-source feature must be beatgrid; got ${String(record.feature)}`);
		if (record.source !== 'rekordbox' && record.source !== 'own') {
			throw new TypeError(`analysis-source source must be rekordbox or own; got ${String(record.source)}`);
		}
		return { type, feature: record.feature, source: record.source };
	}
	if (type === 'auto_play_two_track') {
		_exactKeys(record, ['type']);
		return { type };
	}
	if (type === 'auto_play_next_arm' || type === 'auto_play_next_cancel') {
		_exactKeys(record, ['type']);
		return { type };
	}
	if (type === 'pins_show_other_users') {
		_exactKeys(record, ['type']);
		return { type };
	}
	if (type === 'tech_mode_toggle') {
		_exactKeys(record, ['type']);
		return { type };
	}
	if (type === 'tech_mode_peek') {
		_exactKeys(record, ['type', 'peeking']);
		return { type, peeking: _boolean('peeking', record.peeking) };
	}
	if (type === 'tech_mode_opt_reveal') {
		_exactKeys(record, ['type', 'revealed']);
		return { type, revealed: _boolean('revealed', record.revealed) };
	}
	if (type === 'tech_mode_eq_raised') {
		_exactKeys(record, ['type', 'raised']);
		return { type, raised: _boolean('raised', record.raised) };
	}
	if (type === 'tech_mode_edge_hover') {
		_exactKeys(record, ['type', 'edge', 'hovered']);
		return { type, edge: _edge(record.edge), hovered: _boolean('hovered', record.hovered) };
	}
	if (type === 'playlist_undo' || type === 'playlist_redo' || type === 'rescue_stop_all') {
		_exactKeys(record, ['type']);
		return { type };
	}
	if (type === 'rescue_resume') {
		_exactKeys(record, ['type', 'decks']);
		if (!Array.isArray(record.decks) || record.decks.length === 0) {
			throw new RangeError('rescue_resume requires at least one deck');
		}
		const decks = record.decks.map((entry) => {
			const row = _record(entry);
			_exactKeys(row, ['deck', 'position_ms']);
			const position_ms = _finite('position_ms', row.position_ms);
			if (position_ms < 0) throw new RangeError('position_ms must be >= 0');
			return { deck: _deck(row.deck), position_ms };
		});
		return { type, decks };
	}
	if (type === 'pairing_snapshot_open') {
		_exactKeys(record, ['type']);
		return { type };
	}
	if (type === 'pairing_snapshot_remove_eq_adjuster') {
		_exactKeys(record, ['type', 'deck', 'band']);
		const deck = _deck(record.deck);
		if (record.band !== 'low' && record.band !== 'mid' && record.band !== 'high') {
			throw new TypeError(`band must be low, mid, or high; got ${String(record.band)}`);
		}
		return { type, deck, band: record.band };
	}
	if (type === 'pairing_snapshot_save') {
		_exactKeys(record, ['type', 'from_deck', 'to_deck']);
		const from_deck = _deck(record.from_deck);
		const to_deck = _deck(record.to_deck);
		if (from_deck === to_deck) throw new RangeError('pairing snapshot requires two distinct decks');
		return { type, from_deck, to_deck };
	}
	if (type === 'library_panels') {
		_exactKeys(record, ['type', 'panel', 'collapsed']);
		if (record.panel !== 'next' && record.panel !== 'recommended') {
			throw new TypeError(`library panel must be next or recommended; got ${String(record.panel)}`);
		}
		return { type, panel: record.panel, collapsed: _boolean('collapsed', record.collapsed) };
	}
	if (type === 'show_stems') {
		_exactKeys(record, ['type', 'enabled']);
		return { type, enabled: _boolean('enabled', record.enabled) };
	}
	if (type === 'set_waveform_design') {
		_exactKeys(record, ['type', 'design']);
		const design = parseWaveformDesign(record.design);
		if (design === undefined) throw new TypeError('design is required');
		return { type, design };
	}
	if (type === 'feedback_mark') {
		_exactKeys(record, ['type', 'vote']);
		if (record.vote !== 'bad' && record.vote !== 'good' && record.vote !== 'great') {
			throw new TypeError(`feedback vote must be bad, good, or great; got ${String(record.vote)}`);
		}
		return { type, vote: record.vote };
	}
	const deck = _deck(record.deck);
	if (type === 'load') {
		_exactKeys(record, [
			'type',
			'deck',
			'stable_id',
			'refuseIfMaster',
			'stems',
			'suppressCommandErrorToast'
		]);
		if (typeof record.stable_id !== 'string' || record.stable_id.trim() === '') {
			throw new TypeError('stable_id must be a non-empty string');
		}
		const refuseIfMaster = record.refuseIfMaster === undefined
			? undefined
			: _boolean('refuseIfMaster', record.refuseIfMaster);
		const stems = record.stems === undefined ? undefined : _boolean('stems', record.stems);
		const suppressCommandErrorToast = record.suppressCommandErrorToast === undefined
			? undefined
			: _boolean('suppressCommandErrorToast', record.suppressCommandErrorToast);
		return {
			type,
			deck,
			stable_id: record.stable_id,
			...(refuseIfMaster === undefined ? {} : { refuseIfMaster }),
			...(stems === undefined ? {} : { stems }),
			...(suppressCommandErrorToast === undefined ? {} : { suppressCommandErrorToast })
		};
	} else if (type === 'load_play_intent') {
		_exactKeys(record, ['type', 'deck', 'generation', 'desired_play']);
		return { type, deck, generation: _generation(record.generation), desired_play: _boolean('desired_play', record.desired_play) };
	} else if (type === 'unload') {
		_exactKeys(record, ['type', 'deck', 'refuseIfMaster']);
		if (record.refuseIfMaster === undefined) return { type, deck };
		return { type, deck, refuseIfMaster: _boolean('refuseIfMaster', record.refuseIfMaster) };
	} else if (type === 'cue') {
		_exactKeys(record, ['type', 'deck']);
		return { type, deck };
	} else if (type === 'master') {
		_exactKeys(record, ['type', 'deck', 'lock']);
		if (record.lock === undefined) return { type, deck };
		return { type, deck, lock: _boolean('lock', record.lock) };
	} else if (type === 'play') {
		_exactKeys(record, ['type', 'deck', 'playing', 'quantize', 'start_at_context_sec']);
		const playing = _boolean('playing', record.playing);
		const start_at_context_sec =
			record.start_at_context_sec === undefined
				? undefined
				: _finite('start_at_context_sec', record.start_at_context_sec);
		if (start_at_context_sec !== undefined && start_at_context_sec < 0) {
			throw new RangeError('start_at_context_sec must be >= 0');
		}
		if (record.quantize === undefined && start_at_context_sec === undefined) {
			return { type, deck, playing };
		}
		return {
			type,
			deck,
			playing,
			...(record.quantize === undefined ? {} : { quantize: _boolean('quantize', record.quantize) }),
			...(start_at_context_sec === undefined ? {} : { start_at_context_sec })
		};
	} else if (type === 'safety_loop_save' || type === 'safety_loop_clear') {
		_exactKeys(record, ['type', 'deck']);
		return { type, deck };
	} else if (type === 'safety_loop_arm') {
		_exactKeys(record, ['type', 'deck', 'armed']);
		return { type, deck, armed: _boolean('armed', record.armed) };
	} else if (type === 'key_sync') {
		_exactKeys(record, ['type', 'deck', 'enabled']);
		return { type, deck, enabled: _boolean('enabled', record.enabled) };
	} else if (type === 'seek') {
		_exactKeys(record, ['type', 'deck', 'position_ms']);
		const position_ms = _finite('position_ms', record.position_ms);
		if (position_ms < 0) throw new RangeError('position_ms must be >= 0');
		return { type, deck, position_ms };
	} else if (type === 'waveform_seek') {
		_exactKeys(record, ['type', 'deck', 'position_ms', 'snap']);
		const position_ms = _finite('position_ms', record.position_ms);
		if (position_ms < 0) throw new RangeError('position_ms must be >= 0');
		const snap = record.snap;
		if (snap !== 'downbeat' && snap !== 'beat' && snap !== 'exact') {
			throw new TypeError(`waveform_seek snap must be downbeat|beat|exact; got ${String(snap)}`);
		}
		return { type, deck, position_ms, snap };
	} else if (type === 'loop') {
		_exactKeys(record, ['type', 'deck', 'loop']);
		if (record.loop === null) return { type, deck, loop: null };
		const loop = _record(record.loop);
		_exactKeys(loop, ['in_ms', 'out_ms']);
		return {
			type,
			deck,
			loop: { in_ms: _finite('loop.in_ms', loop.in_ms), out_ms: _finite('loop.out_ms', loop.out_ms) }
		};
	} else if (type === 'beat_loop') {
		_exactKeys(record, ['type', 'deck', 'beats', 'start_ms']);
		const beats = _finite('beats', record.beats);
		if (!Number.isInteger(beats) || beats <= 0) {
			throw new RangeError(`beats must be a positive integer; got ${beats}`);
		}
		if (record.start_ms === undefined) return { type, deck, beats };
		const start_ms = _finite('start_ms', record.start_ms);
		if (start_ms < 0) throw new RangeError('start_ms must be >= 0');
		return { type, deck, beats, start_ms };
	} else if (type === 'beat_jump') {
		_exactKeys(record, ['type', 'deck', 'beats']);
		const beats = _finite('beats', record.beats);
		if (!Number.isInteger(beats) || beats === 0) {
			throw new RangeError(`beats must be a non-zero integer; got ${beats}`);
		}
		return { type, deck, beats };
	} else if (type === 'loop_interval_mode') {
		_exactKeys(record, ['type', 'deck', 'enabled']);
		return { type, deck, enabled: _boolean('enabled', record.enabled) };
	} else if (type === 'loop_interval_base') {
		_exactKeys(record, ['type', 'deck', 'base']);
		const base = _finite('base', record.base);
		// assertLoopGridBase owns the bounds; parsing only guarantees a number
		// reaches it, so the legal-window rule lives in exactly one place.
		assertLoopGridBase(base);
		return { type, deck, base };
	} else if (type === 'tempo') {
		_exactKeys(record, ['type', 'deck', 'ratio']);
		return { type, deck, ratio: _finite('ratio', record.ratio) };
	} else if (type === 'pitch_range') {
		_exactKeys(record, ['type', 'deck', 'range']);
		if (record.range !== 8 && record.range !== 16 && record.range !== 100) {
			throw new RangeError(`range must be 8, 16, or 100; got ${String(record.range)}`);
		}
		return { type, deck, range: record.range };
	} else if (type === 'quantize' || type === 'beat_sync' || type === 'master_tempo' || type === 'slip' || type === 'channel_cue') {
		_exactKeys(record, ['type', 'deck', 'enabled']);
		return { type, deck, enabled: _boolean('enabled', record.enabled) };
	} else if (type === 'quantize_grid') {
		_exactKeys(record, ['type', 'deck', 'beats']);
		if (record.beats !== 1 && record.beats !== 4 && record.beats !== 8 && record.beats !== 'phase') {
			throw new RangeError(`beats must be 1, 4, 8, or "phase"; got ${String(record.beats)}`);
		}
		return { type, deck, beats: record.beats };
	} else if (type === 'stem_mute') {
		_exactKeys(record, ['type', 'deck', 'stem', 'muted']);
		return { type, deck, stem: _stem(record.stem), muted: _boolean('muted', record.muted) };
	} else if (type === 'stem_solo') {
		_exactKeys(record, ['type', 'deck', 'stem', 'solo']);
		return { type, deck, stem: _stem(record.stem), solo: _boolean('solo', record.solo) };
	} else if (type === 'stem_eq_mode') {
		_exactKeys(record, ['type', 'deck', 'enabled']);
		return { type, deck, enabled: _boolean('enabled', record.enabled) };
	} else if (type === 'stem_gain') {
		_exactKeys(record, ['type', 'deck', 'stem', 'value']);
		return { type, deck, stem: _stem(record.stem), value: _unit('value', record.value) };
	} else if (type === 'key_nudge') {
		_exactKeys(record, ['type', 'deck', 'semitones']);
		if (record.semitones !== -1 && record.semitones !== 1) {
			throw new RangeError(`semitones must be -1 or 1, got ${String(record.semitones)}`);
		}
		return { type, deck, semitones: record.semitones };
	} else if (type === 'sync_mode') {
		_exactKeys(record, ['type', 'deck', 'mode']);
		if (record.mode !== 'beat' && record.mode !== 'bar') {
			throw new TypeError(`mode must be "beat" or "bar"; got ${String(record.mode)}`);
		}
		return { type, deck, mode: record.mode };
	} else if (type === 'trim' || type === 'fader' || type === 'filter') {
		_exactKeys(record, ['type', 'deck', 'value']);
		return { type, deck, value: _unit('value', record.value) };
	} else if (type === 'eq') {
		_exactKeys(record, ['type', 'deck', 'band', 'value']);
		if (record.band !== 'low' && record.band !== 'mid' && record.band !== 'high') {
			throw new TypeError(`band must be low, mid, or high; got ${String(record.band)}`);
		}
		return { type, deck, band: record.band, value: _unit('value', record.value) };
	} else if (type === 'assign') {
		_exactKeys(record, ['type', 'deck', 'assign']);
		if (record.assign !== 'A' && record.assign !== 'B' && record.assign !== 'THRU') {
			throw new TypeError(`assign must be A, B, or THRU; got ${String(record.assign)}`);
		}
		return { type, deck, assign: record.assign };
	} else if (type === 'hot_cue_save') {
		_exactKeys(record, ['type', 'deck', 'slot', 'in_ms', 'revision', 'comment']);
		const in_ms = _finite('in_ms', record.in_ms);
		if (!Number.isInteger(in_ms) || in_ms < 0) throw new RangeError('in_ms must be a non-negative integer');
		const base: Extract<PerformanceCommand, { type: 'hot_cue_save' }> = {
			type: 'hot_cue_save',
			deck,
			slot: _hotCueSlot(record.slot),
			in_ms,
			revision: _revision('revision', record.revision)
		};
		const comment = _optionalStringOrNull('comment', record.comment);
		return comment === undefined ? base : { ...base, comment };
	} else if (type === 'hot_cue_clear') {
		_exactKeys(record, ['type', 'deck', 'slot', 'revision']);
		return { type, deck, slot: _hotCueSlot(record.slot), revision: _revision('revision', record.revision) };
	} else if (type === 'hot_cue_restore') {
		_exactKeys(record, ['type', 'deck', 'slot', 'revision', 'reversal_id']);
		return {
			type,
			deck,
			slot: _hotCueSlot(record.slot),
			revision: _revision('revision', record.revision),
			reversal_id: _revision('reversal_id', record.reversal_id)
		};
	} else if (type === 'hot_cue_trigger') {
		_exactKeys(record, ['type', 'deck', 'slot']);
		return { type, deck, slot: _hotCueSlot(record.slot) };
	}
	throw new TypeError(`unknown performance command type: ${type}`);
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
 * trusting a cached countdown, then self-clear once the schedule has landed -
 * the same "recompute, don't cache" rule `deckTransportClock` follows. */
function _waveformSeekArmedSnapshot(
	deckId: DeckId
): { target_position_ms: number; remaining_ms: number } | null {
	const armed = waveformSeekArmed[deckId];
	if (armed === null) return null;
	const remainingMs = (armed.target_context_time - _hotCueDriver.contextTimeNowSec()) * 1000;
	if (remainingMs <= 0) {
		waveformSeekArmed[deckId] = null;
		return null;
	}
	return { target_position_ms: armed.target_position_ms, remaining_ms: remainingMs };
}

function _hotCueArmedSnapshot(
	deckId: DeckId
): { slot: HotCueSlot; target_position_ms: number; remaining_ms: number } | null {
	const armed = hotCueArmed[deckId];
	if (armed === null) return null;
	const remainingMs = (armed.target_context_time - _hotCueDriver.contextTimeNowSec()) * 1000;
	if (remainingMs <= 0) {
		hotCueArmed[deckId] = null;
		return null;
	}
	return { slot: armed.slot, target_position_ms: armed.target_position_ms, remaining_ms: remainingMs };
}

function _quantizedLaunchArmedSnapshot(
	deckId: DeckId
): { remaining_ms: number; launch_at_context_sec: number } | null {
	const armed = quantizedLaunchArmed[deckId];
	if (armed === null) return null;
	const remainingMs = (armed.launch_at_context_sec - _quantizedLaunchDriver.contextTimeNowSec()) * 1000;
	if (remainingMs <= 0) {
		quantizedLaunchArmed[deckId] = null;
		return null;
	}
	return { remaining_ms: remainingMs, launch_at_context_sec: armed.launch_at_context_sec };
}

function _openPairingSnapshot(): PairingSnapshot {
	const unit: 'beats' | 'time' = uiPrefs.beat_sync_max ? 'beats' : 'time';
	const decks = DECK_IDS.flatMap((deckId) => {
		const deck = getDeckState(deckId);
		if (deck.stable_id === null) return [];
		const channel = mixerState.channels[deckId];
		const positionBeat = [...(deck.anlz?.beatgrid.beats ?? [])]
			.reverse()
			.find((beat) => beat.t * 1000 <= deck.position_ms);
		let timestampValue = deck.position_ms;
		if (unit === 'beats') {
			if (positionBeat === undefined) {
				throw new Error(`CH${deckId} has no beatgrid timestamp for pairing capture`);
			}
			timestampValue = positionBeat.n;
		}
		const adjustments: Array<{ band: EqBand; value: number }> = [
			{ band: 'low', value: channel.eq_low },
			{ band: 'mid', value: channel.eq_mid },
			{ band: 'high', value: channel.eq_high }
		];
		const eq_adjusts = adjustments.filter((adjust) => adjust.value !== 0.5);
		return [{
			deck_id: deckId,
			stable_id: deck.stable_id,
			title: deck.title ?? deck.stable_id,
			position_ms: deck.position_ms,
			timestamp: { unit, value: timestampValue },
			eq_adjusts
		}];
	});
	return { version: 1, beat_sync_max: uiPrefs.beat_sync_max, decks };
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
		beat_sync_enabled: deck.beat_sync_enabled,
		key_sync_enabled: deck.key_sync_enabled,
		master_tempo_enabled: deck.master_tempo_enabled,
		slip_enabled: deck.slip_enabled,
		slip_active: deck.slip_active,
		slip_position_ms: deck.slip_position_ms,
		is_master: deck.is_master,
		sync_mode: deck.sync_mode,
		sync_error: deck.sync_error,
		processor_error: deck.processor_error,
		last_load_latency_ms: deck.last_load_latency_ms,
		load_generation: deck.load_generation,
		last_load_stages:
			deck.last_load_stages === null ? null : { ...deck.last_load_stages },
		stems: {
			...deck.stems,
			alignment: deck.stems.alignment === null ? null : { ...deck.stems.alignment },
			controls: {
				vocal: { ...deck.stems.controls.vocal },
				instrumental: { ...deck.stems.controls.instrumental },
				drums: { ...deck.stems.controls.drums }
			}
		},
		loop: deck.loop === null ? null : { ...deck.loop },
		safety_loop: deck.safety_loop === null ? null : { ...deck.safety_loop },
		loop_interval: {
			grid_mode: loopIntervalView[deckId].gridMode,
			grid_base: loopIntervalView[deckId].gridBase,
			choices: loopIntervalChoices(loopIntervalView[deckId].gridBase)
		},
		beatgrid: beatgrid.beatgrid,
		beatgrid_ms: beatgrid.beatgrid_ms,
		phrases: deck.anlz?.phrases?.map((phrase) => ({
			start_ms: phrase.start_s * 1000,
			end_ms: phrase.end_s * 1000,
			kind: phrase.kind,
			mood: phrase.mood
		})) ?? [],
		hot_cue_slots: (['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'] as HotCueSlot[]).map((slot) => ({
			slot,
			cue: deck.hot_cues.find((cue) => cue.slot === slot) ?? null,
			revision: deck.hot_cue_revisions[slot]
		})),
		hot_cue_reversal: hotCueReversals[deckId],
		hot_cue_armed: _hotCueArmedSnapshot(deckId),
		waveform_seek_armed: _waveformSeekArmedSnapshot(deckId),
		quantized_launch_armed: _quantizedLaunchArmedSnapshot(deckId),
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
		master_deck: masterDecks[0] ?? null,
		master_mode: getMasterMode(),
		master_reason: getMasterReason(),
		transition: readTransition(),
		command_pending: performanceCommandStatus.active > 0 || performanceCommandStatus.queued > 0,
		command_queued: performanceCommandStatus.queued,
		load_play_intent: Object.fromEntries(
			Object.entries(pendingLoadPlayState()).map(([deck, pending]) => [
				deck,
				pending === null ? null : { generation: pending.generation, desired_play: pending.desiredPlay }
			])
		) as Record<DeckId, { generation: number; desired_play: boolean } | null>,
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
				outputs: mixerState.headphones.outputs.map((output) => ({ ...output })),
				inputs: mixerState.headphones.inputs.map((input) => ({ ...input }))
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
		preview: {
			stable_id: previewCue.stable_id,
			playing: previewCue.playing,
			position_ms: previewCue.position_ms,
			duration_ms: previewCue.duration_ms,
			route: previewCue.route,
			rate: previewCue.rate,
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
		technically_working: {
			active: isTechModeActive(),
			peeking: isPeeking(),
			opt_reveal_active: isOptRevealActive(),
			eq_raised: isEqRaised(),
			hovered_edges: hoveredEdgeList()
		},
		waveform_stutter: { ...waveformStutterSnapshot() },
		library_panels: {
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
		ui: {
			show_stems: uiPrefs.show_stems,
			waveform_design: uiPrefs.waveform_design
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

function _commandDeck(command: PerformanceCommand): DeckId | null {
	return 'deck' in command ? command.deck : null;
}

function _completeCommand(command: PerformanceCommand): PerformanceState {
	notifyRescueTransportEvent(command);
	return queryPerformanceState();
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
	const deck = _commandDeck(command);
	if (command.type === 'channel_cue') {
		if (deck === null) throw new Error('channel_cue has no deck command queue scope');
		return [deck];
	}
	if (
		command.type === 'trim' ||
		command.type === 'eq' ||
		command.type === 'filter' ||
		command.type === 'fader' ||
		command.type === 'stem_mute' ||
		command.type === 'stem_solo' ||
		command.type === 'assign' ||
		command.type === 'crossfader' ||
		command.type === 'master_volume' ||
		command.type === 'master_mute' ||
		command.type === 'browser_select_playlist' ||
		command.type === 'headphone_mix' ||
		command.type === 'headphone_level' ||
		// CUEOUT-14: the abort must never queue behind the calibration it stops.
		command.type === 'headphone_calibrate_abort' ||
		// CUEOUT-15: the preview owns no deck, so serializing it behind one
		// would make a library click wait on a transport it cannot touch.
		command.type === 'preview_cue' ||
		command.type === 'preview_stop' ||
		command.type === 'library_panels' ||
		command.type === 'show_stems' ||
		command.type === 'set_waveform_design' ||
		// View state only: no engine write to serialize, so queueing these
		// behind a deck's command scope would stall a control that cannot
		// conflict with anything.
		command.type === 'loop_interval_mode' ||
		command.type === 'loop_interval_base' ||
		// LIBUX-05 overlay chrome: no deck, no engine write, nothing to
		// serialize against.
		command.type === 'tech_mode_toggle' ||
		command.type === 'tech_mode_peek' ||
		command.type === 'tech_mode_opt_reveal' ||
		command.type === 'tech_mode_eq_raised' ||
		command.type === 'tech_mode_edge_hover'
		|| command.type === 'pairing_snapshot_open'
		|| command.type === 'pairing_snapshot_remove_eq_adjuster'
		|| command.type === 'pairing_snapshot_save'
		// AutoPlay Next arm/cancel dispatch their own scoped loop/eq
		// commands internally (auto-play-next.svelte.ts); this entry point
		// itself has no deck and nothing to serialize against.
		|| command.type === 'auto_play_next_arm'
		|| command.type === 'auto_play_next_cancel'
		|| command.type === 'load_play_intent'
		|| command.type === 'playlist_undo'
		|| command.type === 'playlist_redo'
		|| command.type === 'feedback_mark'
	) {
		return null;
	}
	if (deck === null) throw new Error(`${command.type} has no command queue scope`);
	if (command.type === 'hot_cue_save' || command.type === 'hot_cue_clear' || command.type === 'hot_cue_restore') {
		return [_persistenceScope(deck)];
	}
	if (
		command.type === 'play' ||
		command.type === 'cue' ||
		command.type === 'seek' ||
		command.type === 'waveform_seek' ||
		// Arming resolves as soon as the graph's pending-segment queue accepts
		// the future schedule (no timer holds this scope across the wait -
		// see armHotCueTrigger), so grouping with seek/play cannot stall.
		command.type === 'hot_cue_trigger' ||
		command.type === 'beat_jump' ||
		command.type === 'tempo' ||
		command.type === 'beat_sync' ||
		command.type === 'sync_mode' ||
		command.type === 'master' ||
		command.type === 'master_tempo' ||
		command.type === 'key_sync' ||
		command.type === 'key_nudge'
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

/**
 * Q1 / S2: `pressT0Ms` is the input stamp on the `performance.now()` epoch, cut
 * at the UI boundary BEFORE the command can queue. It travels as an argument
 * rather than as a command field on purpose: `PerformanceCommand` is the
 * validated IPC wire shape (`_exactKeys` rejects unknown fields), and an
 * instrument has no business widening a public protocol.
 */
async function _execute(command: PerformanceCommand, pressT0Ms?: number): Promise<void> {
	_recordPerformanceCommand(command);
	// Rust engine mode (opt-in, ?engine=rust): audio commands go to odj-audio
	// instead of the Web Audio engine; see lib/audio-engine/rust-mode.svelte.ts.
	if (await executeInRustEngine(command, pushToast)) return;
	if (command.type === 'load') {
		// refuseIfMaster, rechecked here inside the queued run() slot for
		// this deck's scope, not just at the UI dispatch boundary: 'master'
		// shares this deck's scope ([deck, 'sync']), so a MASTER command
		// queued moments before this load can complete BETWEEN the UI-layer
		// is_master check and this command's own turn, making the deck
		// master out from under a check that already passed (r3920224754).
		// getDeckState reads engine state directly, which is authoritative
		// by the time this command's scope has been acquired - any
		// same-scope command queued earlier is guaranteed to have already
		// settled. Opt-in only (see the PerformanceCommand union comment):
		// a standalone load must stay able to target the master, same as
		// unload below.
		if (command.refuseIfMaster === true && getDeckState(command.deck).is_master) {
			throw new Error(`cannot load: CH${command.deck} is the live master - unload or reassign it first`);
		}
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
				suppressFailureToast: command.suppressCommandErrorToast
			});
		} finally {
			deckLoadSettled();
		}
		hotCueReversals[command.deck] = null;
		hotCueArmed[command.deck] = null;
		waveformSeekArmed[command.deck] = null;
		quantizedLaunchArmed[command.deck] = null;
		noteRecentDeck(command.deck);
	} else if (command.type === 'load_play_intent') {
		// Q1: the stamp rides WITH the intent, so the play this eventually
		// becomes can time from the operator's keydown and not from the load.
		if (
			!setPendingLoadPlayIntent(command.deck, command.generation, command.desired_play, pressT0Ms)
		) {
			throw new Error(`load_play_intent generation ${command.generation} is not pending on CH${command.deck}`);
		}
	} else if (command.type === 'unload') {
		// See the 'load' branch above for why this is rechecked here rather
		// than trusted from the UI-layer check (r3920224754). Opt-in only:
		// engine.unload already supports unloading the live master (it
		// elects another playing master afterward), which a standalone
		// eject (Deck.svelte's Unload button, Quick Draw's unload action)
		// relies on, including when it is the only loaded deck and there is
		// nothing to "reassign master to" first (r3920297846). Only
		// BrowserPanel's destructive-replace path opts in.
		if (command.refuseIfMaster === true && getDeckState(command.deck).is_master) {
			throw new Error(`cannot unload: CH${command.deck} is the live master - unload or reassign it first`);
		}
		await engine.unload(command.deck);
		hotCueReversals[command.deck] = null;
		hotCueArmed[command.deck] = null;
		waveformSeekArmed[command.deck] = null;
		quantizedLaunchArmed[command.deck] = null;
	} else if (command.type === 'play') {
		if (command.playing) {
			if (command.start_at_context_sec !== undefined) {
				await engine.play(command.deck, pressT0Ms, command.start_at_context_sec);
			} else if (quantizedLaunchArmed[command.deck] !== null && command.quantize !== true) {
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
	} else if (command.type === 'seek') {
		waveformSeekArmed[command.deck] = null;
		await engine.quantizedSeek(command.deck, command.position_ms);
	} else if (command.type === 'waveform_seek') {
		const state = getDeckState(command.deck);
		const beats = state.anlz?.beatgrid.beats ?? [];
		const loopEngaged = state.loop !== null && state.loop.engaged;
		const positionSec = state.position_ms / 1000;
		const trustAnlz = state.anlz ?? {
			beatgrid: { source: 'rekordbox', beats: [...beats], beat_count: beats.length, status: 'ok' }
		};
		const plan = planWaveformSeek(
			uiPrefs.beat_sync_max && hasTrustedBeatGrid(trustAnlz),
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
		await engine.setLoop(command.deck, command.loop);
	} else if (command.type === 'beat_loop') {
		await engine.engageBeatLoop(command.deck, command.beats, command.start_ms);
	} else if (command.type === 'beat_jump') {
		await engine.beatJump(command.deck, command.beats);
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
		// 'phase' is rejected earlier in _dispatchUnknown, before this command
		// can even queue - only 1/4/8 ever reach the engine.
		if (command.beats === 'phase') {
			throw new Error('quantize_grid: not_implemented - phase reached _execute unrejected');
		}
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
	} else if (command.type === 'stem_solo') {
		engine.setStemSolo(command.deck, command.stem, command.solo, pressT0Ms);
	} else if (command.type === 'stem_eq_mode') {
		engine.setStemEqMode(command.deck, command.enabled);
	} else if (command.type === 'stem_gain') {
		engine.setStemGain(command.deck, command.stem, command.value);
	} else if (command.type === 'slip') {
		await engine.setSlip(command.deck, command.enabled);
	} else if (command.type === 'key_sync') {
		await engine.setKeySync(command.deck, command.enabled);
	} else if (command.type === 'key_nudge') {
		await engine.nudgeKey(command.deck, command.semitones);
	} else if (command.type === 'trim') {
		engine.setTrim(command.deck, command.value);
	} else if (command.type === 'eq') {
		engine.setEq(command.deck, command.band, command.value, pressT0Ms);
	} else if (command.type === 'filter') {
		engine.setFilter(command.deck, command.value, pressT0Ms);
	} else if (command.type === 'fader') {
		engine.setFader(command.deck, command.value, pressT0Ms);
		} else if (command.type === 'assign') {
		engine.assignChannel(command.deck, command.assign);
	} else if (command.type === 'channel_cue') {
		engine.setChannelCue(command.deck, command.enabled);
	} else if (command.type === 'crossfader') {
		engine.setCrossfader(command.value, pressT0Ms);
	} else if (command.type === 'master_volume') {
		engine.setMaster(command.value);
	} else if (command.type === 'master_mute') {
		// persist:false is the MCP safety rail's mute: silence this page now,
		// but never store it where another browser would start muted.
		setMasterMuted(command.muted, { persist: command.persist !== false });
	} else if (command.type === 'browser_select_playlist') {
		if (_browserAdapter === null) throw new Error('browser_select_playlist requires a mounted browser panel');
		await _browserAdapter.selectPlaylist(command.playlist_id);
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
	} else if (command.type === 'show_stems') {
		setShowStems(command.enabled);
	} else if (command.type === 'set_waveform_design') {
		setWaveformDesign(command.design);
	} else if (command.type === 'safety_loop_save') {
		// Engine-side and synchronous: it captures the deck's currently
		// engaged loop, and throws when there is none to capture.
		engine.saveSafetyLoop(command.deck);
	} else if (command.type === 'safety_loop_arm') {
		engine.setSafetyLoopArmed(command.deck, command.armed);
	} else if (command.type === 'safety_loop_clear') {
		engine.clearSafetyLoop(command.deck);
	} else if (command.type === 'hot_cue_save') {
		const stableId = _hotCueDriver.stableId(command.deck);
		if (stableId === null) throw new Error(`hot cue ${command.slot}: deck is not loaded`);
		// Untrusted own grids (static_grid_untrusted: true) must not BeatSyncMax-snap.
		const beatSyncMaxSnap =
			uiPrefs.beat_sync_max && hasTrustedBeatGrid(getDeckState(command.deck).anlz);
		const savedPositionMs = beatSyncMaxSnap
			? Math.round(
				quantizeToNearestDownbeat(
					getDeckState(command.deck).anlz?.beatgrid.beats ?? [],
					command.in_ms / 1000
				) * 1000
			)
			: command.in_ms;
		const result = await saveHotCue(
			stableId, command.slot, savedPositionMs, command.revision, command.comment
		);
		if (result.reversal === undefined) throw new Error(`hot cue ${command.slot}: server omitted reversal token`);
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
		if (result.reversal === undefined) throw new Error(`hot cue ${command.slot}: server omitted reversal token`);
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
		const deckAnlz = getDeckState(command.deck).anlz;
		const trustAnlz =
			deckAnlz ?? {
				beatgrid: {
					source: 'rekordbox',
					beats: [...beats],
					beat_count: beats.length,
					status: 'ok'
				}
			};
		const plan = planHotCueTrigger(
			uiPrefs.beat_sync_max && hasTrustedBeatGrid(trustAnlz),
			playing,
			loopEngaged,
			positionSec,
			beats
		);
		if (plan.kind === 'immediate') {
			hotCueArmed[command.deck] = null;
			waveformSeekArmed[command.deck] = null;
			await _hotCueDriver.jump(command.deck, cue.in_ms, pressT0Ms);
		} else {
			waveformSeekArmed[command.deck] = null;
			// Mark BEFORE the row can file: the eventual schedule reads this same
			// stamp via press-stamp.ts's claimArmedHotCuePress to distinguish an
			// armed (deferred-to-downbeat) wait from an immediate press row.
			if (pressT0Ms !== undefined) markArmedHotCuePress(pressT0Ms);
			const targetContextTime = await _hotCueDriver.arm(
				command.deck,
				cue.in_ms,
				plan.armAtPositionSec,
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
		throw new Error('auto_play_two_track must be rejected at the dispatch boundary');
	} else if (command.type === 'auto_play_next_arm') {
		if (_autoPlayNextController === null) {
			throw new Error('auto_play_next_arm: no AutoPlay Next controller is mounted on this route');
		}
		await _autoPlayNextController.arm();
	} else if (command.type === 'auto_play_next_cancel') {
		if (_autoPlayNextController === null) {
			throw new Error('auto_play_next_cancel: no AutoPlay Next controller is mounted on this route');
		}
		await _autoPlayNextController.cancel();
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
			decks: _pairingSnapshot.decks.map((deck) => deck.deck_id !== command.deck
				? deck
				: { ...deck, eq_adjusts: deck.eq_adjusts.filter((adjust) => adjust.band !== command.band) })
		};
	} else if (command.type === 'pairing_snapshot_save') {
		if (_pairingSnapshot === null) throw new Error('pairing snapshot is not open');
		const from = _pairingSnapshot.decks.find((deck) => deck.deck_id === command.from_deck);
		const to = _pairingSnapshot.decks.find((deck) => deck.deck_id === command.to_deck);
		if (from === undefined || to === undefined) throw new Error('selected decks are not in the pairing snapshot');
		await createPairing({
			from_stable_id: from.stable_id, to_stable_id: to.stable_id,
			snapshot: { ..._pairingSnapshot, decks: [from, to] }
		});
	} else if (command.type === 'feedback_mark') {
		throw new Error('feedback_mark must be captured at the dispatch boundary');
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
	} else if (command.type === 'rescue_resume') {
		const plans = command.decks.map((entry) => ({
			deck: entry.deck,
			positionSec: entry.position_ms / 1000
		}));
		await engine.rescueResumeTogether(plans);
		_rescueRestoredDecks = command.decks.map((entry) => entry.deck);
	} else if (command.type === 'rescue_stop_all') {
		const decks = _rescueRestoredDecks.length > 0 ? _rescueRestoredDecks : DECK_IDS.filter(
			(deckId) => getDeckState(deckId).playing
		);
		await engine.rescueStopAllTogether(decks);
		_rescueRestoredDecks = [];
	} else {
		const _exhaustive: never = command;
		throw new Error(`Unhandled performance command: ${JSON.stringify(_exhaustive)}`);
	}
}

function _persistCommandError(
	deck: DeckId | null, error: unknown, command?: PerformanceCommand
): void {
	const messageText = _errorMessage(error);
	performanceCommandStatus.last_error = messageText;
	if (deck !== null) performanceCommandStatus.deck_errors[deck] = messageText;
	if (
		(command !== undefined &&
			command.type === 'load' &&
			command.suppressCommandErrorToast === true) ||
		// A worded load failure already raised its own deck-load toast (CLOUDSYNC-33).
		deckFacingMessage(error) !== undefined
	) {
		return;
	}
	let subcontrol = '';
	if (command !== undefined && 'band' in command) subcontrol = command.band;
	else if (command !== undefined && 'stem' in command) subcontrol = command.stem;
	else if (command !== undefined && 'slot' in command) subcontrol = command.slot;
	pushToast(`Performance command failed - ${messageText}`, 'error', undefined, error, {},
		command === undefined ? undefined : `performance:${deck}:${command.type}:${subcontrol}`);
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

function _invalidateCommandSession(generation: number): void {
	_assertCommandSession(generation);
	_activeCommandSession = null;
	_commandStatusGeneration += 1;
	_commandScheduler.invalidateQueued(`performance command session ${generation} was invalidated`);
	_resetCommandStatus();
	// Armed records carry a target time on the route-owned AudioContext, which
	// dies with this session: a record surviving into the next mount would be
	// measured against an uninitialised or brand-new clock (#4011 review).
	for (const deckId of DECK_IDS) {
		waveformSeekArmed[deckId] = null;
		hotCueArmed[deckId] = null;
	}
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
	performancePresetLifecycle.phase = phase;
	performancePresetLifecycle.active = false;
	performancePresetLifecycle.error = error;
	if (phase === 'idle') performancePresetLifecycle.id = null;
}

async function _dispatchWithinPreset(command: PerformanceCommand): Promise<PerformanceState> {
	const deck = _commandDeck(command);
	if (deck !== null) performanceCommandStatus.deck_errors[deck] = null;
	try {
		await _execute(command);
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
		throw new Error(`performance preset ${id} ${phase} returned before the command queue became idle`);
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

async function _dispatchUnknown(
	message: unknown,
	commandGeneration: number,
	pressT0Ms?: number
): Promise<PerformanceState> {
	_assertCommandSession(commandGeneration);
	let command: PerformanceCommand;
	try {
		command = _parseCommand(message);
	} catch (error) {
		_persistCommandError(null, error);
		throw error;
	}
	const deck = _commandDeck(command);
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
	if (command.type === 'auto_play_two_track') {
		const error = new Error(
			'auto_play_two_track: not_implemented - second-track matching and independent rules are planned'
		);
		_persistCommandError(null, error);
		throw error;
	}
	if (command.type === 'pins_show_other_users') {
		const error = new Error(
			'pins_show_other_users: not_implemented - community feature, no cloudsync channel yet'
		);
		_persistCommandError(null, error);
		throw error;
	}
	if (command.type === 'quantize_grid' && command.beats === 'phase') {
		const error = new Error(
			'quantize_grid: not_implemented - will match quantize to the detected phase length'
		);
		_persistCommandError(command.deck, error, command);
		throw error;
	}
	const scopes = performanceCommandQueueScopes(command);
	if (command.type === 'headphone_output_acquire') {
		performanceCommandStatus.active += 1;
		performanceCommandStatus.last_error = null;
		try {
			const acquired = _commandScheduler.runImmediatelyIfIdle('headphone', async () => {
				_assertCommandSession(commandGeneration);
				await _execute(command, pressT0Ms);
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
			await _execute(command, pressT0Ms);
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
	let started = false;

	const run = async (): Promise<PerformanceState> => {
		_assertCommandSession(commandGeneration);
		started = true;
		performanceCommandStatus.queued -= 1;
		performanceCommandStatus.active += 1;
		performanceCommandStatus.last_error = null;
		if (deck !== null) performanceCommandStatus.deck_errors[deck] = null;
		try {
			// Q1: this body starts only AFTER the scope wait above, which is
			// exactly the gap press_to_schedule_ms exists to expose.
			if (_tempoCoalescer.runs(deck, tempoTicket)) {
				await _execute(command, pressT0Ms);
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

	return _commandScheduler.run(scopes, run).finally(() => {
		if (!started && _commandSessionIsCurrent(commandGeneration)) {
			performanceCommandStatus.queued -= 1;
			if (deck !== null) performanceCommandStatus.deck_pending[deck] -= 1;
		}
	});
}

export async function dispatchPerformanceCommand(
	command: PerformanceCommand,
	pressT0Ms?: number
): Promise<PerformanceState> {
	return _dispatchUnknown(command, _currentCommandSession(), pressT0Ms);
}

/**
 * UI event boundary: await the same fail-fast dispatcher, then consume the
 * rejection only after it has been persisted in visible reactive state.
 *
 * Q1 / S2: this is where the press clock starts. The default is taken on ENTRY,
 * before `_dispatchUnknown` can park the command behind another scope's tail,
 * so `press_to_schedule_ms` contains that wait rather than starting after it.
 * A caller holding the originating DOM event should pass `event.timeStamp`,
 * which is already on the `performance.now()` epoch and is earlier still.
 */
export async function runPerformanceCommandFromUi(
	command: PerformanceCommand,
	pressT0Ms: number = performance.now()
): Promise<void> {
	try {
		await dispatchPerformanceCommand(command, pressT0Ms);
	} catch {
		// The dispatcher already populated the deck alert and toast.
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

/** Test hook: parse one performance command message. */
export function parsePerformanceCommandForTest(message: unknown): PerformanceCommand {
	return _parseCommand(message);
}

export function installPerformanceBrowserIpc(): () => void {
	const hosts = _performanceIpcHosts();
	for (const host of hosts) {
		if (host.musicDjToolsPerformance !== undefined) {
			throw new Error('performance IPC is already installed');
		}
	}
	const commandGeneration = _startCommandSession();
	const ipc: PerformanceBrowserIpc = Object.freeze({
		version: 1 as const,
		dispatch: (message: unknown, pressT0Ms?: number) =>
			_dispatchUnknown(message, commandGeneration, _validatedPressStamp(pressT0Ms)),
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
		copyToast: (id: unknown) => copyToast(_toastId(id))
	});
	for (const host of hosts) {
		host.musicDjToolsPerformance = ipc;
	}
	return () => {
		for (const host of hosts) {
			if (host.musicDjToolsPerformance !== ipc) {
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

/**
 * Re-exported for Trackify (PERFMODE-15): performance-ipc.svelte.ts is
 * already a stores.svelte importer, so routing pushToast through here keeps
 * the frontend.max_fan_in count on stores.svelte from growing when a new
 * consumer needs it (.planning/debt/1141.md precedent).
 */
export { pushToast } from '$lib/stores.svelte';
