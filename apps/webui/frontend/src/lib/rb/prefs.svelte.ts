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
import { makeJogRadialWaveformSetters } from './jog-radial-prefs';
import { makeLevelCalibrationSetters } from './level-calibration-prefs';
import { LYRICS_PREF_DEFAULTS, makeLyricsPrefSetters, validateLyricsPrefFields, type LyricsLoadStrategy } from './lyrics-prefs';
import { createDiskPrefsSync, makePrefsHydrator } from './prefs-hydrate';
import { parseAutoSync, parseLastPlaylist, parseLevelCalibration, parseSpotifyLibrary } from './prefs-fields';
import type { AutoSyncPrefs, LastPlaylistPref, LevelCalibrationPrefs, SpotifyLibraryPref } from './prefs-types';
import { makeSpotifyLibrarySetters } from './spotify-library-prefs';
import { validateActiveScheme } from './theme-tokens';
export { DECK_LAYOUT_DURATIONS_MS, type DeckLayoutDurationMs, type DeckLayoutMode } from './deck-layout-prefs';
export { type LyricsLoadStrategy } from './lyrics-prefs';
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

/** App + /performance chrome theme. Default dark. */
export type UiTheme = 'dark' | 'light';

/** When word timings are fetched into RAM for library surfaces. */
export type LyricsLoadStrategy = 'in-view' | 'hover' | 'off';

/** Preferred vendor writeback targets (preference only; CLI writeback today). */
export type AutoSyncDestination = 'rekordbox' | 'djay' | 'open_dj';

export interface RbUiPrefs {
	/** Width, in CSS pixels, of the resizable playlist tree (220 through 520). */
	playlist_tree_width: number;
	/** FR-1: hide missing-file tracks and playlists with available_count == 0. Default OFF. */
	hide_broken_links: boolean;
	/** Track-table row height: compact = current tight rows; cosy = taller. */
	library_density: LibraryDensity;
	/** When true, every transport relocate (including master) uses BAR
	 * phase-preserving sync so bar 1 stays aligned across synced decks. */
	beat_sync_max: boolean;
/** Library list: keep only tracks appropriate as next (Camelot + BPM
	 * window vs master / loaded reference). Toggle with Tab. */
	next_only_filter: boolean;
	/** Library list: keep only remixes (title-marker heuristic, backend
	 * is_remix; the lyric repair signal joins it after the library run). */
	remixes_filter: boolean;
	/** Library list: keep only tracks with real word-level lyrics spanning
	 * more than 5 derived lines (pane-contract VOCALS_FILTER_MIN_LINES). */
	vocals_filter: boolean;
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
	/** MASTER lyric-overlay switch (top-left LYR icon): gates the waveform
	 * lanes, deck lyric lines and scrub-hover words everywhere at once. The
	 * per-surface prefs below survive underneath and return when this comes
	 * back on. The library column and admin pages are data surfaces, not
	 * overlays - they keep their own switches. */
	lyrics_global: boolean;
	/** Lyrics column in the library table (hover tip carries the text). */
	lyrics_library_col: boolean;
	/** Lyric words above the vocal bars while hover-scrubbing a strip. */
	lyrics_hover_scrub: boolean;
	/** How word payloads reach RAM: 'in-view' loads rows as they reveal,
	 * 'hover' waits for a 500ms-debounced hover (default: cheapest),
	 * 'off' never loads in the library (deck/stage still load). */
	lyrics_load_strategy: LyricsLoadStrategy;
	/** Word lanes over the main deck waveforms. Default ON per spec. */
	lyrics_waveform_overlay: boolean;
	/** One-line lyric readout in the deck panel when space allows. */
	lyrics_deck_line: boolean;
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
	level_calibration: LevelCalibrationPrefs;
	/** Master switch (TopBar LYR) for every lyric overlay. */
	lyrics_global: boolean;
	/** Lyrics column in the library table (hover tip carries the text). */
	lyrics_library_col: boolean;
	/** Word readout + click-to-audition while hover-scrubbing a preview strip. */
	lyrics_hover_scrub: boolean;
	/** When the library pulls word timings into memory. */
	lyrics_load_strategy: LyricsLoadStrategy;
	/** The word-lane gate: word lanes over the main deck waveforms (D13.4). */
	lyrics_waveform_overlay: boolean;
	/** Current lyric line under the deck hot cues. */
	lyrics_deck_line: boolean;
}

const DEFAULTS: RbUiPrefs = {
	playlist_tree_width: PLAYLIST_TREE_WIDTH_DEFAULT,
	hide_broken_links: false,
	library_density: 'compact',
	beat_sync_max: true,
	next_only_filter: false,
	remixes_filter: false,
	vocals_filter: false,
	next_panel_collapsed: false,
	recommended_panel_collapsed: false,
	auto_play_enabled: true,
	auto_play_enforce_order: false,
	auto_play_maximize_reach: true,
	lyrics_global: true,
	lyrics_library_col: true,
	lyrics_hover_scrub: true,
	lyrics_load_strategy: 'hover',
	lyrics_waveform_overlay: true,
	lyrics_deck_line: true,
	theme: 'dark',
	hide_todo_settings: false,
	auto_sync: { rekordbox: false, djay: false, open_dj: false },
	usb_toast_enabled: true,
	usb_toast_ms: 5000,
	usb_auto_open_panel: false,
	technically_working_animate: true,
	jog_radial_waveform: false,
	show_agent_pins: true,
	confirm: {},
	last_playlist: null,
	spotify_library: { pinned_ids: [], recent_ids: [] },
	deck_layout: 'more',
	deck_layout_animate: true,
	deck_layout_duration_ms: 200,
	level_calibration: { red_dbfs: null, red_enabled: false, ceiling_dbfs: null, ceiling_enabled: false },
	...LYRICS_PREF_DEFAULTS
};

// ----------------------------------------------------------- _helpers

function _storage(): Storage | null {
	return typeof window === 'undefined' ? null : window.localStorage;
}

function _applyThemeDom(theme: UiTheme): void {
	if (typeof document === 'undefined') return;
	document.documentElement.dataset.theme = theme;
	document.documentElement.style.colorScheme = theme;
	validateActiveScheme(theme);
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
	if (parsed.next_only_filter !== undefined && typeof parsed.next_only_filter !== 'boolean') {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (next_only_filter is not a boolean) - ` +
				'clear the localStorage key to recover'
		);
	}
	for (const key of [
		'remixes_filter',
		'vocals_filter',
		'lyrics_global',
		'lyrics_library_col',
		'lyrics_hover_scrub',
		'lyrics_waveform_overlay',
		'lyrics_deck_line'
	] as const) {
		if (parsed[key] !== undefined && typeof parsed[key] !== 'boolean') {
			throw new Error(
				`${STORAGE_KEY}: malformed prefs blob (${key} is not a boolean) - ` +
					'clear the key or fix the value'
			);
		}
	}
	if (
		parsed.lyrics_load_strategy !== undefined &&
		!['in-view', 'hover', 'off'].includes(parsed.lyrics_load_strategy as string)
	) {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (lyrics_load_strategy must be ` +
				"'in-view' | 'hover' | 'off') - clear the key or fix the value"
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
	const {
		deck_layout: deckLayout,
		deck_layout_animate: deckLayoutAnimate,
		deck_layout_duration_ms: deckLayoutDurationMs
	} = validateDeckLayoutFields(parsed, STORAGE_KEY);
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
		next_only_filter: parsed.next_only_filter ?? DEFAULTS.next_only_filter,
		remixes_filter: parsed.remixes_filter ?? DEFAULTS.remixes_filter,
		vocals_filter: parsed.vocals_filter ?? DEFAULTS.vocals_filter,
		next_panel_collapsed: parsed.next_panel_collapsed ?? DEFAULTS.next_panel_collapsed,
		recommended_panel_collapsed:
			parsed.recommended_panel_collapsed ?? DEFAULTS.recommended_panel_collapsed,
		auto_play_enabled: parsed.auto_play_enabled ?? DEFAULTS.auto_play_enabled,
		auto_play_enforce_order:
			parsed.auto_play_enforce_order ?? DEFAULTS.auto_play_enforce_order,
		auto_play_maximize_reach:
			parsed.auto_play_maximize_reach ?? DEFAULTS.auto_play_maximize_reach,
		lyrics_global: parsed.lyrics_global ?? DEFAULTS.lyrics_global,
		lyrics_library_col: parsed.lyrics_library_col ?? DEFAULTS.lyrics_library_col,
		lyrics_hover_scrub: parsed.lyrics_hover_scrub ?? DEFAULTS.lyrics_hover_scrub,
		lyrics_load_strategy: parsed.lyrics_load_strategy ?? DEFAULTS.lyrics_load_strategy,
		lyrics_waveform_overlay: parsed.lyrics_waveform_overlay ?? DEFAULTS.lyrics_waveform_overlay,
		lyrics_deck_line: parsed.lyrics_deck_line ?? DEFAULTS.lyrics_deck_line,
		theme: theme ?? DEFAULTS.theme,
		hide_todo_settings: parsed.hide_todo_settings ?? DEFAULTS.hide_todo_settings,
		auto_sync: autoSync,
		usb_toast_enabled: parsed.usb_toast_enabled ?? DEFAULTS.usb_toast_enabled,
		usb_toast_ms: parsed.usb_toast_ms ?? DEFAULTS.usb_toast_ms,
		usb_auto_open_panel: parsed.usb_auto_open_panel ?? DEFAULTS.usb_auto_open_panel,
		technically_working_animate:
			parsed.technically_working_animate ?? DEFAULTS.technically_working_animate,
		jog_radial_waveform: parsed.jog_radial_waveform ?? DEFAULTS.jog_radial_waveform,
		show_agent_pins: parsed.show_agent_pins ?? DEFAULTS.show_agent_pins,
		confirm: { ...(confirm as RbUiPrefs['confirm']) },
		last_playlist: lastPlaylist,
		spotify_library: parseSpotifyLibrary(parsed.spotify_library, STORAGE_KEY),
		deck_layout: deckLayout ?? DEFAULTS.deck_layout,
		deck_layout_animate: deckLayoutAnimate ?? DEFAULTS.deck_layout_animate,
		deck_layout_duration_ms: deckLayoutDurationMs ?? DEFAULTS.deck_layout_duration_ms,
		level_calibration: parseLevelCalibration(parsed.level_calibration, STORAGE_KEY, DEFAULTS.level_calibration),
		...LYRICS_PREF_DEFAULTS,
		...validateLyricsPrefFields(parsed, STORAGE_KEY)
	};
}

function _persist(): void {
	_storage()?.setItem(STORAGE_KEY, JSON.stringify($state.snapshot(uiPrefs)));
}

/** One shared write queue (issue #1578) - see disk-write-chain.ts. */
const _syncDiskPrefs = createDiskPrefsSync();

// -------------------------------------------------------- public API

/** Reactive prefs singleton. Read anywhere; write ONLY via the setters
 * below so every change persists. */
export const uiPrefs = $state<RbUiPrefs>(_load());

_applyThemeDom(uiPrefs.theme);

export function setHideBrokenLinks(next: boolean): void {
	uiPrefs.hide_broken_links = next;
	_persist();
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
	uiPrefs.library_density = next;
	_persist();
}

export function setBeatSyncMax(next: boolean): void {
	uiPrefs.beat_sync_max = next;
	_persist();
}

export function setAutoPlayEnabled(next: boolean): void {
	uiPrefs.auto_play_enabled = next;
	_persist();
}

export function setAutoPlayEnforceOrder(next: boolean): void {
	uiPrefs.auto_play_enforce_order = next;
	_persist();
}

export function setAutoPlayMaximizeReach(next: boolean): void {
	uiPrefs.auto_play_maximize_reach = next;
	_persist();
}

export function setNextOnlyFilter(next: boolean): void {
	uiPrefs.next_only_filter = next;
	_persist();
}

/** Collapse one suggestion panel while retaining the other panel's state. */
export function setLibraryPanelCollapsed(panel: LibraryPanel, collapsed: boolean): void {
	if (panel === 'next') uiPrefs.next_panel_collapsed = collapsed;
	else uiPrefs.recommended_panel_collapsed = collapsed;
	_persist();
}

export function toggleNextOnlyFilter(): void {
	setNextOnlyFilter(!uiPrefs.next_only_filter);
}

export function setRemixesFilter(next: boolean): void {
	uiPrefs.remixes_filter = next;
	_persist();
}

export function setVocalsFilter(next: boolean): void {
	uiPrefs.vocals_filter = next;
	_persist();
}

export function setLyricsGlobal(next: boolean): void {
	uiPrefs.lyrics_global = next;
	_persist();
}

export function toggleLyricsGlobal(): void {
	setLyricsGlobal(!uiPrefs.lyrics_global);
}

export function setLyricsLibraryCol(next: boolean): void {
	uiPrefs.lyrics_library_col = next;
	_persist();
}

export function setLyricsHoverScrub(next: boolean): void {
	uiPrefs.lyrics_hover_scrub = next;
	_persist();
}

export function setLyricsLoadStrategy(next: LyricsLoadStrategy): void {
	uiPrefs.lyrics_load_strategy = next;
	_persist();
}

export function setLyricsWaveformOverlay(next: boolean): void {
	uiPrefs.lyrics_waveform_overlay = next;
	_persist();
}

export function setLyricsDeckLine(next: boolean): void {
	uiPrefs.lyrics_deck_line = next;
	_persist();
}

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

/** The MORE/LESS deck-layout setters (pin 862cd3), built against this
 * module's own uiPrefs/_persist/_syncDiskPrefs (deck-layout-prefs.ts). */
export const {
	setDeckLayoutMode,
	toggleDeckLayoutMode,
	setDeckLayoutAnimate,
	setDeckLayoutDurationMs
} = makeDeckLayoutSetters(uiPrefs, _persist, (patch) => void _syncDiskPrefs(patch));

/** The six karaoke lyric setters (PR-4 section C), built against this module's
 * own uiPrefs/_persist/_syncDiskPrefs (lyrics-prefs.ts). */
export const {
	setLyricsGlobal,
	toggleLyricsGlobal,
	setLyricsLibraryCol,
	setLyricsHoverScrub,
	setLyricsLoadStrategy,
	setLyricsWaveformOverlay,
	setLyricsDeckLine
} = makeLyricsPrefSetters(uiPrefs, _persist, (patch) => void _syncDiskPrefs(patch));

export function setAutoSyncDestination(dest: AutoSyncDestination, next: boolean): void {
	uiPrefs.auto_sync[dest] = next;
	_persist();
	void _syncDiskPrefs({ auto_sync: { ...uiPrefs.auto_sync } });
}

export function setAutoSync(next: AutoSyncPrefs): void {
	uiPrefs.auto_sync = { ...next };
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
