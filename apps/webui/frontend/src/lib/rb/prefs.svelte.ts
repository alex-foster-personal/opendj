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
	validateDeckLayoutFields,
	makeDeckLayoutSetters,
	DECK_LAYOUT_DURATIONS_MS,
	type DeckLayoutDurationMs,
	type DeckLayoutMode
} from './deck-layout-prefs';
import {
	makePlaylistTreeViewSetters,
	validatePlaylistTreeViewField,
	type PlaylistTreeViewMode
} from './playlist-tree-view-prefs';
import type { PreviewBeatSync } from '$lib/player/preview-beat-sync';
import {
	parseWaveformDesign,
	WAVEFORM_DESIGN_DEFAULT,
	type WaveformDesign
} from '$lib/rb/waveform-design';
import {
	parseWavePalette,
	WAVE_PALETTE_DEFAULT,
	type WavePaletteChoice
} from '$lib/rb/wave-palette';
import { makeJogRadialWaveformSetters } from './jog-radial-prefs';
import {
	LIBRARY_FILTER_PREF_DEFAULTS,
	makeLibraryFilterSetters,
	validateLibraryFilterPrefFields
} from './library-filter-prefs';
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
	makePrefsHydrator,
	setLibraryBrowserDiskPref,
	setTopbarDiskPref,
	syncDiskPrefs
} from './prefs-hydrate';
import { parseAutoSync, parseLastPlaylist, parseLevelCalibration, parseSpotifyLibrary } from './prefs-fields';
import type { AutoSyncPrefs, LastPlaylistPref, LevelCalibrationPrefs, SpotifyLibraryPref } from './prefs-types';
import { makeSpotifyLibrarySetters } from './spotify-library-prefs';
import { validateActiveScheme } from './theme-tokens';
import { tryOfferGigHelperPromptOnPostureChange } from './gig-helper-prompt.svelte';
export { DECK_LAYOUT_DURATIONS_MS, type DeckLayoutDurationMs, type DeckLayoutMode } from './deck-layout-prefs';
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
		DevUiPrefs {
	/** Width, in CSS pixels, of the resizable playlist tree (220 through 520). */
	playlist_tree_width: number;
	/** FR-1: hide missing-file tracks and playlists with available_count == 0. Default OFF. */
	hide_broken_links: boolean;
	/** Track-table row height: compact = current tight rows; cosy = taller. */
	library_density: LibraryDensity;
	/** When true, every transport relocate (including master) uses BAR
	 * phase-preserving sync so bar 1 stays aligned across synced decks. */
	beat_sync_max: boolean;
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
	/** Library list: keep only tracks with real word-level lyrics spanning
	 * more than 5 derived lines (pane-contract VOCALS_FILTER_MIN_LINES). */
	vocals_filter: boolean;
	/** Library list: keep only tracks with local audio present. */
	available_offline_filter: boolean;
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
	/** DECKUX-19: per-stem mini-waveforms under deck wavestack rows. Default off. */
	show_stems: boolean;
	/** DECKUX-20: tri-band, mono envelope, or line outline for waveforms. */
	waveform_design: WaveformDesign;
	/** Issue #4219: waveform band colors. 'rekordbox' (default) is CDJ 3Band:
	 * dark blue low, amber mid, white high; 'legacy' is the pre-#4219 orange
	 * low, blue mid, near-white high. Applied as html[data-wave-palette]. */
	wave_palette: WavePaletteChoice;
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
	/** Playlist sidebar: tree list vs column browser (issue #3983). */
	playlist_tree_view: PlaylistTreeViewMode;
	level_calibration: LevelCalibrationPrefs;
	/** Crossfader curve name; unbuilt curves stay disabled in the UI. */
	crossfade_curve: CrossfadeCurve;
	/** Horizontal mouse wheel adjusts filter or color knob on selected channels. */
	horizontal_wheel_knob: HorizontalWheelKnob;
}

const DEFAULTS: RbUiPrefs = {
	playlist_tree_width: PLAYLIST_TREE_WIDTH_DEFAULT,
	hide_broken_links: false,
	library_density: 'compact',
	beat_sync_max: true,
	preview_beat_sync: 'tempo',
	next_panel_collapsed: false,
	recommended_panel_collapsed: false,
	auto_play_enabled: true,
	auto_play_enforce_order: false,
	auto_play_maximize_reach: true,
	theme: 'dark',
	hide_todo_settings: false,
	auto_sync: { rekordbox: false, djay: false, open_dj: false },
	usb_toast_enabled: true,
	usb_toast_ms: 5000,
	usb_auto_open_panel: false,
	technically_working_animate: true,
	jog_radial_waveform: false,
	show_agent_pins: true,
	show_stems: false,
	waveform_design: WAVEFORM_DESIGN_DEFAULT,
	wave_palette: WAVE_PALETTE_DEFAULT,
	confirm: {},
	last_playlist: null,
	spotify_library: { pinned_ids: [], recent_ids: [] },
	deck_layout: 'more',
	deck_layout_animate: true,
	deck_layout_duration_ms: 200,
	deck_right_mirror: false,
	playlist_tree_view: 'tree',
	level_calibration: { red_dbfs: null, red_enabled: false, ceiling_dbfs: null, ceiling_enabled: false },
	crossfade_curve: 'magic',
	horizontal_wheel_knob: 'filter',
	...LYRICS_PREF_DEFAULTS,
	...LIBRARY_FILTER_PREF_DEFAULTS,
	...PERF_TIER_PREF_DEFAULTS,
	...APP_POSTURE_PREF_DEFAULTS,
	...GIG_HELPER_PREF_DEFAULTS,
	...APP_MODE_PREF_DEFAULTS,
	...DEV_UI_PREF_DEFAULTS
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
	if (choice === 'legacy') document.documentElement.dataset.wavePalette = 'legacy';
	else delete document.documentElement.dataset.wavePalette;
}

function _load(): RbUiPrefs {
	const storage = _storage();
	if (storage === null) return { ...DEFAULTS };
	const raw = storage.getItem(STORAGE_KEY);
	if (raw === null) return { ...DEFAULTS }; // first run - the one real default
	const parsed = JSON.parse(raw) as Partial<RbUiPrefs>; // malformed JSON throws - intended
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
	if (parsed.beat_sync_max !== undefined && typeof parsed.beat_sync_max !== 'boolean') {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (beat_sync_max is not a boolean) - ` +
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
	if (
		parsed.preview_beat_sync !== undefined &&
		parsed.preview_beat_sync !== 'off' &&
		parsed.preview_beat_sync !== 'tempo'
	) {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (preview_beat_sync is not off or tempo) - ` +
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
	if (parsed.show_stems !== undefined && typeof parsed.show_stems !== 'boolean') {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (show_stems is not a boolean) - ` +
				'clear the localStorage key to recover'
		);
	}
	const waveformDesign = parseWaveformDesign(parsed.waveform_design);
	const wavePalette = parseWavePalette(parsed.wave_palette);
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
	const playlistTreeView = validatePlaylistTreeViewField(parsed.playlist_tree_view, STORAGE_KEY);
	const lastPlaylist = parseLastPlaylist(parsed.last_playlist, STORAGE_KEY);
	const autoSync = parseAutoSync(parsed.auto_sync, STORAGE_KEY, DEFAULTS.auto_sync);
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
	return {
		playlist_tree_width: treeWidth ?? DEFAULTS.playlist_tree_width,
		hide_broken_links: parsed.hide_broken_links,
		library_density: density ?? DEFAULTS.library_density,
		beat_sync_max: parsed.beat_sync_max ?? DEFAULTS.beat_sync_max,
		next_panel_collapsed: parsed.next_panel_collapsed ?? DEFAULTS.next_panel_collapsed,
		recommended_panel_collapsed:
			parsed.recommended_panel_collapsed ?? DEFAULTS.recommended_panel_collapsed,
		auto_play_enabled: parsed.auto_play_enabled ?? DEFAULTS.auto_play_enabled,
		auto_play_enforce_order:
			parsed.auto_play_enforce_order ?? DEFAULTS.auto_play_enforce_order,
		auto_play_maximize_reach:
			parsed.auto_play_maximize_reach ?? DEFAULTS.auto_play_maximize_reach,
		theme: theme ?? DEFAULTS.theme,
		hide_todo_settings: parsed.hide_todo_settings ?? DEFAULTS.hide_todo_settings,
		auto_sync: autoSync,
		usb_toast_enabled: parsed.usb_toast_enabled ?? DEFAULTS.usb_toast_enabled,
		usb_toast_ms: parsed.usb_toast_ms ?? DEFAULTS.usb_toast_ms,
		usb_auto_open_panel: parsed.usb_auto_open_panel ?? DEFAULTS.usb_auto_open_panel,
		technically_working_animate:
			parsed.technically_working_animate ?? DEFAULTS.technically_working_animate,
		preview_beat_sync: parsed.preview_beat_sync ?? DEFAULTS.preview_beat_sync,
		jog_radial_waveform: parsed.jog_radial_waveform ?? DEFAULTS.jog_radial_waveform,
		show_agent_pins: parsed.show_agent_pins ?? DEFAULTS.show_agent_pins,
		show_stems: parsed.show_stems ?? DEFAULTS.show_stems,
		waveform_design: waveformDesign ?? DEFAULTS.waveform_design,
		wave_palette: wavePalette ?? DEFAULTS.wave_palette,
		confirm: { ...(confirm as RbUiPrefs['confirm']) },
		last_playlist: lastPlaylist,
		spotify_library: parseSpotifyLibrary(parsed.spotify_library, STORAGE_KEY),
		deck_layout: deckLayout ?? DEFAULTS.deck_layout,
		deck_layout_animate: deckLayoutAnimate ?? DEFAULTS.deck_layout_animate,
		deck_layout_duration_ms: deckLayoutDurationMs ?? DEFAULTS.deck_layout_duration_ms,
		deck_right_mirror: deckRightMirror ?? DEFAULTS.deck_right_mirror,
		playlist_tree_view: playlistTreeView ?? DEFAULTS.playlist_tree_view,
		level_calibration: parseLevelCalibration(parsed.level_calibration, STORAGE_KEY, DEFAULTS.level_calibration),
		crossfade_curve: crossfadeCurve ?? DEFAULTS.crossfade_curve,
		horizontal_wheel_knob: horizontalWheelKnob ?? DEFAULTS.horizontal_wheel_knob,
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
		...mergeDevUiPrefsFromParsed(parsed, STORAGE_KEY)
	};
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

_applyThemeDom(uiPrefs.theme);
_applyWavePaletteDom(uiPrefs.wave_palette);

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

export const { toggleSpotifyPinned, rememberSpotifyRecent } = makeSpotifyLibrarySetters(uiPrefs, _persist);
export function setLibraryDensity(next: LibraryDensity): void {
	setLibraryBrowserDiskPref(uiPrefs, _persist, _syncDiskPrefs, 'library_density', next);
}

export function setBeatSyncMax(next: boolean): void {
	setTopbarDiskPref(uiPrefs, _persist, _syncDiskPrefs, 'beat_sync_max', next);
}

/** Local-only: the engine holds no preview voice, so this never leaves the page. */
export function setPreviewBeatSync(next: PreviewBeatSync): void {
	uiPrefs.preview_beat_sync = next;
	_persist();
}

export function setAutoPlayEnabled(next: boolean): void {
	setTopbarDiskPref(uiPrefs, _persist, _syncDiskPrefs, 'auto_play_enabled', next);
}

export function setAutoPlayEnforceOrder(next: boolean): void {
	setTopbarDiskPref(uiPrefs, _persist, _syncDiskPrefs, 'auto_play_enforce_order', next);
}

export function setAutoPlayMaximizeReach(next: boolean): void {
	setTopbarDiskPref(uiPrefs, _persist, _syncDiskPrefs, 'auto_play_maximize_reach', next);
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
	toggleNextOnlyFilter,
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

export const { setJogRadialWaveform } = makeJogRadialWaveformSetters(uiPrefs, _persist, (patch) =>
	void _syncDiskPrefs(patch)
);

/** Persist the agent-pin layer through both local state and its HTTP twin. */
export function setShowAgentPins(next: boolean): void {
	uiPrefs.show_agent_pins = next;
	_persist();
	void _syncDiskPrefs({ show_agent_pins: next });
}

export function setShowStems(next: boolean): void {
	uiPrefs.show_stems = next;
	_persist();
	void _syncDiskPrefs({ show_stems: next });
}

export function setWaveformDesign(next: WaveformDesign): void {
	parseWaveformDesign(next);
	uiPrefs.waveform_design = next;
	_persist();
}

export function setWavePalette(next: WavePaletteChoice): void {
	if (parseWavePalette(next) === undefined) {
		throw new Error('wave_palette must be rekordbox|legacy, got undefined');
	}
	uiPrefs.wave_palette = next;
	_applyWavePaletteDom(next);
	_persist();
}

export function setCrossfadeCurve(next: CrossfadeCurve): void {
	if (next !== 'magic') {
		throw new Error(`crossfade curve ${next} is not implemented - see PARITY-TODO`);
	}
	uiPrefs.crossfade_curve = next;
	_persist();
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
	setDeckRightMirror
} = makeDeckLayoutSetters(uiPrefs, _persist, (patch) => void _syncDiskPrefs(patch));

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

/** Persist a confirm skip / remembered choice. Pass `undefined` to clear. */
export function setConfirmPref<K extends keyof RbUiPrefs['confirm']>(
	key: K,
	value: RbUiPrefs['confirm'][K] | undefined
): void {
	if (value === undefined) {
		delete uiPrefs.confirm[key];
	} else {
		uiPrefs.confirm[key] = value;
	}
	_persist();
	void _syncDiskPrefs({ confirm: uiPrefs.confirm });
}

/** Pull on-disk confirm + theme prefs once (daemon may have remembered choices). */
export const hydrateConfirmPrefsFromDisk = makePrefsHydrator({
	uiPrefs,
	persist: _persist,
	applyThemeDom: _applyThemeDom,
	storageKey: STORAGE_KEY,
	defaults: DEFAULTS
});
