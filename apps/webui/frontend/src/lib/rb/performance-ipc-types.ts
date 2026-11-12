/**
 * Public IPC command and snapshot types for /performance, split out of
 * performance-ipc.svelte.ts. No runes and no module state here.
 */

import type { MixerPlacement } from '$lib/rb/prefs.svelte';
import type { MidiLearnSnapshot } from '$lib/rb/midi/learn-ipc';
import type { MidiTakeoverMode } from '$lib/rb/midi/takeover-policy';
import type { SortKey } from '$lib/components/rb/browser/browser-sort-ipc';
import type { MasterMode, MasterReason } from '$lib/rb/audio-engine-types';
import type { DeckTransportClock } from '$lib/rb/deck-runtime';
import type { AnlzBeat, DeckAudioSnapshot } from '$lib/rb/deck-state-types';
import type { Vocals } from '$lib/rb/api-rb';
import type { DeckId } from '$lib/rb/deck-slots';
import type { PadModeSnapshot } from './pad-mode-catalog';
import type {
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
	BoothSignalMode,
	BoothState,
	CrossfaderAssign,
	EqBand,
	HeadphoneAlignmentMode,
	HeadphoneOutputMode,
	HeadphoneState,
	MixerChannelState
} from '$lib/rb/mixer-types';
import type { StemControl, StemDeckState } from '$lib/rb/stem-types';
import type { StemSeparationSnapshot } from '$lib/rb/stem-separation-port';
import type { TransitionIntent } from '$lib/rb/auto-play-next-plan';
import type { Verdict } from '$lib/rb/autoplay-why';
import type { WaveformSeekSnap } from '$lib/rb/plan-waveform-seek';
import type { WaveformBoolOverlay } from '$lib/rb/waveform-overlay-command';
import type { WaveHeight } from '$lib/rb/wave-height';
import type { WaveGridLines } from '$lib/rb/wave-grid-lines';
import type { WaveformDesign, WaveformDesignPref } from '$lib/rb/waveform-design';
import type {
	DeckNumberArt,
	WavePlayheadMode,
	WaveZoom,
	LibraryPanel,
	PerfView
} from '$lib/rb/prefs.svelte';
import type { SkinSettings } from '$lib/rb/ui-skin';
import type { RelatedCriteriaPrefs } from '$lib/rb/related-criteria-prefs';
import type { AnalysisSource, AnalysisSourceFeature } from '$lib/rb/analysis-source-state.svelte';
import type { PitchRange } from '$lib/rb/audio-engine.svelte';
import type { MemoryCueCallDirection } from '$lib/rb/memory-cue-call';
import type { DeckMoveMode } from '$lib/rb/deck-move';
import type { GridEditIntent } from '$lib/rb/grid-edit-command';
import type {
	KeyLadderRow,
	KeyNudgeStep,
	KeySyncMode,
	KeySyncStatus
} from '$lib/player/key/camelot';
import type { TransitionStatus } from '$lib/rb/transition-classifier';
import type { BeatJumpSize } from '$lib/rb/beat-jump-size.svelte';
import type { DeckAutoGain } from '$lib/rb/auto-gain';
import type { DeckCloudDownload } from '$lib/offload/offload-ops.svelte';
import type { PerformanceFeedbackSummary } from '$lib/rb/performance-feedback';
import type { WavePaletteChoice } from '$lib/rb/wave-palette';
import type { EdgeRegion } from '$lib/rb/technically-working.svelte';
import type { LayoutRegionId, LayoutView } from '$lib/rb/layout-regions';
import type {
	LayoutOverride,
	LayoutSurface,
	LibraryLayout,
	PerformanceLayout
} from '$lib/rb/skin-layout';
import type { BeatFxBeats, BeatFxEffect, BeatFxTarget } from '$lib/rb/beat-fx';
import type { BeatFxUnitView } from '$lib/rb/beat-fx-unit';
import type { WindowLabel } from '$lib/shell/native-shell';
import type { ColorFxDeckView } from '$lib/rb/color-fx.svelte';
import type { TrackLoadResetPrefs, TrackLoadResetId } from '$lib/rb/track-load-reset';
import type { ArmAtPosition } from '$lib/rb/beat-sync-math';
import type { PerformancePresetPhase } from '$lib/rb/performance-preset-constants';
import type {
	ScopeHold,
	ScopedCommandInvalidatedError
} from '$lib/rb/performance-command-scheduler';

/** Shape of waveformStutterSnapshot() for PerformanceState (see audio-health.svelte.ts). */
export interface WaveformStutterSnapshot {
	active: boolean;
	frames: number;
	stutters: number;
	worst_gap_ms: number;
	elapsed_ms: number;
	window_ms: number;
	threshold_ms: number;
}

export type PersistenceScope = `persistence-${DeckId}`;
export type CommandScope = DeckId | PersistenceScope | 'sync' | 'headphone';

/** HTTP-mirrored headphone controls (CUEOUT-04). Acquire stays on
 *  PerformanceCommand only: it needs a visible user gesture. */
export type HeadphoneCommand =
	| { type: 'channel_cue'; deck: DeckId; enabled: boolean }
	| { type: 'headphone_mix'; value: number }
	| { type: 'headphone_level'; value: number }
	| { type: 'headphone_booth_level'; value: number }
	| { type: 'booth_level'; value: number }
	| { type: 'booth_select'; device_id: string }
	| { type: 'booth_signal_mode'; mode: BoothSignalMode }
	| { type: 'head_delay_ms'; value: number }
	| { type: 'headphone_outputs_refresh' }
	| { type: 'headphone_output_select'; device_id: string }
	| { type: 'headphone_master_select'; device_id: string }
	| { type: 'headphone_booth_select'; device_id: string }
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
			edit_id?: string;
			in_ms?: number;
			out_ms?: number;
			stems?: boolean;
			// Caller shows its own failure toast (Trackify skip): mutes this dispatcher's
			// toast AND the engine's (#4036); deck_errors and the server report remain.
			suppressCommandErrorToast?: boolean;
			// KEYSHIFT-03: a library row the compatible filter matched only after a
			// key shift loads shifted by it (integer, -3..3, 0 or absent = none).
			key_shift?: number;
			// AGENT-8332b: an agent's explicit "yes, replace the playing track".
			// The deck is ejected (pause, presented stop, unload) before the load,
			// so the replacement guard then passes. Absent or false = refuse while
			// playing, which is what every UI surface relies on.
			replace_playing?: boolean;
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
	/** CUES-10013: rekordbox CALL < / >, the previous or next memory cue relative to the playhead. */
	| { type: 'memory_cue_call'; deck: DeckId; direction: MemoryCueCallDirection }
	| { type: 'seek'; deck: DeckId; position_ms: number; exact?: boolean }
	| { type: 'waveform_seek'; deck: DeckId; position_ms: number; snap: WaveformSeekSnap }
	| { type: 'set_waveform_design'; design?: WaveformDesignPref; height?: WaveHeight }
	/** ARTNUM-9649: the deck number drawn over each deck's artwork. */
	| { type: 'set_deck_number_art'; mode: DeckNumberArt }
	/** WAVEQS-8348e: the quick-settings popover's overlay switches (waveform-overlay-command.ts). */
	| { type: 'set_waveform_overlay'; overlay: WaveformBoolOverlay; enabled: boolean }
	/** issue #10244: grid_lines takes a value, not a switch. */
	| { type: 'set_waveform_overlay'; overlay: 'grid_lines'; value: WaveGridLines }
	/** WAVEZOOM-6802: the main waveform's zoom step. deck only matters while unlinked. */
	| { type: 'set_wave_zoom'; zoom: WaveZoom; deck?: DeckId }
	/** WAVEZOOM-6802b: one step in (fewer bars) or out, as Ctrl/Cmd-wheel and the keys do. */
	| { type: 'wave_zoom_step'; direction: 'in' | 'out'; deck?: DeckId }
	/** WAVEZOOM-6802c/06: playhead behavior when zoomed out, and one zoom vs per deck. */
	| { type: 'set_wave_zoom_options'; playhead: WavePlayheadMode; linked: boolean }
	| {
			type: 'set_skin';
			ui_skin: SkinSettings['ui_skin'];
			wave_palette: SkinSettings['wave_palette'];
			wave_split_master: SkinSettings['wave_split_master'];
	  }
	| { type: 'set_layout_preset'; surface: LayoutSurface; preset: LayoutOverride[LayoutSurface] }
	/** DL-2 (#9429): agent parity for layout drag handles; validates px before prefs write (prefs clamp). */
	| { type: 'set_layout_size'; region: LayoutRegionId; px: number }
	| { type: 'reset_layout'; region?: LayoutRegionId }
	/** Optional load condition is checked inside the queue, not at input time.
	 * A stale momentary gesture is a no-op and returns the unchanged read model. */
	// quantize false engages the range as given (a stored hot-cue or memory loop, DECKCTL-6235d).
	| {
			type: 'loop';
			deck: DeckId;
			loop: { in_ms: number; out_ms: number } | null;
			quantize?: false;
			if_load_generation?: number;
	  }
	| {
			type: 'beat_loop';
			deck: DeckId;
			beats: number;
			start_ms?: number;
			if_load_generation?: number;
	  }
	| { type: 'loop_in'; deck: DeckId }
	| { type: 'loop_out'; deck: DeckId }
	| { type: 'reloop'; deck: DeckId }
	| { type: 'beat_jump'; deck: DeckId; beats: number }
	| { type: 'beat_jump_size'; deck: DeckId; beats: BeatJumpSize }
	/** DECKUX-6794: snapped jump onto the fixed bar grid; the sign is the direction, |bars| the block (4, or 8 with Shift). */
	| { type: 'grid_jump'; deck: DeckId; bars: number }
	/** DECKMOVE-01: clone deck `from` (track, playhead on the audio clock, live loop, transport, key, strip, stems)
	 * onto `to`; `move` then crossfades `crossfade_beats` (default 8, 0 cuts) and unloads `from`, `copy` leaves
	 * it playing. A playing `to` is refused unless `force`. */
	| {
			type: 'deck_move';
			from: DeckId;
			to: DeckId;
			crossfade_beats?: number;
			mode?: DeckMoveMode;
			force?: boolean;
	  }
	| { type: 'loop_interval_mode'; deck: DeckId; enabled: boolean }
	/** PADFX-11718: the deck's performance-pad mode (HOT CUE menu); Pad FX refuses with the beat FX unit's reason. */
	| { type: 'pad_mode'; deck: DeckId; mode: string }
	| { type: 'loop_interval_base'; deck: DeckId; base: number }
	| { type: 'tempo'; deck: DeckId; ratio: number }
	| { type: 'pitch_range'; deck: DeckId; range: PitchRange }
	// Q1-DEFAULT-ON: `by_user: true` is the command's provenance, set by the
	// Q button, the Quick Draw toggle, and an agent relaying a person's
	// explicit ask. Without it an `enabled: false` is not a user's, and
	// `_quantizeDefaultOn` reverts it to on at dispatch.
	| { type: 'quantize'; deck: DeckId; enabled: boolean; by_user?: boolean }
	// Pin a67bafbfc4b0: the quantize GRID, separate from the on/off toggle
	// above. 1/4/8 snap to the beat grid; DECKCTL-6235c 'phase' snaps a pause,
	// seek or cue to the nearest phrase boundary (Web Audio engine only).
	| { type: 'quantize_grid'; deck: DeckId; beats: 1 | 4 | 8 | 'phase' }
	| { type: 'beat_sync'; deck: DeckId; enabled: boolean; normalization?: 0.5 | 2; master?: DeckId }
	| { type: 'sync_mode'; deck: DeckId; mode: SyncMode }
	| { type: 'master'; deck: DeckId; lock?: boolean }
	| { type: 'master_tempo'; deck: DeckId; enabled: boolean }
	| { type: 'stem_mute'; deck: DeckId; stem: StemControl; muted: boolean }
	| { type: 'stem_solo'; deck: DeckId; stem: StemControl; solo: boolean; exclusive?: boolean }
	| { type: 'stem_eq_mode'; deck: DeckId; enabled: boolean }
	/** STEM-46/47: get this deck's stems now (retry a failed load, start a held one).
	 * STEM-77: with `stable_id`, it does nothing unless the deck still holds
	 * that track when it runs, so a probe for one track never reloads another
	 * loaded meanwhile. */
	| { type: 'stem_load'; deck: DeckId; stable_id?: string }
	/** STEM-77: enqueue separation of the loaded track on a NO STEMS deck
	 * (POST /api/v1/jobs kind stems.separate); stem_load lands the result.
	 * With `stable_id`, it is refused unless the deck still holds that track
	 * when it runs, so a click on one track never pays for another. */
	| { type: 'stem_separate'; deck: DeckId; stable_id?: string }
	| { type: 'stem_gain'; deck: DeckId; stem: StemControl; value: number }
	/** STEMPERF-11663: per-stem send into the channel's colour FX input, 0..1 linear. */
	| { type: 'stem_fx_send'; deck: DeckId; stem: StemControl; value: number }
	| { type: 'slip'; deck: DeckId; enabled: boolean }
	| { type: 'roll'; deck: DeckId; beats: number | null }
	/** KEYSYNC-01: optional mode (exact, relative, compatible) and target (a deck or 'master'); omitted keeps the deck's own. */
	| {
			type: 'key_sync';
			deck: DeckId;
			enabled: boolean;
			mode?: KeySyncMode;
			target?: DeckId | 'master';
	  }
	| { type: 'key_nudge'; deck: DeckId; semitones: number }
	/** KEYSYNC-01 ladder: an absolute key shift (-12..12, 0 = original key); turns key lock on, KEY SYNC off. */
	| { type: 'key_shift'; deck: DeckId; semitones: number }
	// GRIDEDIT-6235/-6235d: the deck's grid adjust / grid shift, one press (or a
	// reset of one half) on the stored grid edit of the track the deck holds.
	// Queued on the deck's persistence scope, as hot-cue writes are: a preset's
	// barrier waits for an edit already in flight, and transport commands on
	// the deck never wait behind its round trip.
	| { type: 'grid_edit'; deck: DeckId; edit: GridEditIntent }
	| { type: 'trim'; deck: DeckId; value: number }
	/** AUTOGAIN-04: the user's own auto gain dB for the loaded track (null = use the analysed one). */
	| { type: 'auto_gain_offset'; deck: DeckId; offset_db: number | null; stable_id: string }
	// AGENT-30: `at_context_sec` starts the step on that AudioContext time (sample-accurate), not on dispatch.
	| {
			type: 'eq';
			deck: DeckId;
			band: EqBand;
			value: number;
			at_context_sec?: number;
			step_id?: string;
	  }
	| { type: 'filter'; deck: DeckId; value: number }
	| { type: 'fader'; deck: DeckId; value: number; at_context_sec?: number; step_id?: string }
	| { type: 'assign'; deck: DeckId; assign: CrossfaderAssign }
	| { type: 'channel_cue'; deck: DeckId; enabled: boolean }
	| { type: 'crossfader'; value: number }
	| { type: 'master_volume'; value: number }
	| { type: 'headphone_mix'; value: number }
	| { type: 'headphone_level'; value: number }
	| { type: 'headphone_booth_level'; value: number }
	| { type: 'booth_level'; value: number }
	| { type: 'booth_select'; device_id: string }
	| { type: 'booth_signal_mode'; mode: BoothSignalMode }
	| { type: 'head_delay_ms'; value: number }
	| { type: 'master_mute'; muted: boolean; persist?: boolean }
	// LIBUX-8626: stable_id also selects and scrolls to that track once the list shows.
	| { type: 'browser_select_playlist'; playlist_id: string; stable_id?: string }
	// LIBUX-10046: show (true) or leave (false) the library's Related tab.
	| { type: 'browser_related'; active: boolean }
	// LIBUX-10046: edit the Related Tracks criteria (any non-empty subset); the cog's
	// onchange dispatches this, so UI and agents share one path.
	| {
			type: 'browser_related_criteria';
			bpm_percent?: number;
			key?: boolean;
			genre?: boolean;
			color?: boolean;
			rating?: boolean;
			mytag?: boolean;
			artist?: boolean;
	  }
	// LIBUX-10046: restore the #9753 default criteria (the cog's Reset).
	| { type: 'browser_related_criteria_reset' }
	/** IOPIN-06: shared command/query parity for the confirmed pickup policy. */
	| { type: 'midi_takeover_mode'; mode: MidiTakeoverMode }
	/** Agent parity for the learn status Retry / Cancel / Dismiss buttons (issue #10580). */
	| { type: 'midi_learn_retry' }
	| { type: 'midi_learn_cancel' }
	| { type: 'headphone_outputs_refresh' }
	| { type: 'headphone_output_acquire' }
	| { type: 'headphone_output_select'; device_id: string }
	| { type: 'headphone_master_select'; device_id: string }
	| { type: 'headphone_booth_select'; device_id: string }
	| { type: 'headphone_input_select'; device_id: string }
	| { type: 'preview_cue'; stable_id: string; ratio: number; bpm?: number }
	| { type: 'preview_stop' }
	| { type: 'output_mode'; mode: HeadphoneOutputMode }
	| { type: 'headphone_alignment_mode'; value: HeadphoneAlignmentMode }
	| { type: 'master_delay_ms'; value: number }
	| { type: 'headphone_calibrate'; interactive?: boolean }
	| { type: 'headphone_calibrate_abort' }
	| { type: 'analysis_source'; feature: AnalysisSourceFeature; source: AnalysisSource }
	/** TOPVIEW-06: two-track AutoPlay, the set kept on decks 1 and 2 (the
	 * AutoPlay menu checkbox's twin; persists uiPrefs.auto_play_two_deck). */
	| { type: 'auto_play_two_track'; enabled: boolean }
	/** TOPVIEW-01..03: which view the /performance wavestack area shows (the
	 * top bar's view icons; persists uiPrefs.perf_view). */
	| { type: 'set_perf_view'; view: PerfView }
	/** TOPVIEW-04: whether the performance-pad rows are shown (the top bar's
	 * PAD button; persists uiPrefs.show_pads). */
	| { type: 'show_pads'; enabled: boolean }
	/** DECKGAP-7691: which mixer values reset when a track loads (one choice, every deck). */
	| {
			type: 'track_load_reset';
			level: boolean;
			stems: boolean;
			eq: boolean;
			trim: boolean;
			filter: boolean;
	  }
	/** DECKGAP-7691h: the panel's Reset now. Applies the ticked resets (or `parts`) to this
	 * deck at once, as ordinary mixer and stem commands through this dispatcher. */
	| { type: 'reset_deck_now'; deck: DeckId; parts?: TrackLoadResetId[] }
	/** DECKGAP-7691b: the key nudge arrows' step size, shared by every deck. */
	| { type: 'key_nudge_step'; step: KeyNudgeStep }
	/** Pin fc60002b81a8: early next-track transition trigger, the ">|" split
	 * of the AutoPlay button. Real (not stubbed) - see auto-play-next.ts /
	 * auto-play-next.svelte.ts for the approximate loop/duck/cut it arms. */
	| {
			type: 'auto_play_next_arm';
			intent?: TransitionIntent;
			out_deck?: DeckId;
			in_deck?: DeckId;
			from_ms?: number;
	  }
	| { type: 'auto_play_next_cancel' }
	/** AUTOPLAN-06: the AutoPlay menu's Decide now. */
	| { type: 'auto_play_decide_now' }
	// AUTOPLAY-8881b: the one-tap verdict on the transition that ran (ADR-0139). Pure logging.
	| { type: 'autoplay_verdict'; out: string; in: string; verdict: Verdict }
	| { type: 'auto_play_next_intent'; intent: TransitionIntent }
	/** AUTOPLAN-01: the AutoPlay menu's early planning checkbox and target. */
	| { type: 'auto_play_early'; enabled?: boolean; decide_after_s?: number }
	/** UI contract only: the "show other users' pins" toggle (pin 88e3abec02a0)
	 * is stubbed - community comment-pin sync has no cloudsync channel yet. */
	| { type: 'pins_show_other_users' }
	| { type: 'library_panels'; panel: LibraryPanel; collapsed: boolean }
	| { type: 'library_panels_toggle'; collapsed?: boolean }
	| { type: 'show_stems'; enabled: boolean }
	/** WAVE-EQ-5230: deck waveforms follow the engine-reported EQ/filter gains. */
	| { type: 'wave_eq_reflect'; enabled: boolean }
	/** The mixer's S button: stem dials beside the EQ on every channel with stems. */
	| { type: 'mixer_stems'; enabled: boolean }
	/** Issue #9434: where the mixer sits; `center` or `sides` (#9371). */
	| { type: 'mixer_placement'; placement: MixerPlacement }
	// DECKCTL-6235: the AU preference (park a fresh load at its first memory cue).
	| { type: 'deck_autocue'; enabled: boolean }
	/** COLORFX-01..05: the channel color knob's effect (FILTER, SPACE, ECHO, CRUSH), the
	 * echo division, and optionally the knob itself. See color-fx.svelte.ts.
	 * COLORFX-8341: `next` cycles the effect; switching effect returns the knob to
	 * center unless `value` is given (the old effect is removed, never stacked). */
	| {
			type: 'color_fx';
			deck: DeckId | 'all';
			effect?: 'filter' | 'space' | 'crush' | 'echo' | 'next';
			echo_beats?: 0.25 | 0.5 | 0.75 | 1;
			value?: number;
	  }
	/** BEATFX-12: channel-select beat FX unit (issues #9783, #10015). */
	| {
			type: 'beat_fx';
			target?: BeatFxTarget;
			effect?: BeatFxEffect;
			beats?: BeatFxBeats;
			level?: number;
			enabled?: boolean;
			quantize?: boolean;
			xpad?: number | null;
	  }
	/** AGENT-20: the AutoPlay switch, the TopBar button's command twin. PLAY-18:
	 * only a user turns it off, so `enabled: false` needs `by_user: true`. */
	| { type: 'autoplay'; enabled: boolean; by_user?: boolean }
	| { type: 'feedback_mark'; vote: 'bad' | 'good' | 'great' }
	| { type: 'safety_loop_save'; deck: DeckId }
	| { type: 'safety_loop_arm'; deck: DeckId; armed: boolean }
	| { type: 'safety_loop_clear'; deck: DeckId }
	| {
			type: 'hot_cue_save';
			deck: DeckId;
			slot: HotCueSlot;
			in_ms: number;
			revision: string;
			comment?: string | null;
			out_ms?: number | null;
	  }
	| { type: 'hot_cue_clear'; deck: DeckId; slot: HotCueSlot; revision: string }
	| {
			type: 'hot_cue_restore';
			deck: DeckId;
			slot: HotCueSlot;
			revision: string;
			reversal_id: string;
	  }
	/** #884: the pad-press/MIDI trigger entry point, distinct from hot_cue_save
	 * (which persists a NEW position). BeatSyncMax may arm rather than jump
	 * immediately - see the module docstring and `hot_cue_armed` in query(). */
	| { type: 'hot_cue_trigger'; deck: DeckId; slot: HotCueSlot }
	/** CUES-10639: flag or clear the track's one active memory loop; the loop
	 * menu and agents share this queue with hot-cue persistence writes. */
	| { type: 'active_memory_loop'; deck: DeckId; in_ms: number; out_ms: number; active: boolean }
	// LIBUX-05 "technically-working mode": UI-only overlay state, no engine
	// write to serialize, but still a real UI action - every one of these has
	// a Ctrl+R / Opt / cmd+E keyboard equivalent in technically-working-hotkeys.ts,
	// so agent-native parity requires the same commands here.
	| { type: 'tech_mode_toggle' }
	| { type: 'tech_mode_peek'; peeking: boolean }
	| { type: 'tech_mode_opt_reveal'; revealed: boolean }
	| { type: 'tech_mode_eq_raised'; raised: boolean }
	| { type: 'tech_mode_edge_hover'; edge: EdgeRegion; hovered: boolean }
	// TOPVIEW-05: the top bar's Track information (i) overlay, UI state with
	// no engine write, shared by the button and agents (Codex P1 4300879440).
	| { type: 'track_info_toggle' }
	// Deck Quick Draw menu Track info; deck-targeted so an agent can open any deck (#9448, PRRT_kwDOSEvNd860v-hu).
	| { type: 'track_info_open'; deck: DeckId }
	| { type: 'track_info_close' }
	// OJ-9477: the top bar's OJ button; toggles the agent sidebar, UI state only.
	| { type: 'oj_toggle' }
	/** Issue #9451: open the overlay for a library row (context menu Track info). */
	| { type: 'track_info_open'; stable_id: string }
	| { type: 'pairing_snapshot_open' }
	| { type: 'pairing_snapshot_remove_eq_adjuster'; deck: DeckId; band: EqBand }
	| { type: 'pairing_snapshot_save'; from_deck: DeckId; to_deck: DeckId }
	| { type: 'playlist_undo' }
	| { type: 'playlist_redo' }
	// LIBUX-8626: undo an applied history entry and every newer one, or redo a
	// pending entry and every older one (the undo popover's row click).
	| { type: 'playlist_history_jump'; command_id: string }
	| { type: 'cmdk_open'; query?: string }
	| { type: 'cmdk_close' }
	| {
			type: 'cmdk_pick';
			index: number;
			id?: string;
			deck?: DeckId;
			target?: DeckId;
			confirm?: boolean;
			vocals_only?: boolean;
	  }
	| { type: 'cmdk_mode'; mode: 'plain' | 'ai' }
	| { type: 'window'; action: 'open' | 'close' | 'focus'; label: 'second-screen' }
	| {
			type: 'rescue_resume';
			decks: Array<{ deck: DeckId; position_ms: number }>;
			master_deck: DeckId;
	  }
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
	/** The size the deck's two beat jump buttons move by (DECKUX-10470). */
	beat_jump_size_beats: BeatJumpSize;
	beat_sync_enabled: boolean;
	key_sync_enabled: boolean;
	/** Whether the KEY SYNC arm is actually following a master now (DECKUX-34). */
	key_sync_status: KeySyncStatus;
	/** KEYSYNC-01: exact (default), relative or compatible. */
	key_sync_mode: KeySyncMode;
	/** KEYSYNC-01: the deck KEY SYNC follows, or 'master'. */
	key_sync_target: DeckId | 'master';
	/** KEYSYNC-01 ladder per target deck ("1".."4", loaded keyed decks only):
	 * rows +6..-6 of {shift, key, pattern, desirable} (Mixed In Key patterns). */
	key_options: Partial<Record<`${DeckId}`, KeyLadderRow[]>>;
	master_tempo_enabled: boolean;
	slip_enabled: boolean;
	slip_active: boolean;
	slip_position_ms: number | null;
	roll_beats: number | null;
	roll_stack: number[];
	is_master: boolean;
	sync_mode: SyncMode;
	sync_error: string | null;
	processor_error: string | null;
	presentation_clock_error: string | null;
	/** Last successful load wall ms; null until a load completes (KPI). */
	last_load_latency_ms: number | null;
	/** Monotonic successful-load commit number for durable reload observation. */
	load_generation: number;
	/** Last load stage map (ms); null until a load. CLI/IPC feedback. */
	last_load_stages: Record<string, number> | null;
	stems: StemDeckState;
	/** STEM-77: the loaded track's separation job, pending start and refusal,
	 * the same inputs the deck's SEPARATE button renders; null with no track,
	 * or while the separation store is not loaded yet (a deck showing NO
	 * STEMS loads it, and so does the first `stem_separate` command); a load
	 * that failed shows here as `refusal`, the text the deck's button shows. */
	stem_separation: StemSeparationSnapshot | null;
	loop: LoopState | null;
	/** The saved SAFE slot is engine truth, so agents can verify a resize did
	 * not leave a stale snapshot that out-crossing engage could restore. */
	safety_loop: SafetyLoopSlot | null;
	/** DECKUX-10478: pending manual loop-in point (ms); null when none. */
	pending_loop_in_ms: number | null;
	/** DECKUX-10478: last engaged loop this load, for RELOOP after exit. */
	last_loop: LoopMemory | null;
	/** Loop cluster view state, so an agent that can drive the interval grid
	 * can also read back which mode and window it landed on. */
	loop_interval: { grid_mode: boolean; grid_base: number; choices: number[] };
	/** PADFX-11718: the pad mode the deck's pad bank shows, and why each other mode is or is not selectable. */
	pad_mode: PadModeSnapshot;
	beatgrid: Array<{ n: number; bpm: number; time_ms: number }>;
	beatgrid_ms: number[];
	/** In-time of the `transition_in` row in deck.anlz.cues, or null when absent. */
	transition_in_ms: number | null;
	/** Real AnlzPhrase boundaries lifted for agent phrase-time planning. */
	phrases: Array<{ start_ms: number; end_ms: number; kind: number; mood: number }>;
	/** Validated vocal-analysis status from the deck's published ANLZ (issue #1031). */
	vocals: Vocals | null;
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
	/** Whether this deck's loop is engaged: an engaged loop wraps the playhead, so a plan can never reach its window. */
	loop_engaged: boolean;
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
	/** OFFLOAD: the cloud download a deck is waiting on before it loads (the deck's bar). */
	cloud_downloads: Record<DeckId, DeckCloudDownload | null>;
	/** AUTOGAIN-02: what each deck's trim stage applies now (ready, error at 0 dB, or empty). */
	auto_gain: Record<DeckId, DeckAutoGain>;
	decks: Record<DeckId, PerformanceDeckSnapshot>;
	mixer: {
		crossfader: number;
		master: number;
		booth: BoothState;
		channels: Record<DeckId, MixerChannelState>;
		headphones: HeadphoneState;
	};
	master: { muted: boolean };
	browser: {
		active_playlist: string | null;
		search: string | null;
		sort: { key: SortKey; direction: 'asc' | 'desc' } | null;
		selected_row: string | null;
		load: PerformanceBrowserPaneSnapshot['load'];
		related_active: boolean;
	};
	midi_takeover: { mode: MidiTakeoverMode };
	midi_learn: MidiLearnSnapshot;
	history: Array<{ id: string; type: PerformanceCommand['type'] }>;
	preset: PerformancePresetLifecycleSnapshot;
	rescue_restore: {
		phase: 'idle' | 'restoring' | 'resuming' | 'done' | 'failed';
		playing_deck_ids: DeckId[];
		per_deck: Record<DeckId, 'pending' | 'decoded' | 'failed'>;
		started_at_ms: number;
	};
	/** RESCUE-07: decks a reload stopped and the banner offers to resume (null when
	 * no offer). Agents act on it with `play` per deck, first deck first. */
	reload_resume: { decks: DeckId[]; error: string | null } | null;
	waveform_stutter: WaveformStutterSnapshot;
	library_panels: {
		collapsed: boolean;
		/** Legacy per-panel prefs; the UI uses the combined `collapsed` field. */
		next_collapsed: boolean;
		recommended_collapsed: boolean;
	};
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
	feedback_marks: PerformanceFeedbackSummary;
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
		/** CUEOUT-6060: the bar and phase a snapped preview started on, else null;
		 * `snap_refusal` says why a 'phase' preview played unsnapped. */
		snap: {
			master_beat_number: 1 | 2 | 3 | 4;
			beat_phase: number;
			offset_ms: number;
			clamped: boolean;
			/** True when the master and preview lock only at half or double tempo:
			 * the start is in phase but the bar counts differ (beat-sync-fold). */
			tempo_folded: boolean;
		} | null;
		snap_refusal: string | null;
		/** Decoded preview audio held right now, and the cap it is held under.
		 * Readable here because "how much memory is the preview holding" is a
		 * question an agent has to be able to answer without a profiler. */
		cache_tracks: number;
		cache_bytes: number;
		cache_budget_bytes: number;
	};
	last_error: string | null;
	pairing_snapshot: PairingSnapshot | null;
	/** CMDK-15: the Cmd K bar as an agent sees it; null while no v2 bar is mounted. */
	cmdk: CmdkSnapshot | null;
	/** Open extra desktop-shell windows, by label (currently second-screen). */
	windows: WindowLabel[];
	technically_working: {
		active: boolean;
		peeking: boolean;
		opt_reveal_active: boolean;
		eq_raised: boolean;
		hovered_edges: EdgeRegion[];
	};
	/** TOPVIEW-05: whether the (i) overlay is open, and on which deck and track. */
	track_info: { open: boolean; deck: DeckId | null; stable_id: string | null };
	/** COLORFX-04: per deck, the color knob's effect, echo division and timing. */
	color_fx: Record<DeckId, ColorFxDeckView>;
	/** BEATFX-12: the beat FX unit mirror (effect, division, tempo). */
	beat_fx: BeatFxUnitView;
	/** DL-2: resizable regions; what set_layout_size / reset_layout move. */
	layout: LayoutView;
	ui: {
		show_stems: boolean;
		/** The mixer's S button, so a mixer_stems command can be read back. */
		mixer_stems: boolean;
		/** Issue #9434: center column or mirrored sides (#9371); `mixer_placement` command read back. */
		mixer_placement: MixerPlacement;
		/** LIBUX-10046: Related Tracks criteria the cog edits. */
		library_related_criteria: RelatedCriteriaPrefs;
		/** AGENT-20: so an autoplay command's mirror_delta names what it changed. */
		auto_play_enabled: boolean;
		/** DECKCTL-6235: the AU switch, so a deck_autocue command can be read back. */
		deck_autocue: boolean;
		/** TOPVIEW-06: AutoPlay keeps to decks 1 and 2. */
		auto_play_two_deck: boolean;
		/** TOPVIEW-01..03: the view the wavestack area shows. */
		perf_view: PerfView;
		/** TOPVIEW-04: the performance-pad rows are shown. */
		show_pads: boolean;
		/** DECKGAP-7691: which mixer values reset when a track loads. */
		track_load_reset: TrackLoadResetPrefs;
		/** DECKGAP-7691b: the key nudge arrows' step size. */
		key_nudge_step: KeyNudgeStep;
		/** ARTNUM-9649: the deck number drawn over each deck's artwork. */
		deck_number_art: DeckNumberArt;
		/** Stored pref; 'auto' follows the skin. */
		waveform_design: WaveformDesignPref;
		/** WAVEQS-11733: mirrored full row or single-sided half. */
		wave_height: WaveHeight;
		/** What the waveforms actually paint (auto resolved through the skin). */
		waveform_design_effective: WaveformDesign;
		ui_skin: SkinSettings['ui_skin'];
		wave_palette: SkinSettings['wave_palette'];
		wave_palette_effective: WavePaletteChoice;
		wave_split_master: SkinSettings['wave_split_master'];
		/** Stored override; 'auto' can be read back. */
		layout_override: LayoutOverride;
		/** Effective presets (auto resolved through the skin). */
		layout: { performance: PerformanceLayout; library: LibraryLayout };
		wave_eq_reflect: boolean;
		/** WAVEZOOM-6802..06: shared step, each deck's effective step, options. */
		wave_zoom: WaveZoom;
		wave_zoom_decks: Record<`${DeckId}`, WaveZoom>;
		wave_zoom_playhead: WavePlayheadMode;
		wave_zoom_linked: boolean;
		/** WAVEQS-8348e: what set_waveform_overlay reads back, one boolean per overlay. */
		waveform_overlays: Record<WaveformBoolOverlay, boolean>;
		/** issue #10244: what set_waveform_overlay grid_lines reads back. */
		wave_grid_lines: WaveGridLines;
		/** STRUCTUI-12787: segment display prefs for agents (issue #11436). */
		segment_display: import('./section-overlay-prefs').SectionOverlayPrefsState;
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
	/** The active pane's fill: `complete` false while rows are still arriving,
	 * so an agent never reads a partial list as the whole one. Null: no pane. */
	load: { complete: boolean; rows_loaded: number } | null;
	/** LIBUX-10046: the Related tab is showing instead of the active pane. */
	related_active: boolean;
}

/** BrowserPanel owns playlist loading, while this module owns the public
 * command protocol. Registering the narrow adapter keeps both boundaries
 * explicit and makes a missing mounted browser fail loudly for an agent. */
export interface PerformanceBrowserAdapter {
	selectPlaylist(playlistId: string, stableId?: string): Promise<void>;
	readSnapshot(): PerformanceBrowserPaneSnapshot;
	/** LIBUX-10046: the one path that shows or leaves the Related tab. */
	setRelatedActive(active: boolean): void;
}

/** Pin fc60002b81a8: same decoupling shape as PerformanceBrowserAdapter above
 * - auto-play-next.svelte.ts owns the real orchestration and already
 * imports dispatchPerformanceCommand from this module, so this module
 * registers rather than statically imports it back (a static import both
 * ways would be a real cycle, not just a slack-ratchet number). A missing
 * registration fails loudly, same rationale as a missing browser adapter. */
export interface AutoPlayNextController {
	arm(request: {
		intent?: TransitionIntent | undefined;
		outDeck?: DeckId | undefined;
		inDeck?: DeckId | undefined;
		fromMs?: number | undefined;
	}): Promise<boolean>;
	cancel(): Promise<void>;
	decideNow(): Promise<void>;
	setIntent(intent: TransitionIntent): void;
}

export interface PlaylistHistoryAdapter {
	undo(): Promise<void>;
	redo(): Promise<void>;
	jump(commandId: string): Promise<void>;
}

// CMDK-15: the bar's snapshot and adapter shapes live with the bar's own pure
// logic, so the bar can name them without importing this module.
/**
 * CMDK-15: the v2 Cmd K bar's open state, and the adapter it registers. The bar
 * declares the same two shapes in `$lib/rb/cmdk-actions` so it never imports
 * this module; /performance's `registerAdapter={registerCmdkAdapter}` binding
 * type-checks one against the other, so they cannot drift apart unnoticed.
 */
export interface CmdkSnapshot {
	open: boolean;
	query: string;
	selected: number;
	target: DeckId;
	rows: Array<{
		/** CMDK-6848: a `playlist` row is a library playlist, by its playlist id; picking it opens it as a tree click does. */
		kind: 'track' | 'playlist' | 'setting' | 'command';
		id: string;
		label: string;
		pair?: 'aligned' | 'loose';
		fit?: number;
		/** A setting row: the value it shows (On, Off or the option's label), as the bar draws it (Codex P2 BLOCKING, #6691). */
		value?: string;
		/** A setting row: what Enter does to it for the current query, in the row's own words (Codex P2 BLOCKING, #6691). */
		action?: string;
	}>;
	/** CMDK-20: plain search, or AI search (each track row then carries JEV's `fit`). */
	mode: 'plain' | 'ai';
	/** CMDK-21: the AI answer's state: idle, working, ready, no-key or error. */
	ai: 'idle' | 'working' | 'ready' | 'no-key' | 'error';
	/** With pairs on and a master loaded: the master's pairs lookup (a pair row's id is `pair:<stable_id>:loose` or `pair:<stable_id>:aligned:<basis>`). */
	pairs?: { status: 'loading' | 'ready' } | { status: 'error'; message: string };
	/** While open: the collection index and search warmup; `loading` or `error` means the track rows are not a search result yet. */
	collection?: { status: 'loading' | 'ready' } | { status: 'error'; message: string };
	/** While open: the deck picker or eject confirmation that holds the bar, so the rows above are not the choice being made (CMDK-15, Codex #6691). */
	dialog?:
		| { kind: 'deck-picker'; deck: DeckId; title: string; confirming?: boolean }
		| { kind: 'eject'; deck: DeckId };
	/** The footer's pick state: an aligned pair loading, else the last pick's failure (CMDK-15). */
	operation?:
		{ status: 'loading'; title: string; deck: DeckId } | { status: 'error'; message: string };
}
export interface CmdkAdapter {
	open(query: string | null): Promise<void>;
	close(): void;
	snapshot(): CmdkSnapshot;
	/**
	 * CMDK-19: pick row `index` as Enter does; rejects with the reason when it does not run. With `id`
	 * (the row id `query().cmdk` reported), a row that is no longer that one is refused, not picked.
	 * `vocalsOnly` is Shift+Enter: the track loads and its vocals are soloed once the stems are ready.
	 */
	pick(
		index: number,
		deck: DeckId | null,
		confirm: boolean,
		id: string | null,
		target: DeckId | null,
		vocalsOnly: boolean,
		source: 'user-ui' | 'agent-command'
	): Promise<void>;
	/** CMDK-20: switch plain and AI search as Tab does; rejects when the bar is closed or the flag is off. */
	setMode(mode: 'plain' | 'ai'): void;
}

export type PerformancePresetLifecyclePhase =
	'idle' | 'queued' | PerformancePresetPhase | 'ready' | 'error';

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
	/**
	 * Bug #58 fault drill: fail the next `fail_add_module` Signalsmith addModule
	 * calls, then run the output-stall graph rebuild. Resolves when the rebuild
	 * (with its retries) finishes; returns the decks still re-attaching (none on
	 * success) and the state.
	 */
	drillGraphRebuild(
		input: unknown
	): Promise<{ reattaching: DeckId[]; error: string | null; state: PerformanceState }>;
	/** Bug #58: decks waiting on a graph rebuild retry. */
	reattachingDecks(): DeckId[];
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

export interface PerformanceQuantizedLaunchDriver {
	arm(deck: DeckId, pressT0Ms?: number): Promise<number>;
	clear(deck: DeckId): void;
	contextTimeNowSec(): number;
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
	/** HOTCUE-LOOP-01: engage a loop hot cue's stored range exactly (no
	 * re-snap), right after `jump` landed on its start. */
	engageLoop(deck: DeckId, loop: { in_ms: number; out_ms: number }): Promise<void>;
	/** Defer the jump to the deck's own next downbeat; returns the absolute
	 * AudioContext time the schedule lands at. pressT0Ms is Q1's
	 * operator-felt press stamp. A resolver `armAt` is called with the
	 * engine's live position inside the scheduling transaction. */
	arm(deck: DeckId, positionMs: number, armAt: ArmAtPosition, pressT0Ms?: number): Promise<number>;
	contextTimeNowSec(): number;
}

/** CMDK-19: the decks plus 'sync' a coordinated operation holds (`runCoordinatedFromUi`). */
export type CoordinatedHold = ScopeHold<CommandScope>;

/**
 * What a UI-boundary command came to. `no_session` is the one EXPECTED
 * refusal: no command session is current, because the route has not started
 * one yet (it does so after its first async hydration) or has torn it down.
 * `failed` is everything else, and has always been reported by the time the
 * caller sees it.
 */
export type PerformanceUiCommandResult =
	| { ok: true }
	| { ok: false; reason: 'no_session'; error: ScopedCommandInvalidatedError }
	| { ok: false; reason: 'failed'; error: unknown };
