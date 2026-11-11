/**
 * localStorage-backed UI prefs for the /performance browser (FR-1).
 *
 * Rune module - the .svelte.ts extension is REQUIRED for $state
 * (RECON-FRONTEND 10.1). One JSON blob under STORAGE_KEY.
 *
 * Fail-fast policy: a MISSING key is the real first-run state and yields
 * the documented defaults; a PRESENT but malformed blob throws loudly
 * (no silent reset - clear the key to recover). The app is SPA-only
 * (ssr=false in +layout.ts) so localStorage always exists in the browser;
 * the typeof guard only protects unit tests.
 */

import {
	beatJumpSizesFromPrefs,
	beatJumpSizesToPrefs,
	onBeatJumpSizeChange,
	restoreBeatJumpSizes,
	type BeatJumpSizesByDeck
} from './beat-jump-size.svelte';
import {
	DRAG_DEFAULTS,
	applyDragSensitivity,
	validateDragSensitivity,
	type DragSensitivityPrefs
} from './drag-sensitivity.svelte';
import {
	validateDeckLayoutFields,
	makeDeckLayoutSetters,
	DECK_LAYOUT_DURATIONS_MS,
	type DeckLayoutDurationMs,
	type DeckLayoutMode
} from './deck-layout-prefs';
import {
	makeTopbarViewSetters,
	TOPBAR_VIEW_DEFAULTS,
	validateTopbarViewFields,
	type TopbarViewPrefsState
} from './topbar-view-prefs';
import {
	makePlaylistTreeViewSetters,
	validatePlaylistTreeViewField,
	type PlaylistTreeViewMode
} from './playlist-tree-view-prefs';
import {
	PIN_VISIBLE_DAYS_DEFAULT,
	PIN_VISIBLE_DAYS_MAX,
	PIN_VISIBLE_DAYS_MIN,
	isPinVisibleDays
} from '$lib/rb/feedback-pin-age';
import type { PreviewBeatSync } from '$lib/player/preview-beat-sync';
import { DECIDE_AFTER_MAX_S, DECIDE_AFTER_MIN_S, DEFAULT_DECIDE_AFTER_S } from '$lib/rb/auto-play-early';
import {
	parseWaveHeight,
	validateWaveHeightPrefFields,
	WAVE_HEIGHT_DEFAULT,
	WAVE_HEIGHT_PREF_DEFAULTS,
	type WaveHeight
} from '$lib/rb/wave-height';
import {
	parseWaveformDesignPref,
	WAVEFORM_DESIGN_PREF_DEFAULT,
	type WaveformDesignPref
} from '$lib/rb/waveform-design';
import {
	parseWavePalettePref,
	WAVE_PALETTE_PREF_DEFAULT,
	type WavePaletteChoice,
	type WavePalettePref
} from '$lib/rb/wave-palette';
import { makeJogRadialWaveformSetters } from './jog-radial-prefs';
import {
	WAVE_ZOOM_PREF_DEFAULTS,
	deckWaveZoom,
	parseWavePlayheadMode,
	parseWaveZoom,
	stepWaveZoom,
	validateWaveZoomPrefFields,
	type WavePlayheadMode,
	type WaveZoom,
	type WaveZoomPrefsState
} from './wave-zoom';
import type { DeckId } from '$lib/player/constants';
// Re-exported for the performance IPC, which is at its import fan-out limit.
export { deckWaveZoom, parseWavePlayheadMode, parseWaveZoom, type WavePlayheadMode, type WaveZoom };
import {
	DECK_NUMBER_ART_PREF_DEFAULTS,
	DECK_NUMBER_ART_VALUES,
	parseDeckNumberArt,
	requireDeckNumberArt,
	validateDeckNumberArtPrefFields,
	type DeckNumberArt,
	type DeckNumberArtPrefsState
} from './deck-number-art';
// Re-exported for the performance IPC (import fan-out limit).
export { requireDeckNumberArt, type DeckNumberArt };
import {
	AUTO_ANALYZE_PREF_DEFAULTS,
	makeAutoAnalyzeSetters,
	validateAutoAnalyzePrefFields,
	type AutoAnalyzePrefsState
} from './auto-analyze-prefs';
import {
	SECTION_OVERLAY_PREF_DEFAULTS,
	makeSectionOverlaySetters,
	validateSectionOverlayPrefFields,
	type SectionOverlayPrefsState
} from './section-overlay-prefs';
import {
	SPEAKER_LINK_PREF_DEFAULTS,
	makeSpeakerLinkSetters,
	validateSpeakerLinkPrefFields,
	type SpeakerLinkPrefsState
} from './speaker-link-prefs';
import {
	TEMPO_LANE_PREF_DEFAULTS,
	makeTempoLaneSetters,
	validateTempoLanePrefFields,
	type TempoLanePrefsState
} from './tempo-lane-prefs';
import {
	VOCAL_LANE_PREF_DEFAULTS,
	makeVocalLaneSetters,
	validateVocalLanePrefFields,
	type VocalLanePrefsState
} from './vocal-lane-prefs';
import {
	WAVE_GRID_LINES_PREF_DEFAULTS,
	makeWaveGridLinesSetter,
	validateWaveGridLinesPrefFields,
	type WaveGridLinesPrefsState
} from './wave-grid-lines';
import {
	LIBRARY_FILTER_PREF_DEFAULTS,
	makeLibraryFilterSetters,
	validateLibraryFilterPrefFields
} from './library-filter-prefs';
import {
	COMPATIBLE_FILTER_DEFAULTS,
	validateCompatibleFilterPrefs,
	type CompatibleFilterPrefs
} from './compatible-filter-prefs';
import {
	RELATED_CRITERIA_DEFAULTS,
	validateRelatedCriteriaPrefs,
	type RelatedCriteriaPrefs
} from './related-criteria-prefs';
import {
	canonicalizeLibraryKeyFilter,
	validateLibraryKeyFilterField
} from './library-key-filter';
import { isPageGoingAway } from '$lib/api/lifecycle-abort';
import { reportSettingSaveError } from '$lib/settings/setting-save-errors';
import { makeLevelCalibrationSetters } from './level-calibration-prefs';
import {
	LYRICS_PREF_DEFAULTS,
	makeLyricsPrefSetters,
	validateLyricsPrefFields,
	type LyricsLoadStrategy,
	type LyricsPrefs
} from './lyrics-prefs';
import {
	APP_MODE_PREF_DEFAULTS,
	bindAppModePrefSetters,
	mergeAppModePrefsFromParsed,
	type AppModePrefs
} from './app-mode-prefs';
import {
	GIG_HELPER_PREF_DEFAULTS,
	bindGigHelperPrefSetters,
	mergeGigHelperPrefsFromParsed,
	type GigHelperPrefs
} from './gig-helper-prefs';
import {
	DEV_UI_PREF_DEFAULTS,
	makeDevUiPrefSetters,
	mergeDevUiPrefsFromParsed,
	type DevUiPrefs
} from './dev-ui-prefs';
import {
	makeVariantPrefAccessors,
	onVariantChange,
	type VariantPrefs
} from './variant-prefs';
import {
	DECK_DEFAULT_PREF_DEFAULTS,
	announceDeckDefaults,
	makeDeckDefaultPrefSetters,
	mergeDeckDefaultPrefsFromParsed,
	type DeckDefaultPrefs
} from './deck-default-prefs';
import {
	APP_POSTURE_PREF_DEFAULTS,
	bindAppPosturePrefSetters,
	mergeAppPosturePrefsFromParsed,
	type AppPosturePref,
	type AppPosturePrefs
} from './app-posture-prefs';
import {
	PERF_TIER_PREF_DEFAULTS,
	bindPerfTierPrefSetters,
	mergePerfTierPrefsFromParsed,
	type PerfTierPrefs
} from './perf-tier-prefs';
import {
	makeAutoCueRefresher,
	makePrefsHydrator,
	makeLibraryKeyFilterRehydrator,
	setLibraryBrowserDiskPref,
	setTopbarDiskPref,
	putDiskPrefsVerified,
	syncDiskPrefs
} from './prefs-hydrate';
import { notePick, type CommandPick } from '../palette/rank';
import {
	parseAutoSync,
	parseCmdkCommandPicks,
	parseLastPlaylist,
	parseLevelCalibration,
	parseSpotifyLibrary,
	parseVariants
} from './prefs-fields';
import type { AutoSyncPrefs, LastPlaylistPref, LevelCalibrationPrefs, SpotifyLibraryPref } from './prefs-types';
import { makeSpotifyLibrarySetters } from './spotify-library-prefs';
import { validateActiveScheme } from './theme-tokens';
import { DEFAULT_KEY_NOTATION, installKeyNotationSource, isKeyNotation, KEY_NOTATIONS, type KeyNotation } from './key-notation';
import { toggleHiddenColumn, validateHiddenColumns, type HideableColumn } from './library-columns';
import {
	applyUiSkinDom,
	effectiveWavePalette,
	nextUiSkin,
	parseUiSkin,
	parseWaveSplitMaster,
	UI_SKIN_DEFAULT,
	UI_SKIN_PRE_GOTHIC_DEFAULT,
	WAVE_SPLIT_MASTER_DEFAULT,
	type UiSkin,
	type WaveSplitMaster
} from './ui-skin';
import {
	LAYOUT_OVERRIDE_DEFAULT,
	parseLayoutOverride,
	parseLayoutPreset,
	parseLayoutSurface,
	type LayoutOverride,
	type LayoutSurface
} from './skin-layout';
import { tryOfferGigHelperPromptOnPostureChange } from './gig-helper-prompt.svelte';
import { announceBeatSyncMaxChange } from './beat-sync-max-enable';
// Re-exported so performance-ipc (at the frontend.max_fan_out ceiling) reaches
// the enabler through the prefs import it already has.
export { enableBeatSyncAfterLoad, installBeatSyncMaxEnabler } from './beat-sync-max-enable';
export { DECK_LAYOUT_DURATIONS_MS, type DeckLayoutDurationMs, type DeckLayoutMode } from './deck-layout-prefs';
// The top bar's 2-deck toggle copy, re-exported beside setDeckLayoutMode so
// TopBar reads both from this one module (quality ratchet: max fan-out).
export { describeTwoDeckToggle } from './two-deck-toggle';
export { type LyricsLoadStrategy } from './lyrics-prefs';
export type { AppModeId } from './app-mode';
export type { GigHelperPref } from './gig-helper-prefs';
export type { AppPosturePref } from './app-posture-prefs';
export type { PerfTierPref } from './perf-tier-prefs';
export type { AutoSyncPrefs, LastPlaylistPref } from './prefs-types';

const STORAGE_KEY = 'mdt.rb.ui-prefs.v1';

/** Playlist tree width bounds in CSS pixels (220-520). */
export const PLAYLIST_TREE_WIDTH_MIN = 220;
export const PLAYLIST_TREE_WIDTH_MAX = 520;
export const PLAYLIST_TREE_WIDTH_DEFAULT = 300;

/** Library track-table row density (browser list only - not decks/mixer). */
export type LibraryDensity = 'compact' | 'cosy';

/** The two optional suggestion panels below the library table. */
export type LibraryPanel = 'next' | 'recommended';

export type { PreviewBeatSync };

/** App + /performance chrome theme. Default dark. */
export type UiTheme = 'dark' | 'light';

/** Preferred vendor writeback targets (preference only; CLI writeback today). */
export type AutoSyncDestination = 'rekordbox' | 'djay' | 'open_dj';

/** Crossfader curve selection (MIXUX-08). Only magic is live today. */
export type CrossfadeCurve = 'magic' | 'bass_swap' | 'linear';

/** Horizontal wheel target on /performance (MIXUX-08). Color routes to FILTER until built. */
export type HorizontalWheelKnob = 'filter' | 'color';

export interface RbUiPrefs
	extends PerfTierPrefs,
		AppPosturePrefs,
		GigHelperPrefs,
		AppModePrefs,
		LyricsPrefs,
		DevUiPrefs,
		VariantPrefs,
		TopbarViewPrefsState,
		DeckDefaultPrefs,
		SectionOverlayPrefsState,
		AutoAnalyzePrefsState,
		DeckNumberArtPrefsState,
		SpeakerLinkPrefsState,
		TempoLanePrefsState,
		VocalLanePrefsState,
		WaveGridLinesPrefsState,
		WaveZoomPrefsState {
	/** Width, in CSS pixels, of the resizable playlist tree (220 through 520). */
	playlist_tree_width: number;
	/** Per-deck beat jump size in beats (DECKUX-10470), restored on boot. */
	beat_jump_sizes: BeatJumpSizesByDeck;
	/** FR-1: hide missing-file tracks and playlists with available_count == 0. Default OFF. */
	hide_broken_links: boolean;
	/** Track-table row height: compact = current tight rows; cosy = taller. */
	library_density: LibraryDensity;
	/** Sort inside each playlist section instead of hiding the sections (D7). */
	sort_within_sections: boolean;
	/** Edge sampler on/off (spec edge-sampler 7.4); off = edges never wake, no gutter. */
	sampler_enabled: boolean;
	/** Section ids shown collapsed on this machine. */
	collapsed_sections: string[];
	/** How every key cell is written (keys are stored as canonical Camelot). */
	key_notation: KeyNotation;
	/** LIBUX-54: track-table columns hidden from the header chooser, in table order. */
	library_hidden_columns: HideableColumn[];
	/** When true, every transport relocate (including master) uses BAR
	 * phase-preserving sync so bar 1 stays aligned across synced decks. */
	beat_sync_max: boolean;
	/** DECKCTL-6235 (alpha status I11): the deck AU button. On: a loaded track
	 * parks at its first memory cue (else its first beat), and a finished
	 * track restarts from its cue rather than 0:00. */
	deck_autocue: boolean;
	/**
	 * CUEOUT-15 R6: tempo of the library preview voice. 'tempo' matches a
	 * playing master deck when the match fits the preview pitch range (half and
	 * double time count); 'off' plays every preview at its own tempo.
	 */
	preview_beat_sync: PreviewBeatSync;
/** Library list: keep only tracks appropriate as next (Camelot + BPM
	 * window vs master / loaded reference). Toggle with Tab. */
	next_only_filter: boolean;
	/** Library list: keep only remixes (title-marker heuristic, backend
	 * is_remix; the lyric repair signal joins it after the library run). */
	remixes_filter: boolean;
	/** Library list: keep only tracks with vocals (VOCALS-FILTER-01, the rule
	 * is browser/vocals-filter.ts rowVocalsVerdict). */
	vocals_filter: boolean;
	/** Library list: keep only tracks with local audio present. */
	available_offline_filter: boolean;
	/** Library list: exact canonical Camelot multi-select (issue #7599). */
	library_key_filter: string[];
	/** Persisted independently so either collapsed rail entry can restore its panel. */
	next_panel_collapsed: boolean;
	recommended_panel_collapsed: boolean;
	/**
	 * Auto-play next track onto a free/stopped follower when the playing
	 * source enters the remaining-time window (~16s). Hard-cut v1.
	 */
	auto_play_enabled: boolean;
	/**
	 * When true, AutoPlay walks strict playlist order after the current track.
	 * When false (default), picks earliest un-played membership row with
	 * Camelot key +-1 and BPM inside Beat Sync pitch bounds.
	 */
	auto_play_enforce_order: boolean;
	/**
	 * Smart AutoPlay only: prefer the compatible next track with the fewest
	 * onward options (slack / maximize reachable chain). Off = greedy earliest.
	 */
	auto_play_maximize_reach: boolean;
	/** AUTOPLAN-01: decide around the soft target and plan the handoff; off restores the end-of-track handoff alone. */
	auto_play_early_planning: boolean;
	/** AUTOPLAN-01: the soft target, seconds on the master. Taste, to calibrate by ear. */
	auto_play_decide_after_s: number;
	/** Light/dark chrome. Default dark. Applied to documentElement. */
	theme: UiTheme;
	/** Hide grayed PARITY-TODO rows in the settings overlay. */
	hide_todo_settings: boolean;
	/**
	 * Preferred auto-sync destinations. Persisted for setup/settings;
	 * vendor DB writeback is still manual CLI (apps/sync/apply_ratings.py).
	 */
	auto_sync: AutoSyncPrefs;
	/** #328 USB tracker: toast when a new stick is detected. */
	usb_toast_enabled: boolean;
	/** Dismiss delay for that toast, in milliseconds. */
	usb_toast_ms: number;
	/**
	 * Open the USB panel on ANY new detect. Off by default: a volume that
	 * still needs its first-seen answers already forces the panel open, so
	 * this only adds the intrusion for sticks that need nothing.
	 */
	usb_auto_open_panel: boolean;
	/**
	 * LIBUX-05: "Technically-working mode" edge-reveal overlay. True = smooth
	 * fade transitions (default); false = instant appear/disappear.
	 */
	technically_working_animate: boolean;
	/** DECKUX-02: polar preview waveform on jog dials instead of the red tick. */
	jog_radial_waveform: boolean;
	/** PIN-AGENT-01: agent findings stay independently visible from operator pins. */
	show_agent_pins: boolean;
	/**
	 * How many days comment pins stay drawn. 0 shows every pin. Local-only:
	 * the daemon ui-prefs schema does not have this field.
	 */
	pin_visible_days: number;
	/**
	 * CMDK-9465: Cmd K command picks for frecency ranking. Local-only: the daemon
	 * ui-prefs schema does not have this field.
	 */
	cmdk_command_picks: CommandPick[];
	/** DECKUX-19: per-stem mini-waveforms under deck wavestack rows. Default off. */
	show_stems: boolean;
	/** Mixer S button (the maintainer, Mon 2 Nov 2026): stem level dials VOCAL/INST/BASS
	 * beside HI/MID/LOW and DRUMS beside FILTER, on every channel whose track
	 * has stems; the mixer column widens 20% while on. Default off. */
	mixer_stems: boolean;
	/** DECKUX-5917b: hovering a deck, wavestack row or mixer strip highlights its linked panels. Default on. */
	deck_hover_link: boolean;
	/** LIBUX-11716: shrink 4-deck view so the library shows more rows. Default off. */
	compact_decks: boolean;
	/** Compatible-filter range knobs (LIBUX-32). */
	compatible_filter: CompatibleFilterPrefs;
	/** Related Tracks criteria (LIBUX-10046). */
	library_related_criteria: RelatedCriteriaPrefs;
	/** LIBM-129 v2: configured watcher folders (no daemon yet). */
	library_watcher_folders: string[];
	/** DECKUX-20: tri-band, mono envelope, line outline or blocks, or 'auto'
	 * (default) to follow the skin (ui-skin.ts SKIN_WAVE_LOOK). */
	waveform_design: WaveformDesignPref;
	/** WAVEQS-11733: 'full' mirrors about the centre line; 'half' is single-sided from the bottom baseline. */
	wave_height: WaveHeight;
	/** WAVE-EQ-5230: deck waveforms follow each deck's live EQ and filter, as the
	 * audio engine reports them. Default on. */
	wave_eq_reflect: boolean;
	/** Issue #4219: waveform band colors. 'rekordbox' (default) is CDJ 3Band:
	 * dark blue low, amber mid, white high; 'legacy' is the pre-#4219 orange
	 * low, blue mid, near-white high. 'auto' (default) follows the skin. The
	 * EFFECTIVE palette is applied as html[data-wave-palette]. */
	wave_palette: WavePalettePref;
	/** Chrome skin layered over the theme, applied as html[data-skin]. */
	ui_skin: UiSkin;
	/** Issue #10199: each surface's layout preset, or 'auto' to follow SKIN_LAYOUT. */
	layout_override: LayoutOverride;
	/** Split main waveform (master on top); 'auto' follows the skin. */
	wave_split_master: WaveSplitMaster;
	/** One-time defaults migrations already applied to this blob (PREFS_DEFAULTS_MIGRATION). */
	defaults_migration: number;
	/** Migration 2's false is not yet acknowledged by the daemon copy: until it
	 * is, the disk value cannot hydrate over it and every boot resends it. */
	radial_off_pending: boolean;
	/** A local AutoPlay choice the daemon has not acknowledged: until it does, the disk value
	 * cannot hydrate over it and every boot resends it (#10717). */
	auto_play_unsaved: boolean;
	/**
	 * Destructive / move confirms: false = skip the prompt forever.
	 * Missing keys mean "ask". Persisted under the same blob.
	 */
	confirm: {
		delete_playlist?: boolean;
		playlist_drop_mode?: 'add' | 'move';
		/** false = skip double-click Load+play confirm (do this every time). */
		dblclick_load_play?: boolean;
	};
	/**
	 * Identity of the playlist the first browser pane last held, restored on
	 * the next boot of /performance so the track table does not open blank.
	 * null = nothing remembered yet (first run), which resolves to All Tracks
	 * when the library is non-empty. Identity only - counts are re-fetched,
	 * never restored, so a stale number can never reach the screen.
	 */
	last_playlist: LastPlaylistPref | null;
	spotify_library: SpotifyLibraryPref;
	/** Pin 862cd3: MORE/LESS two-deck performance layout. Default 'more'. */
	deck_layout: DeckLayoutMode;
	/** Animate the deck_layout switch. Off = instant swap (reduced-motion always 0ms). */
	deck_layout_animate: boolean;
	/** Transition duration in ms when deck_layout_animate is true. */
	deck_layout_duration_ms: DeckLayoutDurationMs;
	/** Mirror deck 2 control row horizontally for mixer-facing symmetry (issue #3983). */
	deck_right_mirror: boolean;
	/** Mirror deck 1 and 3 control rows (local-only pref). */
	deck_left_mirror: boolean;
	/** Playlist sidebar: tree list vs column browser (issue #3983). */
	playlist_tree_view: PlaylistTreeViewMode;
	level_calibration: LevelCalibrationPrefs;
	/** Crossfader curve name; unbuilt curves stay disabled in the UI. */
	crossfade_curve: CrossfadeCurve;
	/** Horizontal mouse wheel adjusts filter or color knob on selected channels. */
	horizontal_wheel_knob: HorizontalWheelKnob;
	/** R6: click-drag base gain and distance falloff (local-only pref). */
	drag_sensitivity: DragSensitivityPrefs;
}

/** Version of the one-time defaults migration (Mon 5 Oct 2026): Gothic becomes
 * the default skin and both waveform prefs become 'auto' (follow the skin).
 * Every write persists the whole blob, so a stored value that equals an old
 * default cannot be told apart from an explicit pick of it. A blob below this
 * version therefore migrates exactly the OLD DEFAULT values (or absent keys)
 * once; anything else was an explicit choice and is kept. The blob then
 * carries this version, so a later explicit pick of an old default sticks. */
export const PREFS_DEFAULTS_MIGRATION = 2;

/** Migration 1 (above): the Gothic skin and Auto waveform defaults. */
const _SKIN_DEFAULTS_MIGRATION = 1;
/** Migration 2 (Wed 7 Oct 2026, the maintainer: "set radial waveforms to default off"):
 * the code default was already false, but every blob that ever persisted kept
 * an old true. A blob below this version gets jog_radial_waveform = false ONCE;
 * a later explicit true then sticks. */
const _RADIAL_OFF_MIGRATION = 2;

/** Pre-migration defaults, matched exactly (see PREFS_DEFAULTS_MIGRATION). */
const _PRE_GOTHIC_WAVEFORM_DESIGN: WaveformDesignPref = 'tri-band';
const _PRE_GOTHIC_WAVE_PALETTE: WavePalettePref = 'rekordbox';

const DEFAULTS: RbUiPrefs = {
	playlist_tree_width: PLAYLIST_TREE_WIDTH_DEFAULT,
	beat_jump_sizes: beatJumpSizesToPrefs(),
	hide_broken_links: false,
	library_density: 'compact',
	sort_within_sections: false,
	sampler_enabled: true,
	collapsed_sections: [],
	key_notation: DEFAULT_KEY_NOTATION,
	library_hidden_columns: [],
	beat_sync_max: true,
	deck_autocue: false,
	// CUEOUT-6060 (the maintainer's recommended default, fleet-af #335 1A): a preview
	// snaps onto the master's bar in phase. A stored 'tempo' is kept.
	preview_beat_sync: 'phase',
	next_panel_collapsed: false,
	recommended_panel_collapsed: false,
	auto_play_enabled: false,
	auto_play_enforce_order: false,
	auto_play_maximize_reach: true,
	auto_play_early_planning: true,
	auto_play_decide_after_s: DEFAULT_DECIDE_AFTER_S,
	theme: 'dark',
	hide_todo_settings: false,
	auto_sync: { rekordbox: false, djay: false, open_dj: false },
	usb_toast_enabled: true,
	usb_toast_ms: 5000,
	usb_auto_open_panel: false,
	technically_working_animate: true,
	jog_radial_waveform: false,
	show_agent_pins: true,
	pin_visible_days: PIN_VISIBLE_DAYS_DEFAULT,
	cmdk_command_picks: [],
	show_stems: false,
	mixer_stems: false,
	deck_hover_link: true,
	compact_decks: false,
	compatible_filter: { ...COMPATIBLE_FILTER_DEFAULTS },
	library_related_criteria: { ...RELATED_CRITERIA_DEFAULTS },
	library_key_filter: [],
	library_watcher_folders: [],
	waveform_design: WAVEFORM_DESIGN_PREF_DEFAULT,
	wave_height: WAVE_HEIGHT_DEFAULT,
	wave_eq_reflect: true,
	wave_palette: WAVE_PALETTE_PREF_DEFAULT,
	ui_skin: UI_SKIN_DEFAULT,
	layout_override: { performance: 'auto', library: 'auto' },
	wave_split_master: WAVE_SPLIT_MASTER_DEFAULT,
	defaults_migration: PREFS_DEFAULTS_MIGRATION,
	radial_off_pending: false,
	auto_play_unsaved: false,
	confirm: {},
	last_playlist: null,
	spotify_library: { pinned_ids: [], recent_ids: [], shrink_main_for_player: true },
	deck_layout: 'more',
	deck_layout_animate: true,
	deck_layout_duration_ms: 200,
	...TOPBAR_VIEW_DEFAULTS,
	deck_right_mirror: false,
	deck_left_mirror: false,
	playlist_tree_view: 'tree',
	level_calibration: { red_dbfs: null, red_enabled: false, ceiling_dbfs: null, ceiling_enabled: false },
	crossfade_curve: 'magic',
	horizontal_wheel_knob: 'filter',
	drag_sensitivity: { base: DRAG_DEFAULTS.base, falloff_px: DRAG_DEFAULTS.falloffPx },
	...SECTION_OVERLAY_PREF_DEFAULTS,
	...AUTO_ANALYZE_PREF_DEFAULTS,
	...DECK_NUMBER_ART_PREF_DEFAULTS,
	...SPEAKER_LINK_PREF_DEFAULTS,
	...TEMPO_LANE_PREF_DEFAULTS,
	...VOCAL_LANE_PREF_DEFAULTS,
	...WAVE_GRID_LINES_PREF_DEFAULTS,
	...WAVE_ZOOM_PREF_DEFAULTS,
	...LYRICS_PREF_DEFAULTS,
	...LIBRARY_FILTER_PREF_DEFAULTS,
	...PERF_TIER_PREF_DEFAULTS,
	...APP_POSTURE_PREF_DEFAULTS,
	...GIG_HELPER_PREF_DEFAULTS,
	...APP_MODE_PREF_DEFAULTS,
	...DEV_UI_PREF_DEFAULTS,
	variants: {},
	...DECK_DEFAULT_PREF_DEFAULTS
};

// ----------------------------------------------------------- _helpers

function _storage(): Storage | null {
	return typeof window === 'undefined' ? null : window.localStorage;
}

function _applyThemeDom(theme: UiTheme): void {
	if (typeof document === 'undefined') return;
	document.documentElement.dataset.theme = theme;
	document.documentElement.style.colorScheme = theme;
	// Dev builds only: the contrast tables (theme-tokens + color-contrast) are a
	// developer diagnostic that logs to the console, and in a production build
	// they cost the library first paint about 3 KB gzip for no visible effect.
	// The same rules gate CI statically (theme-tokens.test.mjs and the contrast
	// tests), so a failing shipped scheme is still caught before release.
	if (import.meta.env.DEV) validateActiveScheme(theme);
}

/** theme.css keys the legacy waveform override blocks on this attribute;
 * the default palette needs no attribute, so it is removed rather than set. */
function _applyWavePaletteDom(choice: WavePaletteChoice): void {
	if (typeof document === 'undefined') return;
	if (choice === 'rekordbox') delete document.documentElement.dataset.wavePalette;
	else if (choice === 'legacy' || choice === 'mono' || choice === 'blue') document.documentElement.dataset.wavePalette = choice;
}

/** True when _load migrated a stored blob, so boot persists the new version. */
let _bootMigrated = false;

type SkinLookPrefs = Pick<RbUiPrefs, 'ui_skin' | 'waveform_design' | 'wave_palette' | 'defaults_migration'>;

/** Apply PREFS_DEFAULTS_MIGRATION to a stored blob's skin-look fields. */
function _migrateSkinDefaults(
	skin: UiSkin | undefined,
	design: WaveformDesignPref | undefined,
	palette: WavePalettePref | undefined,
	version: unknown
): SkinLookPrefs {
	if (version !== undefined && (typeof version !== 'number' || !Number.isInteger(version) || version < 0)) {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (defaults_migration must be a non-negative integer) - ` +
				'clear the localStorage key to recover'
		);
	}
	if (version !== undefined && version >= _SKIN_DEFAULTS_MIGRATION) {
		return {
			ui_skin: skin ?? DEFAULTS.ui_skin,
			waveform_design: design ?? DEFAULTS.waveform_design,
			wave_palette: palette ?? DEFAULTS.wave_palette,
			defaults_migration: version
		};
	}
	_bootMigrated = true;
	return {
		ui_skin: skin === undefined || skin === UI_SKIN_PRE_GOTHIC_DEFAULT ? DEFAULTS.ui_skin : skin,
		waveform_design:
			design === undefined || design === _PRE_GOTHIC_WAVEFORM_DESIGN ? DEFAULTS.waveform_design : design,
		wave_palette: palette === undefined || palette === _PRE_GOTHIC_WAVE_PALETTE ? DEFAULTS.wave_palette : palette,
		defaults_migration: _SKIN_DEFAULTS_MIGRATION
	};
}

/** True when a stored blob carries an unacknowledged migration 2, so boot resends it. */
let _radialBootWrite = false;

/** Apply migration 2 on top of the skin step; returns the radial value, the
 * new version and whether the daemon copy still has to learn the false. */
function _migrateRadialOff(
	radial: boolean | undefined,
	version: number,
	pending: boolean
): { jog_radial_waveform: boolean; defaults_migration: number; radial_off_pending: boolean } {
	if (version >= _RADIAL_OFF_MIGRATION) {
		return {
			jog_radial_waveform: radial ?? DEFAULTS.jog_radial_waveform,
			defaults_migration: version,
			radial_off_pending: pending
		};
	}
	_bootMigrated = true;
	return { jog_radial_waveform: false, defaults_migration: PREFS_DEFAULTS_MIGRATION, radial_off_pending: true };
}

type LibraryPrefsValidated = {
	hide_broken_links: boolean;
	library_density?: RbUiPrefs['library_density'] | undefined;
	key_notation?: KeyNotation | undefined;
	library_hidden_columns?: HideableColumn[] | undefined;
	playlist_tree_width?: number | undefined;
};

type ThemePrefsValidated = {
	theme?: RbUiPrefs['theme'] | undefined;
};

type SkinMixerDeckPrefsParsed = {
	skinLook: SkinLookPrefs;
	radialOff: ReturnType<typeof _migrateRadialOff>;
	waveSplitMaster: ReturnType<typeof parseWaveSplitMaster>;
	crossfadeCurve: RbUiPrefs['crossfade_curve'] | undefined;
	horizontalWheelKnob: RbUiPrefs['horizontal_wheel_knob'] | undefined;
	deckLayout: ReturnType<typeof validateDeckLayoutFields>['deck_layout'];
	deckLayoutAnimate: ReturnType<typeof validateDeckLayoutFields>['deck_layout_animate'];
	deckLayoutDurationMs: ReturnType<typeof validateDeckLayoutFields>['deck_layout_duration_ms'];
	deckRightMirror: ReturnType<typeof validateDeckLayoutFields>['deck_right_mirror'];
	deckLeftMirror: boolean;
	playlistTreeView: ReturnType<typeof validatePlaylistTreeViewField>;
	lastPlaylist: ReturnType<typeof parseLastPlaylist>;
	autoSync: ReturnType<typeof parseAutoSync>;
	layoutOverride: LayoutOverride;
};

function _validateLibraryPrefs(parsed: Partial<RbUiPrefs>): LibraryPrefsValidated {
	if (typeof parsed.hide_broken_links !== 'boolean') {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (hide_broken_links is not a boolean) - ` +
				'clear the localStorage key to recover'
		);
	}
	const density = parsed.library_density;
	if (density !== undefined && density !== 'compact' && density !== 'cosy') {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (library_density must be 'compact'|'cosy') - ` +
				'clear the localStorage key to recover'
		);
	}
	const keyNotation = parsed.key_notation;
	if (keyNotation !== undefined && !isKeyNotation(keyNotation)) {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (key_notation must be ${KEY_NOTATIONS.join('|')}) - ` +
				'clear the localStorage key to recover'
		);
	}
	const treeWidth = parsed.playlist_tree_width;
	if (
		treeWidth !== undefined &&
		(typeof treeWidth !== 'number' ||
			!Number.isFinite(treeWidth) ||
			!Number.isInteger(treeWidth) ||
			treeWidth < PLAYLIST_TREE_WIDTH_MIN ||
			treeWidth > PLAYLIST_TREE_WIDTH_MAX)
	) {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (playlist_tree_width must be an integer from ` +
				`${PLAYLIST_TREE_WIDTH_MIN} through ${PLAYLIST_TREE_WIDTH_MAX}) - clear the localStorage key to recover`
		);
	}
	return {
		hide_broken_links: parsed.hide_broken_links,
		library_density: density,
		key_notation: keyNotation,
		library_hidden_columns:
			parsed.library_hidden_columns === undefined
				? undefined
				: validateHiddenColumns(
						parsed.library_hidden_columns,
						`${STORAGE_KEY}: malformed prefs blob (clear the localStorage key to recover)`
					),
		playlist_tree_width: treeWidth
	};
}

function _validateAutomationPrefs(parsed: Partial<RbUiPrefs>): void {
	if (parsed.beat_sync_max !== undefined && typeof parsed.beat_sync_max !== 'boolean') {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (beat_sync_max is not a boolean) - ` +
				'clear the localStorage key to recover'
		);
	}
	if (parsed.deck_autocue !== undefined && typeof parsed.deck_autocue !== 'boolean') {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (deck_autocue is not a boolean) - ` +
				'clear the localStorage key to recover'
		);
	}
	if (parsed.next_panel_collapsed !== undefined && typeof parsed.next_panel_collapsed !== 'boolean') {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (next_panel_collapsed is not a boolean) - ` +
				'clear the key to recover'
		);
	}
	if (
		parsed.recommended_panel_collapsed !== undefined &&
		typeof parsed.recommended_panel_collapsed !== 'boolean'
	) {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (recommended_panel_collapsed is not a boolean) - ` +
				'clear the key to recover'
		);
	}
	if (parsed.auto_play_enabled !== undefined && typeof parsed.auto_play_enabled !== 'boolean') {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (auto_play_enabled is not a boolean) - ` +
				'clear the localStorage key to recover'
		);
	}
	if (parsed.auto_play_unsaved !== undefined && typeof parsed.auto_play_unsaved !== 'boolean') {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (auto_play_unsaved is not a boolean) - ` +
				'clear the localStorage key to recover'
		);
	}
	if (parsed.auto_play_early_planning !== undefined && typeof parsed.auto_play_early_planning !== 'boolean') {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (auto_play_early_planning is not a boolean) - ` +
				`${JSON.stringify(parsed.auto_play_early_planning)}`
		);
	}
	if (
		parsed.auto_play_decide_after_s !== undefined &&
		!(
			typeof parsed.auto_play_decide_after_s === 'number' &&
			parsed.auto_play_decide_after_s >= DECIDE_AFTER_MIN_S &&
			parsed.auto_play_decide_after_s <= DECIDE_AFTER_MAX_S
		)
	) {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (auto_play_decide_after_s is not ${DECIDE_AFTER_MIN_S}..${DECIDE_AFTER_MAX_S}) - ` +
				`${JSON.stringify(parsed.auto_play_decide_after_s)}`
		);
	}
	if (
		parsed.auto_play_enforce_order !== undefined &&
		typeof parsed.auto_play_enforce_order !== 'boolean'
	) {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (auto_play_enforce_order is not a boolean) - ` +
				'clear the localStorage key to recover'
		);
	}
	if (
		parsed.auto_play_maximize_reach !== undefined &&
		typeof parsed.auto_play_maximize_reach !== 'boolean'
	) {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (auto_play_maximize_reach is not a boolean) - ` +
				'clear the localStorage key to recover'
		);
	}
}

function _validateThemeAndDevicePrefs(parsed: Partial<RbUiPrefs>): ThemePrefsValidated {
	const theme = parsed.theme;
	if (theme !== undefined && theme !== 'dark' && theme !== 'light') {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (theme must be 'dark'|'light') - ` +
				'clear the localStorage key to recover'
		);
	}
	if (parsed.hide_todo_settings !== undefined && typeof parsed.hide_todo_settings !== 'boolean') {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (hide_todo_settings is not a boolean) - ` +
				'clear the localStorage key to recover'
		);
	}
	if (parsed.usb_toast_enabled !== undefined && typeof parsed.usb_toast_enabled !== 'boolean') {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (usb_toast_enabled is not a boolean) - ` +
				'clear the localStorage key to recover'
		);
	}
	if (
		parsed.usb_toast_ms !== undefined &&
		(typeof parsed.usb_toast_ms !== 'number' ||
			!Number.isFinite(parsed.usb_toast_ms) ||
			parsed.usb_toast_ms <= 0)
	) {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (usb_toast_ms must be a positive finite number) - ` +
				'clear the localStorage key to recover'
		);
	}
	if (
		parsed.usb_auto_open_panel !== undefined &&
		typeof parsed.usb_auto_open_panel !== 'boolean'
	) {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (usb_auto_open_panel is not a boolean) - ` +
				'clear the localStorage key to recover'
		);
	}
	if (
		parsed.technically_working_animate !== undefined &&
		typeof parsed.technically_working_animate !== 'boolean'
	) {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (technically_working_animate is not a boolean) - ` +
				'clear the localStorage key to recover'
		);
	}
	return { theme };
}

function _validatePerformanceTogglePrefs(parsed: Partial<RbUiPrefs>): void {
	if (
		parsed.preview_beat_sync !== undefined &&
		parsed.preview_beat_sync !== 'off' &&
		parsed.preview_beat_sync !== 'tempo' &&
		parsed.preview_beat_sync !== 'phase'
	) {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (preview_beat_sync is not off, tempo or phase) - ` +
				'clear the localStorage key to recover'
		);
	}
	if (parsed.jog_radial_waveform !== undefined && typeof parsed.jog_radial_waveform !== 'boolean') {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (jog_radial_waveform is not a boolean) - ` +
				'clear the localStorage key to recover'
		);
	}
	if (parsed.show_agent_pins !== undefined && typeof parsed.show_agent_pins !== 'boolean') {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (show_agent_pins is not a boolean) - ` +
				'clear the localStorage key to recover'
		);
	}
	if (parsed.pin_visible_days !== undefined && !isPinVisibleDays(parsed.pin_visible_days)) {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (pin_visible_days must be an integer from ` +
				`${PIN_VISIBLE_DAYS_MIN} to ${PIN_VISIBLE_DAYS_MAX}) - ` +
				'clear the localStorage key to recover'
		);
	}
	if (parsed.show_stems !== undefined && typeof parsed.show_stems !== 'boolean') {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (show_stems is not a boolean) - ` +
				'clear the localStorage key to recover'
		);
	}
	if (parsed.wave_eq_reflect !== undefined && typeof parsed.wave_eq_reflect !== 'boolean') {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (wave_eq_reflect is not a boolean) - ` +
				'clear the localStorage key to recover'
		);
	}
	if (parsed.mixer_stems !== undefined && typeof parsed.mixer_stems !== 'boolean') {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (mixer_stems is not a boolean) - ` +
				'clear the localStorage key to recover'
		);
	}
	if (parsed.deck_hover_link !== undefined && typeof parsed.deck_hover_link !== 'boolean') {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (deck_hover_link is not a boolean) - ` +
				'clear the localStorage key to recover'
		);
	}
	if (parsed.compact_decks !== undefined && typeof parsed.compact_decks !== 'boolean') {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (compact_decks is not a boolean) - ` +
				'clear the localStorage key to recover'
		);
	}
}

function _parseSkinMixerAndDeckPrefs(parsed: Partial<RbUiPrefs>): SkinMixerDeckPrefsParsed {
	const skinLook = _migrateSkinDefaults(
		parseUiSkin(parsed.ui_skin),
		parseWaveformDesignPref(parsed.waveform_design),
		parseWavePalettePref(parsed.wave_palette),
		parsed.defaults_migration
	);
	if (parsed.radial_off_pending !== undefined && typeof parsed.radial_off_pending !== 'boolean') {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (radial_off_pending is not a boolean) - ` +
				'clear the localStorage key to recover'
		);
	}
	const radialOff = _migrateRadialOff(
		parsed.jog_radial_waveform,
		skinLook.defaults_migration,
		parsed.radial_off_pending ?? false
	);
	_radialBootWrite = radialOff.radial_off_pending;
	skinLook.defaults_migration = radialOff.defaults_migration;
	const waveSplitMaster = parseWaveSplitMaster(parsed.wave_split_master);
	let layoutOverride: LayoutOverride = {
		performance: LAYOUT_OVERRIDE_DEFAULT.performance,
		library: LAYOUT_OVERRIDE_DEFAULT.library
	};
	if (parsed.layout_override !== undefined) {
		try {
			layoutOverride = parseLayoutOverride(parsed.layout_override);
		} catch (err) {
			const msg = err instanceof Error ? err.message : String(err);
			throw new Error(
				`${STORAGE_KEY}: malformed prefs blob (layout_override: ${msg}) - ` +
					'clear the localStorage key to recover'
			);
		}
	}
	const crossfadeCurve = parsed.crossfade_curve;
	if (
		crossfadeCurve !== undefined &&
		crossfadeCurve !== 'magic' &&
		crossfadeCurve !== 'bass_swap' &&
		crossfadeCurve !== 'linear'
	) {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (crossfade_curve must be magic|bass_swap|linear) - ` +
				'clear the localStorage key to recover'
		);
	}
	const horizontalWheelKnob = parsed.horizontal_wheel_knob;
	if (
		horizontalWheelKnob !== undefined &&
		horizontalWheelKnob !== 'filter' &&
		horizontalWheelKnob !== 'color'
	) {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (horizontal_wheel_knob must be filter|color) - ` +
				'clear the localStorage key to recover'
		);
	}
	const {
		deck_layout: deckLayout,
		deck_layout_animate: deckLayoutAnimate,
		deck_layout_duration_ms: deckLayoutDurationMs,
		deck_right_mirror: deckRightMirror
	} = validateDeckLayoutFields(parsed, STORAGE_KEY);
	if (parsed.deck_left_mirror !== undefined && typeof parsed.deck_left_mirror !== 'boolean') {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (deck_left_mirror is not a boolean) - ` +
				'clear the localStorage key to recover'
		);
	}
	const deckLeftMirror = parsed.deck_left_mirror ?? DEFAULTS.deck_left_mirror;
	const playlistTreeView = validatePlaylistTreeViewField(parsed.playlist_tree_view, STORAGE_KEY);
	const lastPlaylist = parseLastPlaylist(parsed.last_playlist, STORAGE_KEY);
	const autoSync = parseAutoSync(parsed.auto_sync, STORAGE_KEY, DEFAULTS.auto_sync);
	return {
		skinLook,
		radialOff,
		waveSplitMaster,
		crossfadeCurve,
		horizontalWheelKnob,
		deckLayout,
		deckLayoutAnimate,
		deckLayoutDurationMs,
		deckRightMirror,
		deckLeftMirror,
		playlistTreeView,
		lastPlaylist,
		autoSync,
		layoutOverride
	};
}

function _parseConfirmPrefs(parsed: Partial<RbUiPrefs>): RbUiPrefs['confirm'] {
	const confirm = parsed.confirm ?? DEFAULTS.confirm;
	if (confirm !== null && typeof confirm !== 'object') {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (confirm must be an object) - ` +
				'clear the localStorage key to recover'
		);
	}
	const dropMode = (confirm as RbUiPrefs['confirm']).playlist_drop_mode;
	if (dropMode !== undefined && dropMode !== 'add' && dropMode !== 'move') {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (confirm.playlist_drop_mode must be 'add'|'move') - ` +
				'clear the localStorage key to recover'
		);
	}
	return confirm as RbUiPrefs['confirm'];
}

function _assembleLoadedPrefs(
	parsed: Partial<RbUiPrefs>,
	library: LibraryPrefsValidated,
	themePrefs: ThemePrefsValidated,
	skinMixerDeck: SkinMixerDeckPrefsParsed,
	confirm: RbUiPrefs['confirm']
): RbUiPrefs {
	const { theme } = themePrefs;
	const {
		skinLook,
		waveSplitMaster,
		crossfadeCurve,
		horizontalWheelKnob,
		deckLayout,
		deckLayoutAnimate,
		deckLayoutDurationMs,
		deckRightMirror,
		deckLeftMirror,
		playlistTreeView,
		lastPlaylist,
		autoSync,
		radialOff,
		layoutOverride
	} = skinMixerDeck;
	return {
		playlist_tree_width: library.playlist_tree_width ?? DEFAULTS.playlist_tree_width,
		hide_broken_links: library.hide_broken_links,
		library_density: library.library_density ?? DEFAULTS.library_density,
		sort_within_sections: parsed.sort_within_sections === true,
		sampler_enabled: parsed.sampler_enabled !== false,
		collapsed_sections: Array.isArray(parsed.collapsed_sections)
			? parsed.collapsed_sections
			: DEFAULTS.collapsed_sections,
		key_notation: library.key_notation ?? DEFAULTS.key_notation,
		library_hidden_columns: library.library_hidden_columns ?? DEFAULTS.library_hidden_columns,
		beat_sync_max: parsed.beat_sync_max ?? DEFAULTS.beat_sync_max,
		deck_autocue: parsed.deck_autocue ?? DEFAULTS.deck_autocue,
		next_panel_collapsed: parsed.next_panel_collapsed ?? DEFAULTS.next_panel_collapsed,
		recommended_panel_collapsed:
			parsed.recommended_panel_collapsed ?? DEFAULTS.recommended_panel_collapsed,
		auto_play_enabled: parsed.auto_play_enabled ?? DEFAULTS.auto_play_enabled,
		auto_play_unsaved: parsed.auto_play_unsaved ?? DEFAULTS.auto_play_unsaved,
		auto_play_enforce_order:
			parsed.auto_play_enforce_order ?? DEFAULTS.auto_play_enforce_order,
		auto_play_maximize_reach:
			parsed.auto_play_maximize_reach ?? DEFAULTS.auto_play_maximize_reach,
		auto_play_early_planning: parsed.auto_play_early_planning ?? DEFAULTS.auto_play_early_planning,
		auto_play_decide_after_s: parsed.auto_play_decide_after_s ?? DEFAULTS.auto_play_decide_after_s,
		theme: theme ?? DEFAULTS.theme,
		hide_todo_settings: parsed.hide_todo_settings ?? DEFAULTS.hide_todo_settings,
		auto_sync: autoSync,
		usb_toast_enabled: parsed.usb_toast_enabled ?? DEFAULTS.usb_toast_enabled,
		usb_toast_ms: parsed.usb_toast_ms ?? DEFAULTS.usb_toast_ms,
		usb_auto_open_panel: parsed.usb_auto_open_panel ?? DEFAULTS.usb_auto_open_panel,
		technically_working_animate:
			parsed.technically_working_animate ?? DEFAULTS.technically_working_animate,
		preview_beat_sync: parsed.preview_beat_sync ?? DEFAULTS.preview_beat_sync,
		jog_radial_waveform: radialOff.jog_radial_waveform,
		radial_off_pending: radialOff.radial_off_pending,
		show_agent_pins: parsed.show_agent_pins ?? DEFAULTS.show_agent_pins,
		pin_visible_days: isPinVisibleDays(parsed.pin_visible_days)
			? parsed.pin_visible_days
			: DEFAULTS.pin_visible_days,
		show_stems: parsed.show_stems ?? DEFAULTS.show_stems,
		mixer_stems: parsed.mixer_stems ?? DEFAULTS.mixer_stems,
		deck_hover_link: parsed.deck_hover_link ?? DEFAULTS.deck_hover_link,
		compact_decks: parsed.compact_decks ?? DEFAULTS.compact_decks,
		...skinLook,
		layout_override: { ...layoutOverride },
		wave_eq_reflect: parsed.wave_eq_reflect ?? DEFAULTS.wave_eq_reflect,
		wave_split_master: waveSplitMaster ?? DEFAULTS.wave_split_master,
		confirm: { ...confirm },
		last_playlist: lastPlaylist,
		spotify_library: parseSpotifyLibrary(parsed.spotify_library, STORAGE_KEY),
		cmdk_command_picks: parseCmdkCommandPicks(parsed.cmdk_command_picks, STORAGE_KEY),
		deck_layout: deckLayout ?? DEFAULTS.deck_layout,
		deck_layout_animate: deckLayoutAnimate ?? DEFAULTS.deck_layout_animate,
		deck_layout_duration_ms: deckLayoutDurationMs ?? DEFAULTS.deck_layout_duration_ms,
		deck_right_mirror: deckRightMirror ?? DEFAULTS.deck_right_mirror,
		deck_left_mirror: deckLeftMirror,
		..._topbarViewFromParsed(parsed),
		playlist_tree_view: playlistTreeView ?? DEFAULTS.playlist_tree_view,
		level_calibration: parseLevelCalibration(parsed.level_calibration, STORAGE_KEY, DEFAULTS.level_calibration),
		crossfade_curve: crossfadeCurve ?? DEFAULTS.crossfade_curve,
		horizontal_wheel_knob: horizontalWheelKnob ?? DEFAULTS.horizontal_wheel_knob,
		drag_sensitivity:
			parsed.drag_sensitivity === undefined
				? { ...DEFAULTS.drag_sensitivity }
				: validateDragSensitivity(parsed.drag_sensitivity, STORAGE_KEY),
		...SECTION_OVERLAY_PREF_DEFAULTS,
		...validateSectionOverlayPrefFields(parsed, STORAGE_KEY),
		...AUTO_ANALYZE_PREF_DEFAULTS,
		...validateAutoAnalyzePrefFields(parsed, STORAGE_KEY),
		...DECK_NUMBER_ART_PREF_DEFAULTS,
		...validateDeckNumberArtPrefFields(parsed as Record<string, unknown>, STORAGE_KEY),
		...SPEAKER_LINK_PREF_DEFAULTS,
		...validateSpeakerLinkPrefFields(parsed, STORAGE_KEY),
		...TEMPO_LANE_PREF_DEFAULTS,
		...validateTempoLanePrefFields(parsed, STORAGE_KEY),
		...VOCAL_LANE_PREF_DEFAULTS,
		...validateVocalLanePrefFields(parsed, STORAGE_KEY),
		...WAVE_HEIGHT_PREF_DEFAULTS,
		...validateWaveHeightPrefFields(parsed, STORAGE_KEY),
		...WAVE_GRID_LINES_PREF_DEFAULTS,
		...validateWaveGridLinesPrefFields(parsed as Record<string, unknown>, STORAGE_KEY),
		...WAVE_ZOOM_PREF_DEFAULTS,
		...validateWaveZoomPrefFields(parsed as Record<string, unknown>, STORAGE_KEY),
		...LYRICS_PREF_DEFAULTS,
		...validateLyricsPrefFields(parsed, STORAGE_KEY),
		...LIBRARY_FILTER_PREF_DEFAULTS,
		...validateLibraryFilterPrefFields(parsed, STORAGE_KEY),
		...mergePerfTierPrefsFromParsed(parsed, STORAGE_KEY),
		...APP_POSTURE_PREF_DEFAULTS,
		...mergeAppPosturePrefsFromParsed(parsed, STORAGE_KEY),
		...GIG_HELPER_PREF_DEFAULTS,
		...mergeGigHelperPrefsFromParsed(parsed, STORAGE_KEY),
		...APP_MODE_PREF_DEFAULTS,
		...mergeAppModePrefsFromParsed(parsed, STORAGE_KEY),
		compatible_filter: {
			...COMPATIBLE_FILTER_DEFAULTS,
			...validateCompatibleFilterPrefs(parsed.compatible_filter, STORAGE_KEY)
		},
		library_related_criteria: {
			...RELATED_CRITERIA_DEFAULTS,
			...validateRelatedCriteriaPrefs(parsed.library_related_criteria, STORAGE_KEY)
		},
		library_key_filter: validateLibraryKeyFilterField(parsed.library_key_filter, STORAGE_KEY),
		library_watcher_folders: Array.isArray(parsed.library_watcher_folders)
			? parsed.library_watcher_folders.filter((p): p is string => typeof p === 'string')
			: DEFAULTS.library_watcher_folders,
		...mergeDevUiPrefsFromParsed(parsed, STORAGE_KEY),
		variants: parseVariants(parsed.variants, STORAGE_KEY),
		...mergeDeckDefaultPrefsFromParsed(parsed, STORAGE_KEY),
		// A malformed stored value throws (fail fast), like the merges above.
		beat_jump_sizes:
			parsed.beat_jump_sizes === undefined
				? DEFAULTS.beat_jump_sizes
				: beatJumpSizesFromPrefs(parsed.beat_jump_sizes)
	};
}

/** TOPVIEW-01..06: the top-bar view fields, each falling back to its default. */
function _topbarViewFromParsed(parsed: Parameters<typeof validateTopbarViewFields>[0]): TopbarViewPrefsState {
	const v = validateTopbarViewFields(parsed, STORAGE_KEY);
	return {
		perf_view: v.perf_view ?? DEFAULTS.perf_view,
		show_pads: v.show_pads ?? DEFAULTS.show_pads,
		auto_play_two_deck: v.auto_play_two_deck ?? DEFAULTS.auto_play_two_deck
	};
}

function _load(): RbUiPrefs {
	const storage = _storage();
	if (storage === null) return { ...DEFAULTS };
	const raw = storage.getItem(STORAGE_KEY);
	// First run in this storage. The daemon copy may still hold a true a
	// pre-migration build wrote, so migration 2 supersedes it here too; the
	// hydrator rewrites it only if disk really holds true (Sol P1, PR #6829).
	if (raw === null) return { ...DEFAULTS, radial_off_pending: true };
	const parsed = JSON.parse(raw) as Partial<RbUiPrefs>; // malformed JSON throws - intended
	const library = _validateLibraryPrefs(parsed);
	_validateAutomationPrefs(parsed);
	const themePrefs = _validateThemeAndDevicePrefs(parsed);
	_validatePerformanceTogglePrefs(parsed);
	const skinMixerDeck = _parseSkinMixerAndDeckPrefs(parsed);
	const confirm = _parseConfirmPrefs(parsed);
	return _assembleLoadedPrefs(parsed, library, themePrefs, skinMixerDeck, confirm);
}

function _persist(): void {
	_storage()?.setItem(STORAGE_KEY, JSON.stringify($state.snapshot(uiPrefs)));
}

/** One shared write queue (issue #1578) - see disk-write-chain.ts. */
const _syncDiskPrefs = syncDiskPrefs;

// -------------------------------------------------------- public API

/** Reactive prefs singleton. Read anywhere; write ONLY via the setters
 * below so every change persists. */
export const uiPrefs = $state<RbUiPrefs>(_load());
installKeyNotationSource(() => uiPrefs.key_notation);

_applyThemeDom(uiPrefs.theme);
_applyWavePaletteDom(effectiveWavePalette(uiPrefs.wave_palette, uiPrefs.ui_skin));
applyUiSkinDom(uiPrefs.ui_skin);
if (_bootMigrated) _persist();
applyDragSensitivity(uiPrefs.drag_sensitivity);
/** The DJ picked the radial waveform this session, so no disk read may undo it. */
let _radialPicked = false;

/** Bumped on every local AutoPlay write; the hydrator skips a body read before it. */
let _autoPlayRev = 0;

/** Migration 2's claim on the radial value is over: disk agrees or the DJ picked. */
function _clearRadialOffPending(): void {
	if (!uiPrefs.radial_off_pending) return;
	uiPrefs.radial_off_pending = false;
	_persist();
}

/** Push migration 2 to the daemon copy, or the next ui-prefs GET would hand
 * the old true straight back. The marker clears only when the daemon
 * acknowledges; a failed write leaves it set, so the next boot resends it and
 * the hydrator keeps skipping the disk value meanwhile, and the failure is
 * reported, never swallowed (Sol P1 x2, PR #6829). */
function _settleRadialOff(): void {
	_syncDiskPrefs({ jog_radial_waveform: false }, putDiskPrefsVerified)
		.then(_clearRadialOffPending)
		.catch((cause) =>
			reportSettingSaveError('Could not save the radial waveform default to disk; retrying next launch', cause)
		);
}
if (_radialBootWrite) _settleRadialOff();

export function setHideBrokenLinks(next: boolean): void {
	setLibraryBrowserDiskPref(uiPrefs, _persist, _syncDiskPrefs, 'hide_broken_links', next);
}

/** Persist the tree width after clamping it to its documented 220-520px range. */
export function setPlaylistTreeWidth(next: number): void {
	if (!Number.isFinite(next)) {
		throw new Error('playlist tree width must be a finite number');
	}
	uiPrefs.playlist_tree_width = Math.round(
		Math.min(PLAYLIST_TREE_WIDTH_MAX, Math.max(PLAYLIST_TREE_WIDTH_MIN, next))
	);
	_persist();
}

/** Remember which playlist the first browser pane holds, so the next boot of
 * /performance restores it instead of opening on a blank track table. Writes
 * only when the identity actually changed - every pane load calls this. */
export function setLastPlaylist(next: LastPlaylistPref | null): void {
	const current = uiPrefs.last_playlist;
	if (next === null) {
		if (current === null) return;
		uiPrefs.last_playlist = null;
		_persist();
		return;
	}
	if (
		current !== null &&
		current.playlist_id === next.playlist_id &&
		current.name === next.name &&
		current.kind === next.kind
	) {
		return;
	}
	uiPrefs.last_playlist = { ...next };
	_persist();
}

export const { toggleSpotifyPinned, rememberSpotifyRecent, setSpotifyShrinkMain } = makeSpotifyLibrarySetters(
	uiPrefs,
	_persist,
	(patch) => void _syncDiskPrefs(patch)
);
export function setSortWithinSections(next: boolean): void {
	setLibraryBrowserDiskPref(uiPrefs, _persist, _syncDiskPrefs, 'sort_within_sections', next);
}

/** Edge sampler on/off (spec edge-sampler 7.4). */
export function setSamplerEnabledPref(next: boolean): void {
	uiPrefs.sampler_enabled = next;
	_persist();
}

export function setSectionCollapsed(sectionId: string, collapsed: boolean): void {
	const rest = uiPrefs.collapsed_sections.filter((id) => id !== sectionId);
	const next = collapsed ? [...rest, sectionId] : rest;
	setLibraryBrowserDiskPref(uiPrefs, _persist, _syncDiskPrefs, 'collapsed_sections', next);
}

export function setLibraryDensity(next: LibraryDensity): void {
	setLibraryBrowserDiskPref(uiPrefs, _persist, _syncDiskPrefs, 'library_density', next);
}
export function setKeyNotation(next: KeyNotation): void {
	setLibraryBrowserDiskPref(uiPrefs, _persist, _syncDiskPrefs, 'key_notation', next);
}
/** LIBUX-54: show or hide one track-table column (the header chooser's checkbox). */
export function toggleLibraryColumn(col: HideableColumn): void {
	setLibraryBrowserDiskPref(
		uiPrefs,
		_persist,
		_syncDiskPrefs,
		'library_hidden_columns',
		toggleHiddenColumn(uiPrefs.library_hidden_columns, col)
	);
}
/** LIBUX-54: the chooser's "Show all columns". */
export function showAllLibraryColumns(): void {
	setLibraryBrowserDiskPref(uiPrefs, _persist, _syncDiskPrefs, 'library_hidden_columns', []);
}

/** DECKUX-37: turning Max ON also turns Beat Sync ON for loaded decks (through
 * the deck SYNC command); turning it OFF leaves every deck as it is. */
export function setBeatSyncMax(next: boolean): void {
	const previous = uiPrefs.beat_sync_max;
	setTopbarDiskPref(uiPrefs, _persist, _syncDiskPrefs, 'beat_sync_max', next);
	announceBeatSyncMaxChange(previous, next).catch((error: unknown) => {
		console.error('[beat-sync-max] enabling Beat Sync on loaded decks failed', error);
	});
}

/** Local-only: the engine holds no preview voice, so this never leaves the page. */
export function setPreviewBeatSync(next: PreviewBeatSync): void {
	uiPrefs.preview_beat_sync = next;
	_persist();
}

/** DECKCTL-6235: an agent's PUT of `deck_autocue` reaches an open page. */
export const refreshAutoCueFromDisk = makeAutoCueRefresher(uiPrefs, _persist, reportSettingSaveError);

/** DECKCTL-6235: the deck AU button. The write runs on the shared disk chain
 * with the verified (throwing) PUT, so the command confirms only a saved
 * choice (Codex P2, PR #6235). A failed save rejects; when it is the newest
 * choice, the live value goes back to the last one disk accepted, which is
 * the value before the first unsaved choice unless a queued save landed
 * since, so a run of failed choices never leaves an unsaved one showing.
 * Either way the newest choice ends by re-reading disk, since another client
 * may have saved a value this page only learned through an event it already
 * consumed (Codex P2, #6235). */
let _autoCueRev = 0;
let _autoCuePending = 0;
let _autoCueSaved = false;
export async function setAutoCue(next: boolean): Promise<void> {
	if (_autoCuePending === 0) _autoCueSaved = uiPrefs.deck_autocue;
	_autoCuePending += 1;
	const rev = ++_autoCueRev;
	let reread: Promise<boolean> | null = null;
	uiPrefs.deck_autocue = next;
	_persist();
	try {
		await _syncDiskPrefs({}, () => putDiskPrefsVerified({ deck_autocue: next }));
		_autoCueSaved = next;
		// A ui_prefs re-read that ran while this PUT was pending may have put
		// the older disk value back, and another client may have written after
		// this PUT landed, its event already consumed. So the newest choice
		// shows what it saved, then settles on what disk holds now before the
		// command confirms (Codex P2s, #6235).
		if (rev === _autoCueRev) {
			if (uiPrefs.deck_autocue !== next) {
				uiPrefs.deck_autocue = next;
				_persist();
			}
			reread = refreshAutoCueFromDisk();
		}
	} catch (error) {
		if (rev === _autoCueRev) {
			uiPrefs.deck_autocue = _autoCueSaved;
			_persist();
			void refreshAutoCueFromDisk();
		}
		throw error;
	} finally {
		_autoCuePending -= 1;
	}
	// Outside the try: the save landed, so a failed re-read must not roll it back.
	if (reread !== null && !(await reread)) {
		throw new Error('Auto cue (AU) was saved, but this page could not re-read it to confirm');
	}
}

/** Save AutoPlay to disk; the unsaved marker clears only when the daemon
 * acknowledges the latest write, and a failure is reported (#10717). */
function _saveAutoPlay(next: boolean, rev: number): void {
	_syncDiskPrefs({ auto_play_enabled: next }, putDiskPrefsVerified)
		.then(() => {
			if (rev !== _autoPlayRev || !uiPrefs.auto_play_unsaved) return;
			uiPrefs.auto_play_unsaved = false;
			_persist();
		})
		.catch((cause) =>
			reportSettingSaveError(
				'Could not save AutoPlay to disk; this browser keeps it and retries next launch',
				cause
			)
		);
}

export function setAutoPlayEnabled(next: boolean): void {
	uiPrefs.auto_play_enabled = next;
	uiPrefs.auto_play_unsaved = true;
	_persist();
	_saveAutoPlay(next, ++_autoPlayRev);
}

if (uiPrefs.auto_play_unsaved) _saveAutoPlay(uiPrefs.auto_play_enabled, _autoPlayRev);

export function setAutoPlayEnforceOrder(next: boolean): void {
	setTopbarDiskPref(uiPrefs, _persist, _syncDiskPrefs, 'auto_play_enforce_order', next);
}

export function setAutoPlayMaximizeReach(next: boolean): void {
	setTopbarDiskPref(uiPrefs, _persist, _syncDiskPrefs, 'auto_play_maximize_reach', next);
}

/** AUTOPLAN-01: local to this page, like the other AutoPlay menu choices that no engine reads. */
export function setAutoPlayEarly(change: { enabled?: boolean; decide_after_s?: number }): void {
	if (change.enabled !== undefined) uiPrefs.auto_play_early_planning = change.enabled;
	if (change.decide_after_s !== undefined) uiPrefs.auto_play_decide_after_s = change.decide_after_s;
	_persist();
}

/** Collapse one suggestion panel while retaining the other panel's state. */
export function setLibraryPanelCollapsed(panel: LibraryPanel, collapsed: boolean): void {
	if (panel === 'next') uiPrefs.next_panel_collapsed = collapsed;
	else uiPrefs.recommended_panel_collapsed = collapsed;
	_persist();
}

/** The Next / Remixes / Vocals library filter setters, built against this
 * module's own uiPrefs/_persist (library-filter-prefs.ts). */
export const {
	setNextOnlyFilter,
	setRemixesFilter,
	setVocalsFilter,
	setAvailableOfflineFilter
} = makeLibraryFilterSetters(uiPrefs, _persist, (patch) => void _syncDiskPrefs(patch));

export function setTheme(next: UiTheme): void {
	uiPrefs.theme = next;
	_applyThemeDom(next);
	_persist();
	void _syncDiskPrefs({ theme: next });
}

export function toggleTheme(): void {
	setTheme(uiPrefs.theme === 'dark' ? 'light' : 'dark');
}

export function setHideTodoSettings(next: boolean): void {
	uiPrefs.hide_todo_settings = next;
	_persist();
	void _syncDiskPrefs({ hide_todo_settings: next });
}

export function setTechnicallyWorkingAnimate(next: boolean): void {
	uiPrefs.technically_working_animate = next;
	_persist();
	void _syncDiskPrefs({ technically_working_animate: next });
}

// The DJ's own pick is queued behind any pending migration write, so it lands
// last on disk and ends the migration's claim on the value.
export const { setJogRadialWaveform } = makeJogRadialWaveformSetters(
	uiPrefs,
	() => {
		_radialPicked = true;
		uiPrefs.radial_off_pending = false;
		_persist();
	},
	(patch) => void _syncDiskPrefs(patch)
);

/** STRUCT-05: labeled section bands over the waveforms (default off). */
export const { setShowSections, toggleShowSections } = makeSectionOverlaySetters(
	uiPrefs,
	_persist,
	(patch) => void _syncDiskPrefs(patch)
);

/** ANALYSIS-11724: which lanes the analyze-on-import drain runs automatically. */
export const {
	setAutoAnalyzeGrid,
	setAutoAnalyzeKey,
	setAutoAnalyzeSections,
	setAutoAnalyzeVocals
} = makeAutoAnalyzeSetters(uiPrefs, _persist, (patch) => void _syncDiskPrefs(patch));

/** AUDIO-DEVICE-06: auto-reconnect a stalled Bluetooth main speaker (default auto). */
export const { setSpeakerAutoRecover } = makeSpeakerLinkSetters(
	uiPrefs,
	_persist,
	(patch) => void _syncDiskPrefs(patch)
);

/** Tempo-change lane (PR #8342): lane visibility and curve scale (default on, fixed_10). */
export const { setShowTempoLane, setTempoLaneScale } = makeTempoLaneSetters(
	uiPrefs,
	_persist,
	(patch) => void _syncDiskPrefs(patch)
);

/** Vocal lane visibility (issue #10238): blue vocal bars on deck rows and overview strip (default on). */
export const { setShowVocalLane } = makeVocalLaneSetters(
	uiPrefs,
	_persist,
	(patch) => void _syncDiskPrefs(patch)
);

/** Beat grid lines (issue #10244): beats_bars (default), bars, off. */
export const { setWaveGridLines } = makeWaveGridLinesSetter(
	uiPrefs,
	_persist,
	(patch) => void _syncDiskPrefs(patch)
);

/** Persist the agent-pin layer through both local state and its HTTP twin. */
export function setShowAgentPins(next: boolean): void {
	uiPrefs.show_agent_pins = next;
	_persist();
	void _syncDiskPrefs({ show_agent_pins: next });
}

/** Local-only: how many days comment pins stay visible. 0 shows every pin. */
export function setPinVisibleDays(next: number): void {
	if (!isPinVisibleDays(next)) {
		throw new Error(
			`pin_visible_days must be an integer from ${PIN_VISIBLE_DAYS_MIN} to ${PIN_VISIBLE_DAYS_MAX}, got ${String(next)}`
		);
	}
	uiPrefs.pin_visible_days = next;
	_persist();
}

/** Local-only: remember a Cmd K command pick for frecency (CMDK-9465). */
export function noteCmdkCommandPick(key: string): void {
	uiPrefs.cmdk_command_picks = notePick(uiPrefs.cmdk_command_picks, key, Date.now());
	_persist();
}

export function setShowStems(next: boolean): void {
	uiPrefs.show_stems = next;
	_persist();
	void _syncDiskPrefs({ show_stems: next });
}

/** The mixer's S button and the `mixer_stems` command. Saved in this browser,
 * the same store the MORE/LESS layout reads first. */
export function setMixerStems(next: boolean): void {
	if (typeof next !== 'boolean') throw new TypeError(`mixer_stems must be boolean, got ${String(next)}`);
	uiPrefs.mixer_stems = next;
	_persist();
}

export function setDeckHoverLink(next: boolean): void {
	uiPrefs.deck_hover_link = next;
	_persist();
	void _syncDiskPrefs({ deck_hover_link: next });
}

export function setCompactDecks(next: boolean): void {
	if (typeof next !== 'boolean') throw new TypeError(`compact_decks must be boolean, got ${String(next)}`);
	uiPrefs.compact_decks = next;
	_persist();
	void _syncDiskPrefs({ compact_decks: next });
}

/** 'auto' follows the active skin; a concrete design overrides it. */
export function setWaveformDesign(next: WaveformDesignPref): void {
	if (parseWaveformDesignPref(next) === undefined) {
		throw new Error('waveform_design must be auto|tri-band|mono|line|blocks|rgb, got undefined');
	}
	uiPrefs.waveform_design = next;
	_persist();
}

export function setWaveHeight(next: WaveHeight): void {
	if (parseWaveHeight(next) === undefined) {
		throw new Error(`wave_height must be full|half, got ${String(next)}`);
	}
	uiPrefs.wave_height = next;
	_persist();
	void _syncDiskPrefs({ wave_height: next });
}

export function setWaveEqReflect(next: boolean): void {
	if (typeof next !== 'boolean') throw new TypeError(`wave_eq_reflect must be a boolean, got ${String(next)}`);
	uiPrefs.wave_eq_reflect = next;
	_persist();
}

/** 'auto' follows the active skin; a concrete palette overrides it. */
export function setWavePalette(next: WavePalettePref): void {
	if (parseWavePalettePref(next) === undefined) {
		throw new Error('wave_palette must be auto|rekordbox|legacy|mono|blue, got undefined');
	}
	uiPrefs.wave_palette = next;
	_applyWavePaletteDom(effectiveWavePalette(next, uiPrefs.ui_skin));
	_persist();
}

export function setCrossfadeCurve(next: CrossfadeCurve): void {
	if (next !== 'magic') {
		throw new Error(`crossfade curve ${next} is not implemented - see PARITY-TODO`);
	}
	uiPrefs.crossfade_curve = next;
	_persist();
}

/** R6: base gain and falloff; applies live to every drag, then persists. */
export function setDragSensitivity(next: Partial<DragSensitivityPrefs>): void {
	if (next === null || typeof next !== 'object' || Array.isArray(next)) {
		throw new TypeError(`setDragSensitivity: expected an object with base and/or falloff_px, got ${String(next)}`);
	}
	const keys = Object.keys(next);
	const unknown = keys.filter((k) => k !== 'base' && k !== 'falloff_px');
	if (keys.length === 0 || unknown.length > 0) {
		throw new TypeError(
			`setDragSensitivity: only base and falloff_px may be set, got ${keys.length === 0 ? 'no fields' : unknown.join(', ')}`
		);
	}
	const merged = validateDragSensitivity({ ...uiPrefs.drag_sensitivity, ...next }, 'setDragSensitivity');
	uiPrefs.drag_sensitivity = merged;
	applyDragSensitivity(merged);
	_persist();
}

/** Agent parity (R6): the same store the Config slider writes. */
export interface DragSensitivityBridge {
	get: () => DragSensitivityPrefs;
	set: (next: Partial<DragSensitivityPrefs>) => void;
	reset: () => void;
}

if (typeof window !== 'undefined') {
	(window as Window & { __mdtDragSensitivity?: DragSensitivityBridge }).__mdtDragSensitivity = {
		get: () => ({ ...uiPrefs.drag_sensitivity }),
		set: (next) => setDragSensitivity(next),
		reset: () => setDragSensitivity({ base: DRAG_DEFAULTS.base, falloff_px: DRAG_DEFAULTS.falloffPx })
	};
}

export function setHorizontalWheelKnob(next: HorizontalWheelKnob): void {
	if (next !== 'filter' && next !== 'color') {
		throw new Error(`horizontal_wheel_knob must be filter|color, got ${next}`);
	}
	uiPrefs.horizontal_wheel_knob = next;
	_persist();
}

export const {
	setDeckLayoutMode,
	toggleDeckLayoutMode,
	setDeckLayoutAnimate,
	setDeckLayoutDurationMs,
	setDeckRightMirror,
	setDeckLeftMirror
} = makeDeckLayoutSetters(uiPrefs, _persist, (patch) => void _syncDiskPrefs(patch));

export const { setPerfView, setShowPads, setAutoPlayTwoDeck } = makeTopbarViewSetters(
	uiPrefs,
	_persist,
	(patch) => void _syncDiskPrefs(patch)
);
export { PERF_VIEWS, isPerfView, type PerfView } from './topbar-view-prefs';

export const { setPlaylistTreeView, togglePlaylistTreeView } = makePlaylistTreeViewSetters(
	uiPrefs,
	_persist,
	(patch) => void _syncDiskPrefs(patch)
);

export const {
	setLyricsGlobal,
	toggleLyricsGlobal,
	setLyricsLibraryCol,
	setLyricsHoverScrub,
	setLyricsLoadStrategy,
	setLyricsWaveformOverlay,
	setLyricsDeckLine
} = makeLyricsPrefSetters(uiPrefs, _persist, (patch) => void _syncDiskPrefs(patch));

export const { setPerfTier } = bindPerfTierPrefSetters(uiPrefs, _persist, (p) => void _syncDiskPrefs(p));
const { setAppPosture: _setAppPostureRaw } = bindAppPosturePrefSetters(
	uiPrefs,
	_persist,
	(p) => void _syncDiskPrefs(p)
);
export function setAppPosture(next: AppPosturePref): void {
	const previous = uiPrefs.app_posture;
	_setAppPostureRaw(next);
	tryOfferGigHelperPromptOnPostureChange(previous, next, uiPrefs.gig_helper);
}
export const { setGigHelper } = bindGigHelperPrefSetters(uiPrefs, _persist, (p) => void _syncDiskPrefs(p));
export const { setAppMode } = bindAppModePrefSetters(uiPrefs, _persist, (p) => void _syncDiskPrefs(p));
/** "Show developer pages" - local-only, see dev-ui-prefs.ts. */
export const { setShowDevUi } = makeDevUiPrefSetters(uiPrefs, _persist);
/** Taste variant picks - local-only, see variant-prefs.ts. setVariant reassigns variants so each
 * instance gets a fresh object. */
export const { readVariant, setVariant } = makeVariantPrefAccessors(uiPrefs, _persist);
export { onVariantChange };
/** "Quantize on new decks" (SETSTUB-02) - see deck-default-prefs.ts. */
export const { setDeckDefaultQuantize, setDeckNeedleLock } = makeDeckDefaultPrefSetters(uiPrefs, _persist, (p) => void _syncDiskPrefs(p));
// Seed the deck store from the stored value without it importing this module.
announceDeckDefaults(uiPrefs);
// DECKUX-10470: restore the per-deck beat jump size, then persist every change
// the `beat_jump_size` command makes, without the leaf module importing this one.
restoreBeatJumpSizes(uiPrefs.beat_jump_sizes);
onBeatJumpSizeChange(() => {
	uiPrefs.beat_jump_sizes = beatJumpSizesToPrefs();
	_persist();
	void _syncDiskPrefs({ beat_jump_sizes: { ...uiPrefs.beat_jump_sizes } });
});

export function setAutoSyncDestination(dest: AutoSyncDestination, next: boolean): void {
	uiPrefs.auto_sync[dest] = next;
	_persist();
	void _syncDiskPrefs({ auto_sync: { ...uiPrefs.auto_sync } });
}

export const { setLevelCalibrationCapture, setLevelCalibrationDisabled } = makeLevelCalibrationSetters(
	uiPrefs,
	_persist,
	(patch) => void _syncDiskPrefs(patch)
);

/** Verified disk writes (PR #4014): each commits only after its PUT lands and
 * rejects otherwise; see verified-pref-writes.ts. Loaded on first use, since
 * nothing at first paint writes (library bundle budget). */
type VerifiedWriters = ReturnType<typeof import('./verified-pref-writes').makeVerifiedPrefWriters>;
let _verifiedLoad: Promise<VerifiedWriters> | null = null;
function _verified(): Promise<VerifiedWriters> {
	_verifiedLoad ??= import('./verified-pref-writes').then((m) =>
		m.makeVerifiedPrefWriters({
			uiPrefs,
			persist: _persist,
			sync: _syncDiskPrefs,
			put: putDiskPrefsVerified,
			unsavedConfirm: _unsavedConfirm,
			libraryKeyFilterUnsaved: _libraryKeyFilterUnsaved,
			markLibraryKeyFilterSaved: (writeRev) => {
				if (_libraryKeyFilterUnsaved.rev === writeRev) _libraryKeyFilterUnsaved.rev = 0;
			}
		})
	);
	return _verifiedLoad;
}
/** Confirm keys disk has not acknowledged yet, by write revision; hydration keeps them. */
const _unsavedConfirm = new Map<string, number>();
let _confirmRev = 0;
const _libraryKeyFilterUnsaved = { rev: 0 };
let _libraryKeyFilterWriteRev = 0;
export const patchCompatibleFilter = (patch: Partial<CompatibleFilterPrefs>): Promise<void> =>
	_verified().then((v) => v.patchCompatibleFilter(patch));
/** LIBUX-10046: Related Tracks criteria, committed after the PUT lands. */
export const patchRelatedCriteria = (patch: Partial<RelatedCriteriaPrefs>): Promise<void> =>
	_verified().then((v) => v.patchRelatedCriteria(patch));
/** LIBUX-10046: Reset restores the #9753 defaults. */
export const resetRelatedCriteria = (): Promise<void> =>
	patchRelatedCriteria({ ...RELATED_CRITERIA_DEFAULTS });
/** LIBM-129 v1 placeholder: paths must already pass syntax + existence checks. */
export const setLibraryWatcherFolders = (paths: readonly string[]): Promise<void> =>
	_verified().then((v) => v.setLibraryWatcherFolders(paths));
/** Issue #7599: multi-select Camelot key wheel filter (local state updates immediately). */
export function setLibraryKeyFilter(keys: readonly string[]): Promise<void> {
	const next = canonicalizeLibraryKeyFilter(keys);
	uiPrefs.library_key_filter = next;
	_persist();
	const writeRev = ++_libraryKeyFilterWriteRev;
	_libraryKeyFilterUnsaved.rev = writeRev;
	return _verified()
		.then((v) => v.setLibraryKeyFilterDisk(writeRev))
		.catch((err: unknown) => {
			// A failed PUT is not "pending" forever: release it and re-read disk, so
			// the table shows what is really saved and later agent writes apply
			// (Codex P2, #7743). The caller still gets the error.
			if (_libraryKeyFilterUnsaved.rev === writeRev) {
				_libraryKeyFilterUnsaved.rev = 0;
				_libraryKeyFilterRereadDeferred = true;
			}
			throw err;
		})
		.finally(() => {
			// A re-read skipped while this write was in flight runs now that it settled.
			if (_libraryKeyFilterRereadDeferred && _libraryKeyFilterUnsaved.rev === 0) {
				_libraryKeyFilterRereadDeferred = false;
				rereadLibraryKeyFilter();
			}
		});
}
let _libraryKeyFilterRereadDeferred = false;
const _hydrateLibraryKeyFilterFromDisk = makeLibraryKeyFilterRehydrator({
	uiPrefs,
	persist: () => _persist(),
	isUnsaved: () => _libraryKeyFilterUnsaved.rev !== 0,
	writeRev: () => _libraryKeyFilterWriteRev,
	onDeferred: () => {
		_libraryKeyFilterRereadDeferred = true;
	}
});
/** The last failed disk re-read of the key filter, shown on the Key control. */
export const libraryKeyFilterSync = $state({ error: null as string | null });
/** Issue #7599: adopt a key filter written elsewhere (agent POST, another
 * window). A failure is kept visible on the Key control, never swallowed. */
let _libraryKeyFilterRereadGen = 0;
export function rereadLibraryKeyFilter(): void {
	// Only the latest re-read may set or clear the error, so an older one that
	// resolves late cannot hide a newer failure (Sol P1, #7743).
	const gen = ++_libraryKeyFilterRereadGen;
	_hydrateLibraryKeyFilterFromDisk().then(
		() => {
			if (gen === _libraryKeyFilterRereadGen) libraryKeyFilterSync.error = null;
		},
		(err: unknown) => {
			if (isPageGoingAway()) return;
			console.error('[library-key-filter] disk re-read failed', err);
			if (gen !== _libraryKeyFilterRereadGen) return;
			const detail = err instanceof Error ? err.message : String(err);
			libraryKeyFilterSync.error = `Key filter could not be re-read from disk: ${detail}`;
		}
	);
}

/** Reset a remembered confirm choice to "ask". Rejects (and keeps the
 * remembered choice) when the disk delete fails, so the caller can show it. */
export function clearConfirmPref(key: keyof RbUiPrefs['confirm']): Promise<void> {
	const rev = _unsavedConfirm.get(key);
	return _verified().then((v) => v.clearConfirmPref(key, rev));
}

/** Remember a confirm choice (resetting to "ask" is `clearConfirmPref`): live at
 * once, and unsaved until disk acknowledges it, so a failed PUT is resent by
 * the next confirm write and hydration cannot drop it. A failed save is shown
 * through the settings save-error sink (an error toast; Sol P1, PR #4014). */
export function setConfirmPref<K extends keyof RbUiPrefs['confirm']>(
	key: K,
	value: Exclude<RbUiPrefs['confirm'][K], undefined>
): void {
	uiPrefs.confirm[key] = value;
	_persist();
	_unsavedConfirm.set(key, ++_confirmRev);
	_verified()
		.then((v) => v.saveUnsavedConfirm())
		.catch((err: unknown) => reportSettingSaveError(`Confirmation choice not saved to disk: ${err}`, err));
}

/** Pull on-disk confirm + theme prefs once (daemon may have remembered choices). */
export const hydrateConfirmPrefsFromDisk = makePrefsHydrator({
	uiPrefs,
	persist: _persist,
	applyThemeDom: _applyThemeDom,
	storageKey: STORAGE_KEY,
	defaults: DEFAULTS,
	isConfirmUnsaved: (key) => _unsavedConfirm.has(key),
	isDiskValueSuperseded: (key) =>
		(key === 'jog_radial_waveform' && uiPrefs.radial_off_pending) ||
		(key === 'auto_play_enabled' && uiPrefs.auto_play_unsaved),
	// Only a boolean or absence is a real answer: anything else is a broken
	// daemon response, reported, and the migration keeps its claim (Sol P1, #6829).
	onSupersededDiskValue: (key, diskValue) => {
		if (key === 'auto_play_enabled') return; // the queued save carries the local value
		// The DJ picked while the GET was in flight: their value stands.
		if (_radialPicked) return;
		if (diskValue === true && uiPrefs.jog_radial_waveform === true) return;
		if (diskValue === true) _settleRadialOff();
		else if (diskValue === false || diskValue === undefined) _clearRadialOffPending();
		else
			reportSettingSaveError(
				`Disk returned a malformed radial waveform setting (${JSON.stringify(diskValue)}); keeping it off`,
				new TypeError(`jog_radial_waveform must be a boolean, got ${typeof diskValue}`)
			);
	},
	isLibraryKeyFilterUnsaved: () => _libraryKeyFilterUnsaved.rev !== 0,
	localWriteRev: (key) => (key === 'auto_play_enabled' ? _autoPlayRev : 0)
});

export function setUiSkin(next: UiSkin): void {
	if (parseUiSkin(next) === undefined) throw new Error('ui_skin must be set, got undefined');
	uiPrefs.ui_skin = next;
	applyUiSkinDom(next);
	// An 'auto' palette follows the skin, so the CSS-var painters repaint too.
	_applyWavePaletteDom(effectiveWavePalette(uiPrefs.wave_palette, next));
	_persist();
}

/** Issue #10199: one surface's preset; 'auto' follows the skin. Same write path as
 * Settings > Performance/Library layout and the set_layout_preset command. */
export function setLayoutOverride(surface: LayoutSurface, preset: unknown): void {
	const s = parseLayoutSurface(surface);
	const p = parseLayoutPreset(s, preset);
	uiPrefs.layout_override = { ...uiPrefs.layout_override, [s]: p };
	_persist();
}

/** Top-bar skin button: advance to the next skin in UI_SKIN_CHOICES order and
 * wrap. Same write path as Settings > UI skin and the set_skin command. */
export function cycleUiSkin(): UiSkin {
	const next = nextUiSkin(uiPrefs.ui_skin);
	setUiSkin(next);
	return next;
}

export function setWaveSplitMaster(next: WaveSplitMaster): void {
	if (parseWaveSplitMaster(next) === undefined) throw new Error('wave_split_master must be set, got undefined');
	uiPrefs.wave_split_master = next;
	_persist();
}

/** WAVEZOOM-6802: the main waveform's zoom step. With no deck, or while the
 * zoom is linked, it sets the shared step; an unlinked deck keeps its own. */
export function setWaveZoom(next: WaveZoom, deck?: DeckId): void {
	if (parseWaveZoom(next) === undefined) throw new Error('wave_zoom must be set, got undefined');
	if (deck === undefined || uiPrefs.wave_zoom_linked) uiPrefs.wave_zoom = next;
	else uiPrefs.wave_zoom_decks = { ...uiPrefs.wave_zoom_decks, [`${deck}`]: next };
	_persist();
}

/** WAVEZOOM-6802b: one ladder step in or out, for every deck or (unlinked) one. */
export function stepWaveZoomPref(direction: 'in' | 'out', deck?: DeckId): void {
	const from = deck === undefined ? uiPrefs.wave_zoom : deckWaveZoom(uiPrefs, deck);
	setWaveZoom(stepWaveZoom(from, direction === 'in' ? 1 : -1), deck);
}

/** WAVEZOOM-6802c: where the playhead sits once zoomed far out. */
export function setWaveZoomPlayhead(next: WavePlayheadMode): void {
	if (parseWavePlayheadMode(next) === undefined) throw new Error('wave_zoom_playhead must be set, got undefined');
	uiPrefs.wave_zoom_playhead = next;
	_persist();
}

/** WAVEZOOM-6802d: one zoom for every deck (true) or one per deck (false).
 * Unlinking starts every deck at the shared step. */
export function setWaveZoomLinked(next: boolean): void {
	if (typeof next !== 'boolean') throw new Error(`wave_zoom_linked must be a boolean, got ${String(next)}`);
	if (!next && uiPrefs.wave_zoom_linked) uiPrefs.wave_zoom_decks = {};
	uiPrefs.wave_zoom_linked = next;
	_persist();
}

// ------------------------------------------------- ?skin= URL override

/** Shareable skin link: `?skin=mono-dev` applies and persists the skin and
 * sets both waveform prefs to 'auto', so the link shows the skin's declared
 * waveform look (SKIN_WAVE_LOOK) in any browser. An
 * unknown value throws via parseUiSkin rather than silently ignoring it. */
function _applySkinFromUrl(): void {
	// SSR has no window; unit-test window stubs have no location. Neither has a URL to read.
	if (typeof window === 'undefined' || window.location === undefined) return;
	const raw = new URLSearchParams(window.location.search).get('skin');
	if (raw === null) return;
	const skin = parseUiSkin(raw) as UiSkin;
	setUiSkin(skin);
	setWavePalette('auto');
	setWaveformDesign('auto');
}

_applySkinFromUrl();

/** ARTNUM-9649: the deck number drawn over each deck's artwork (default off). */
export function setDeckNumberArt(next: DeckNumberArt): void {
	if (parseDeckNumberArt(next) === undefined) throw new Error(`deck_number_art must be ${DECK_NUMBER_ART_VALUES.join('|')}, got ${String(next)}`);
	uiPrefs.deck_number_art = next;
	_persist();
}
