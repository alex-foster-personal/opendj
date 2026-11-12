/**
 * Disk-backed ui-prefs hydration + PUT merge, split out of prefs.svelte.ts
 * so the reactive singleton stays under the 600-line file-size gate.
 */
import type { components } from '../api-types';
import { api, unwrap } from '../api/rb-http';
import {
	DECK_LAYOUT_DURATIONS_MS,
	type DeckLayoutDurationMs,
	type DeckLayoutMode
} from './deck-layout-prefs';
import { announceDeckDefaults } from './deck-default-prefs';
import {
	beatJumpSizesFromPrefs,
	restoreBeatJumpSizes,
	type BeatJumpSizesByDeck
} from './beat-jump-size.svelte';
import { makeKeyFilterReadGuard } from './library-key-filter';
import { makeDiskWriteChain } from './disk-write-chain';
import {
	LYRICS_BOOLEAN_KEYS,
	LYRICS_LOAD_STRATEGIES,
	type LyricsLoadStrategy
} from './lyrics-prefs';
import { APP_MODE_PREF_IDS, type AppModePrefs, type DiskAppModePatch } from './app-mode-prefs';
import { APP_POSTURE_PREFS, type AppPosturePref } from './app-posture-prefs';
import { GIG_HELPER_PREFS, type GigHelperPref } from './gig-helper-prefs';
import { applyAllCaps } from '$lib/rb/cache-caps-registry';
import { setResolvedPosture } from './app-posture';
import { PERF_TIER_PREFS, type PerfTierPref } from './perf-tier-prefs';
import { parseSpeakerAutoRecover, type SpeakerAutoRecoverPref } from './speaker-link-prefs';
import { parseWaveHeight, type WaveHeight } from '$lib/rb/wave-height';
import { parseTempoLaneScale, type TempoLaneScale } from './tempo-lane-prefs';
import { hydrateCueMarkerPrefs } from './cue-marker-prefs';
import { parseWaveGridLines, type WaveGridLines } from './wave-grid-lines';
import {
	parseSectionsMainStyle,
	parseSectionsOverviewStyle,
	parseSectionsUncertain,
	type SectionsMainStyle,
	type SectionsOverviewStyle,
	type SectionsUncertain
} from './section-overlay-prefs';
import { parseAutoSync, parseLevelCalibration } from './prefs-fields';
import { writeBootStampMirror } from './gig-stamp-mirror';
import { hydrateMidiEnabledFromDisk } from '../components/rb/midi/midi-enabled-choice';
import { hydrateSidebarArtFromDisk } from './library-sidebar-art.svelte';
import { hydrateWheelSensitivityFromDisk, type WheelSensitivityDisk } from './wheel-adjust';
import { hydrateLibraryListWheelFromDisk } from './library-wheel-scroll';
import { hydrateMasterMutedFromDisk } from '../player/master-mute.svelte';
import type { CompatibleFilterPrefs } from './compatible-filter-prefs';
import type { RelatedCriteriaPrefs } from './related-criteria-prefs';
import { validateHiddenColumns, type HideableColumn } from './library-columns';
import type { DiskConfirmPatch } from './verified-pref-writes';
import { hydrateTopbarViewFromDisk, type PerfView } from './topbar-view-prefs';
import type {
	AutoSyncPrefs,
	LastPlaylistPref,
	LevelCalibrationPrefs,
	SpotifyLibraryPref
} from './prefs-types';
import { isKeyNotation, KEY_NOTATIONS, type KeyNotation } from './key-notation';

export type UiTheme = 'dark' | 'light';

export type TopbarDiskPrefKey =
	| 'beat_sync_max'
	| 'deck_autocue'
	| 'auto_play_enabled'
	| 'auto_play_enforce_order'
	| 'auto_play_maximize_reach';

export type LibraryDensity = 'compact' | 'cosy';

export type LibraryBrowserDiskPrefKey =
	| 'hide_broken_links'
	| 'library_density'
	| 'sort_within_sections'
	| 'collapsed_sections'
	| 'key_notation'
	| 'library_hidden_columns'
	| 'next_only_filter'
	| 'remixes_filter'
	| 'vocals_filter';

export function setLibraryBrowserDiskPref<K extends LibraryBrowserDiskPrefKey>(
	uiPrefs: Pick<PrefsHydrateTarget, K>,
	persist: () => void,
	sync: (patch: Partial<DiskPrefsPatch>) => void,
	key: K,
	next: PrefsHydrateTarget[K]
): void {
	uiPrefs[key] = next;
	persist();
	void sync({ [key]: next });
}

export function setTopbarDiskPref(
	uiPrefs: Pick<PrefsHydrateTarget, TopbarDiskPrefKey>,
	persist: () => void,
	sync: (patch: Partial<DiskPrefsPatch>) => void,
	key: TopbarDiskPrefKey,
	next: boolean
): void {
	uiPrefs[key] = next;
	persist();
	void sync({ [key]: next });
}

/** Live confirm map: missing keys mean "ask"; no null members. */
export type LiveConfirmPrefs = {
	delete_playlist?: boolean;
	playlist_drop_mode?: 'add' | 'move';
	dblclick_load_play?: boolean;
};

export type DiskPrefsPatch = {
	confirm?: DiskConfirmPatch;
	theme?: UiTheme;
	hide_todo_settings?: boolean;
	auto_sync?: AutoSyncPrefs;
	technically_working_animate?: boolean;
	show_agent_pins?: boolean;
	show_stems?: boolean;
	/** A11Y-11811: Appearance > Reduce motion; OR'd with the OS media query. */
	reduce_motion?: boolean;
	deck_hover_link?: boolean;
	compact_decks?: boolean;
	show_sections?: boolean;
	sections_main_style?: string;
	sections_sticky_title?: boolean;
	sections_next_countdown?: boolean;
	sections_overview_style?: string;
	sections_library_structure_column?: boolean;
	sections_preview_cursor_label?: boolean;
	sections_uncertain?: string;
	auto_analyze_grid?: boolean;
	auto_analyze_key?: boolean;
	auto_analyze_sections?: boolean;
	auto_analyze_vocals?: boolean;
	speaker_auto_recover?: SpeakerAutoRecoverPref;
	keyboard?: { preset?: string; overrides?: Record<string, string[]> };
	show_tempo_lane?: boolean;
	tempo_lane_scale?: TempoLaneScale;
	show_vocal_lane?: boolean;
	wave_height?: WaveHeight;
	wave_grid_lines?: WaveGridLines;
	show_hot_cue_markers?: boolean;
	show_memory_cue_markers?: boolean;
	show_loop_markers?: boolean;
	jog_radial_waveform?: boolean;
	deck_layout?: DeckLayoutMode;
	deck_layout_animate?: boolean;
	deck_layout_duration_ms?: DeckLayoutDurationMs;
	perf_view?: PerfView;
	show_pads?: boolean;
	auto_play_two_deck?: boolean;
	level_calibration?: LevelCalibrationPrefs;
	lyrics_global?: boolean;
	lyrics_library_col?: boolean;
	lyrics_hover_scrub?: boolean;
	lyrics_load_strategy?: LyricsLoadStrategy;
	lyrics_waveform_overlay?: boolean;
	lyrics_deck_line?: boolean;
	perf_tier?: PerfTierPref;
	app_posture?: AppPosturePref;
	gig_helper?: GigHelperPref;
	beat_sync_max?: boolean;
	deck_autocue?: boolean;
	auto_play_enabled?: boolean;
	auto_play_enforce_order?: boolean;
	auto_play_maximize_reach?: boolean;
	master_muted?: boolean;
	deck_default_quantize?: boolean;
	deck_needle_lock?: boolean;
	beat_jump_sizes?: BeatJumpSizesByDeck;
	hide_broken_links?: boolean;
	library_density?: LibraryDensity;
	sort_within_sections?: boolean;
	collapsed_sections?: string[];
	key_notation?: KeyNotation;
	library_hidden_columns?: HideableColumn[];
	next_only_filter?: boolean;
	remixes_filter?: boolean;
	vocals_filter?: boolean;
	available_offline_filter?: boolean;
	wheel_sensitivity?: WheelSensitivityDisk;
	midi_enabled?: boolean;
	spotify_shrink_main_for_player?: boolean;
	library_sidebar_art?: boolean;
	deck_right_mirror?: boolean;
	playlist_tree_view?: 'tree' | 'column';
	app_mode?: DiskAppModePatch;
	compatible_filter?: CompatibleFilterPrefs;
	library_related_criteria?: RelatedCriteriaPrefs;
	library_watcher_folders?: string[];
	midi_setup_dismissed_ports?: string[];
	library_key_filter?: string[];
};

function _diskPrefsToWirePatch(patch: DiskPrefsPatch): components['schemas']['UiPrefsPatch'] {
	const { app_mode, wheel_sensitivity, ...rest } = patch;
	const wire = { ...rest } as components['schemas']['UiPrefsPatch'];
	if (app_mode !== undefined) {
		wire.app_mode = {
			...(app_mode.id !== undefined ? { id: app_mode.id } : {}),
			...(app_mode.last_gig_at !== undefined ? { last_gig_at: app_mode.last_gig_at } : {})
		} as components['schemas']['AppModeOut'];
	}
	if (wheel_sensitivity !== undefined) {
		const ws: Partial<components['schemas']['WheelSensitivityOut']> = {};
		for (const key of ['mouse', 'trackpad', 'library_mouse', 'library_trackpad'] as const) {
			const value = wheel_sensitivity[key];
			if (value !== undefined) ws[key] = value;
		}
		if (Object.keys(ws).length > 0) {
			wire.wheel_sensitivity = ws as components['schemas']['WheelSensitivityOut'];
		}
	}
	return wire;
}

async function _putDiskPrefs(patch: DiskPrefsPatch): Promise<void> {
	try {
		await api.PUT('/api/v1/ui-prefs', { body: _diskPrefsToWirePatch(patch) });
	} catch {
		/* localStorage remains authoritative if daemon is down */
	}
}

/** Like `_putDiskPrefs` but fails loud: a transport error or a non-2xx
 * (the client middleware throws ApiError) rejects, so a caller that reports
 * "saved" can await this first (LIBM-129 watcher folders, PR #4014). */
export async function putDiskPrefsVerified(patch: DiskPrefsPatch): Promise<void> {
	await api.PUT('/api/v1/ui-prefs', { body: _diskPrefsToWirePatch(patch) });
}

export function createDiskPrefsSync() {
	return makeDiskWriteChain<DiskPrefsPatch>(_putDiskPrefs);
}

/** Shared ui-prefs PUT queue (issue #1578); one instance for prefs + Gig stamp. */
export const syncDiskPrefs = createDiskPrefsSync();

export interface PrefsHydrateTarget {
	confirm: LiveConfirmPrefs & Record<string, unknown>;
	theme: UiTheme;
	hide_todo_settings: boolean;
	auto_sync: AutoSyncPrefs;
	technically_working_animate: boolean;
	show_agent_pins: boolean;
	show_stems: boolean;
	reduce_motion: boolean;
	deck_hover_link: boolean;
	compact_decks: boolean;
	show_sections: boolean;
	sections_main_style: SectionsMainStyle;
	sections_sticky_title: boolean;
	sections_next_countdown: boolean;
	sections_overview_style: SectionsOverviewStyle;
	sections_library_structure_column: boolean;
	sections_preview_cursor_label: boolean;
	sections_uncertain: SectionsUncertain;
	auto_analyze_grid: boolean;
	auto_analyze_key: boolean;
	auto_analyze_sections: boolean;
	auto_analyze_vocals: boolean;
	speaker_auto_recover: SpeakerAutoRecoverPref;
	keyboard: { preset: import('$lib/keyboard/presets/index').KeymapPresetId; overrides: Record<string, string[]> };
	show_tempo_lane: boolean;
	tempo_lane_scale: TempoLaneScale;
	show_vocal_lane: boolean;
	wave_height: WaveHeight;
	wave_grid_lines: WaveGridLines;
	show_hot_cue_markers: boolean;
	show_memory_cue_markers: boolean;
	show_loop_markers: boolean;
	jog_radial_waveform: boolean;
	deck_layout: DeckLayoutMode;
	deck_layout_animate: boolean;
	deck_layout_duration_ms: DeckLayoutDurationMs;
	perf_view: PerfView;
	show_pads: boolean;
	auto_play_two_deck: boolean;
	deck_right_mirror: boolean;
	playlist_tree_view: 'tree' | 'column';
	level_calibration: LevelCalibrationPrefs;
	last_playlist: LastPlaylistPref | null;
	lyrics_global: boolean;
	lyrics_library_col: boolean;
	lyrics_hover_scrub: boolean;
	lyrics_load_strategy: LyricsLoadStrategy;
	lyrics_waveform_overlay: boolean;
	lyrics_deck_line: boolean;
	perf_tier: PerfTierPref;
	app_posture: AppPosturePref;
	gig_helper: GigHelperPref;
	app_mode: AppModePrefs['app_mode'];
	beat_sync_max: boolean;
	deck_autocue: boolean;
	auto_play_enabled: boolean;
	auto_play_enforce_order: boolean;
	auto_play_maximize_reach: boolean;
	deck_default_quantize: boolean;
	deck_needle_lock: boolean;
	beat_jump_sizes?: BeatJumpSizesByDeck;
	hide_broken_links: boolean;
	library_density: LibraryDensity;
	sort_within_sections: boolean;
	collapsed_sections: string[];
	key_notation: KeyNotation;
	library_hidden_columns: HideableColumn[];
	next_only_filter: boolean;
	remixes_filter: boolean;
	vocals_filter: boolean;
	available_offline_filter: boolean;
	library_watcher_folders: string[];
	midi_setup_dismissed_ports: string[];
	library_key_filter: string[];
	compatible_filter: CompatibleFilterPrefs;
	library_related_criteria: RelatedCriteriaPrefs;
	spotify_library: SpotifyLibraryPref;
}

/** Union disk + local dismissed MIDI ports so a race cannot drop a dismissal (#10076). */
export function hydrateMidiSetupDismissedPorts(
	uiPrefs: PrefsHydrateTarget,
	body: DiskPrefsPatch
): boolean {
	const disk = Array.isArray(body.midi_setup_dismissed_ports)
		? body.midi_setup_dismissed_ports.filter((p): p is string => typeof p === 'string')
		: [];
	const local = uiPrefs.midi_setup_dismissed_ports.filter((p): p is string => typeof p === 'string');
	const seen = new Set<string>();
	const union: string[] = [];
	for (const name of [...disk, ...local]) {
		if (seen.has(name)) continue;
		seen.add(name);
		union.push(name);
	}
	uiPrefs.midi_setup_dismissed_ports = union;
	const diskSet = new Set(disk);
	return union.length !== disk.length || union.some((n) => !diskSet.has(n));
}

/** The five boolean lyric prefs hydrate in one loop rather than five ifs. */
function _hydrateLyrics(uiPrefs: PrefsHydrateTarget, body: DiskPrefsPatch): void {
	for (const key of LYRICS_BOOLEAN_KEYS) {
		const value = body[key];
		if (typeof value === 'boolean') uiPrefs[key] = value;
	}
	const strategy = body.lyrics_load_strategy;
	if (strategy !== undefined && (LYRICS_LOAD_STRATEGIES as readonly string[]).includes(strategy)) {
		uiPrefs.lyrics_load_strategy = strategy;
	}
}

export interface PrefsHydrateDeps {
	uiPrefs: PrefsHydrateTarget;
	persist: () => void;
	applyThemeDom: (theme: UiTheme) => void;
	storageKey: string;
	defaults: Pick<PrefsHydrateTarget, 'auto_sync' | 'level_calibration'>;
	/** Confirm keys with a local write disk has not acknowledged yet. */
	isConfirmUnsaved?: (key: string) => boolean;
	/** Keys a boot migration just rewrote locally: the disk copy is older, so skip it.
	 * Required with `onSupersededDiskValue`: a missing pair would silently take a
	 * stale disk value or drop the one to reconcile (Sol P1, PR #6829). */
	isDiskValueSuperseded: (key: string) => boolean;
	/** Told what disk held for a superseded key (undefined when absent), so the
	 * owner can rewrite a stale disk copy or see that it already agrees. A key
	 * claimed when the GET was sent counts as superseded even if the claim
	 * ended while it was in flight, so the owner must ignore a value the DJ
	 * has since picked over. */
	onSupersededDiskValue: (key: string, diskValue: unknown) => void;
	isLibraryKeyFilterUnsaved?: () => boolean;
	/** A counter the owner bumps on every local write of `key`; a key written while the GET was in
	 * flight keeps its local value (#10717). */
	localWriteRev?: (key: string) => number;
}

/** Issue #7599: re-read only `library_key_filter` from disk, so a filter set
 * elsewhere (an agent's POST /api/v1/ui-prefs/library-key-filter, another
 * window) reaches the open table. A local write disk has not acknowledged yet
 * wins, as in the boot hydration. */
export function makeLibraryKeyFilterRehydrator(deps: {
	uiPrefs: { library_key_filter: string[] };
	persist: () => void;
	isUnsaved: () => boolean;
	/** Counts local key-filter edits; a read that spans one is discarded. */
	writeRev: () => number;
	/** Called when a read was skipped for an unsaved local write. */
	onDeferred: () => void;
}): () => Promise<void> {
	const guard = makeKeyFilterReadGuard(deps.writeRev);
	return async function hydrateLibraryKeyFilterFromDisk(): Promise<void> {
		const token = guard.begin();
		const late = import('./verified-pref-writes');
		const body = (await unwrap(api.GET('/api/v1/ui-prefs'))) as DiskPrefsPatch;
		const { hydrateLibraryKeyFilter } = await late;
		if (!guard.isCurrent(token)) return;
		// The server always sends the field (UiPrefsOut default []), so an absent
		// one is contract drift: fail rather than report a clean re-read (Sol P1, #7743).
		if (body.library_key_filter === undefined) {
			throw new Error('GET /api/v1/ui-prefs: library_key_filter is missing from the response');
		}
		if (deps.isUnsaved()) {
			// A local write is in flight: re-read once it settles, so an agent's
			// later value is not lost to this skip (Sol P1, #7743).
			deps.onDeferred();
			return;
		}
		hydrateLibraryKeyFilter(deps.uiPrefs, body, deps.isUnsaved);
		deps.persist();
	};
}

/** DECKCTL-6235: re-read the AU preference on a `ui_prefs` change event, so an
 * agent's PUT lights AU and governs the next load without a page reload.
 * An event that lands while a read is in flight owes one more read after it,
 * since the read in flight may already hold the older value. A failed read is
 * retried (AUTO_CUE_READ_ATTEMPTS in all); when every attempt fails, `report`
 * says so out loud, because AU may now be stale. Resolves true only when the
 * last read landed, so a caller that must confirm AU (the `deck_autocue`
 * command, Codex P2 on PR #6235) can fail instead of trusting a stale value. */
export const AUTO_CUE_READ_ATTEMPTS = 3;

export function makeDiskBoolPrefRefresher<K extends 'deck_autocue' | 'reduce_motion'>(
	key: K,
	staleMessage: string,
	uiPrefs: Pick<PrefsHydrateTarget, K>,
	persist: () => void,
	report: (message: string, cause: unknown) => void,
	retryDelayMs = 1000
): () => Promise<boolean> {
	let inFlight: Promise<boolean> | null = null;
	let owed = false;
	async function readOnce(): Promise<void> {
		const body = (await unwrap(api.GET('/api/v1/ui-prefs'))) as DiskPrefsPatch;
		// The response always carries a boolean; anything else is a failed read
		// (retried, then reported), never a confirmation (Sol P1, PR #6235).
		const value = body[key];
		if (typeof value !== 'boolean') {
			throw new Error(`ui-prefs answered without a boolean ${key}: ${JSON.stringify(value)}`);
		}
		if (value !== uiPrefs[key]) {
			uiPrefs[key] = value;
			persist();
		}
	}
	async function readWithRetry(): Promise<boolean> {
		for (let attempt = 1; ; attempt += 1) {
			try {
				await readOnce();
				return true;
			} catch (error) {
				if (attempt >= AUTO_CUE_READ_ATTEMPTS) {
					report(staleMessage, error);
					return false;
				}
				await new Promise((resolve) => setTimeout(resolve, retryDelayMs));
			}
		}
	}
	return () => {
		if (inFlight !== null) {
			owed = true;
			return inFlight;
		}
		inFlight = (async () => {
			try {
				let read: boolean;
				do {
					owed = false;
					read = await readWithRetry();
				} while (owed);
				return read;
			} finally {
				inFlight = null;
			}
		})();
		return inFlight;
	};
}

export function makeAutoCueRefresher(
	uiPrefs: Pick<PrefsHydrateTarget, 'deck_autocue'>,
	persist: () => void,
	report: (message: string, cause: unknown) => void,
	retryDelayMs = 1000
): () => Promise<boolean> {
	return makeDiskBoolPrefRefresher(
		'deck_autocue',
		'Auto cue (AU) changed elsewhere, but this page could not re-read it; AU may be stale until reload',
		uiPrefs,
		persist,
		report,
		retryDelayMs
	);
}

/** A11Y-11811: re-read `reduce_motion` on a `ui_prefs` change event (agent PUT). */
export function makeReduceMotionRefresher(
	uiPrefs: Pick<PrefsHydrateTarget, 'reduce_motion'>,
	persist: () => void,
	report: (message: string, cause: unknown) => void,
	retryDelayMs = 1000
): () => Promise<boolean> {
	return makeDiskBoolPrefRefresher(
		'reduce_motion',
		'Reduce motion changed elsewhere, but this page could not re-read it; motion may be stale until reload',
		uiPrefs,
		persist,
		report,
		retryDelayMs
	);
}

/** Pull on-disk confirm + theme prefs once (daemon may have remembered choices). */

export function makePrefsHydrator(deps: PrefsHydrateDeps): () => Promise<void> {
	const { uiPrefs, persist, applyThemeDom, storageKey, defaults } = deps;
	const isConfirmUnsaved = deps.isConfirmUnsaved ?? (() => false);
	const { isDiskValueSuperseded, onSupersededDiskValue } = deps;
	if (typeof isDiskValueSuperseded !== 'function' || typeof onSupersededDiskValue !== 'function') {
		throw new TypeError(
			'makePrefsHydrator: isDiskValueSuperseded and onSupersededDiskValue are both required'
		);
	}
	const isLibraryKeyFilterUnsaved = deps.isLibraryKeyFilterUnsaved ?? (() => false);
	const localWriteRev = deps.localWriteRev ?? (() => 0);
	return async function hydrateConfirmPrefsFromDisk(): Promise<void> {
		// This PR's confirm and compatible-filter hydration loads with the GET, not
		// at first paint (library bundle budget, PR #4014).
		const late = import('./verified-pref-writes');
		// No blanket catch (review P1, PR #7483): a failed GET, an unreadable
		// body or a contract violation rejects, so the boot path can show it
		// instead of running on stale or default prefs without saying so.
		let contractError: Error | null = null;
		// A claim held when the GET left still covers its body: the disk may have
		// answered before the claim's own write landed (Codex P1, PR #6829).
		const radialClaimedAtRequest = isDiskValueSuperseded('jog_radial_waveform');
		const jogRadialAtRequest = uiPrefs.jog_radial_waveform;
		const autoPlayClaimedAtRequest = isDiskValueSuperseded('auto_play_enabled');
		const autoPlayRevAtRequest = localWriteRev('auto_play_enabled');
		const body = (await unwrap(api.GET('/api/v1/ui-prefs'))) as DiskPrefsPatch;
		const {
			hydrateConfirmFromDisk,
			hydrateCompatibleFilter,
			hydrateRelatedCriteria,
			hydrateLibraryKeyFilter
		} = await late;
		if (body.confirm !== undefined && typeof body.confirm === 'object') {
			hydrateConfirmFromDisk(uiPrefs, body.confirm, isConfirmUnsaved);
		}
		if (body.theme === 'dark' || body.theme === 'light') {
			uiPrefs.theme = body.theme;
			applyThemeDom(body.theme);
		}
		if (typeof body.hide_todo_settings === 'boolean') {
			uiPrefs.hide_todo_settings = body.hide_todo_settings;
		}
		if (body.auto_sync !== undefined && typeof body.auto_sync === 'object') {
			uiPrefs.auto_sync = parseAutoSync(body.auto_sync, storageKey, defaults.auto_sync);
		}
		if (typeof body.technically_working_animate === 'boolean') {
			uiPrefs.technically_working_animate = body.technically_working_animate;
		}
		if (typeof body.show_agent_pins === 'boolean') {
			uiPrefs.show_agent_pins = body.show_agent_pins;
		}
		if (typeof body.show_stems === 'boolean') {
			uiPrefs.show_stems = body.show_stems;
		}
		if (typeof body.reduce_motion === 'boolean') {
			uiPrefs.reduce_motion = body.reduce_motion;
		}
		if (typeof body.deck_hover_link === 'boolean') {
			uiPrefs.deck_hover_link = body.deck_hover_link;
		}
		if (typeof body.compact_decks === 'boolean') {
			uiPrefs.compact_decks = body.compact_decks;
		}
		if (typeof body.show_sections === 'boolean') {
			uiPrefs.show_sections = body.show_sections;
		}
		const mainStyle = parseSectionsMainStyle(body.sections_main_style);
		if (mainStyle !== undefined) uiPrefs.sections_main_style = mainStyle;
		if (typeof body.sections_sticky_title === 'boolean') {
			uiPrefs.sections_sticky_title = body.sections_sticky_title;
		}
		if (typeof body.sections_next_countdown === 'boolean') {
			uiPrefs.sections_next_countdown = body.sections_next_countdown;
		}
		const overviewStyle = parseSectionsOverviewStyle(body.sections_overview_style);
		if (overviewStyle !== undefined) uiPrefs.sections_overview_style = overviewStyle;
		if (typeof body.sections_library_structure_column === 'boolean') {
			uiPrefs.sections_library_structure_column = body.sections_library_structure_column;
		}
		if (typeof body.sections_preview_cursor_label === 'boolean') {
			uiPrefs.sections_preview_cursor_label = body.sections_preview_cursor_label;
		}
		const uncertain = parseSectionsUncertain(body.sections_uncertain);
		if (uncertain !== undefined) uiPrefs.sections_uncertain = uncertain;
		if (typeof body.auto_analyze_grid === 'boolean') {
			uiPrefs.auto_analyze_grid = body.auto_analyze_grid;
		}
		if (typeof body.auto_analyze_key === 'boolean') {
			uiPrefs.auto_analyze_key = body.auto_analyze_key;
		}
		if (typeof body.auto_analyze_sections === 'boolean') {
			uiPrefs.auto_analyze_sections = body.auto_analyze_sections;
		}
		if (typeof body.auto_analyze_vocals === 'boolean') {
			uiPrefs.auto_analyze_vocals = body.auto_analyze_vocals;
		}
		const speakerAutoRecover = parseSpeakerAutoRecover(body.speaker_auto_recover);
		if (speakerAutoRecover !== undefined) {
			uiPrefs.speaker_auto_recover = speakerAutoRecover;
		}
		if (body.keyboard !== undefined) {
			const { parseKeyboardPrefs } = await import('$lib/keyboard/keyboard-prefs');
			const keyboard = parseKeyboardPrefs(body.keyboard);
			if (keyboard !== undefined) {
				uiPrefs.keyboard = keyboard;
			}
		}
		if (typeof body.show_tempo_lane === 'boolean') {
			uiPrefs.show_tempo_lane = body.show_tempo_lane;
		}
		if (typeof body.show_vocal_lane === 'boolean') {
			uiPrefs.show_vocal_lane = body.show_vocal_lane;
		}
		const tempoLaneScale = parseTempoLaneScale(body.tempo_lane_scale);
		if (tempoLaneScale !== undefined) {
			uiPrefs.tempo_lane_scale = tempoLaneScale;
		}
		hydrateCueMarkerPrefs(body, uiPrefs);
		const waveHeight = parseWaveHeight(body.wave_height);
		if (waveHeight !== undefined) {
			uiPrefs.wave_height = waveHeight;
		}
		const waveGridLines = parseWaveGridLines(body.wave_grid_lines);
		if (waveGridLines !== undefined) {
			uiPrefs.wave_grid_lines = waveGridLines;
		}
		const radialChangedDuringGet = uiPrefs.jog_radial_waveform !== jogRadialAtRequest;
		if (radialClaimedAtRequest || isDiskValueSuperseded('jog_radial_waveform')) {
			// A pick made while this GET was in flight stands: do not run the
			// migration settle path from a body read before that pick landed.
			if (!radialChangedDuringGet) {
				onSupersededDiskValue('jog_radial_waveform', body.jog_radial_waveform);
			}
		} else if (typeof body.jog_radial_waveform === 'boolean' && !radialChangedDuringGet) {
			uiPrefs.jog_radial_waveform = body.jog_radial_waveform;
		}
		if (body.deck_layout === 'more' || body.deck_layout === 'less') {
			uiPrefs.deck_layout = body.deck_layout;
		}
		if (typeof body.deck_layout_animate === 'boolean') {
			uiPrefs.deck_layout_animate = body.deck_layout_animate;
		}
		if (
			typeof body.deck_layout_duration_ms === 'number' &&
			(DECK_LAYOUT_DURATIONS_MS as readonly number[]).includes(body.deck_layout_duration_ms)
		) {
			uiPrefs.deck_layout_duration_ms = body.deck_layout_duration_ms;
		}
		if (typeof body.deck_right_mirror === 'boolean') {
			uiPrefs.deck_right_mirror = body.deck_right_mirror;
		}
		if (body.playlist_tree_view === 'tree' || body.playlist_tree_view === 'column') {
			uiPrefs.playlist_tree_view = body.playlist_tree_view;
		}
		if (body.level_calibration !== undefined && typeof body.level_calibration === 'object') {
			uiPrefs.level_calibration = parseLevelCalibration(
				body.level_calibration,
				storageKey,
				defaults.level_calibration
			);
		}
		_hydrateLyrics(uiPrefs, body);
		if (
			body.perf_tier !== undefined &&
			(PERF_TIER_PREFS as readonly string[]).includes(body.perf_tier)
		) {
			uiPrefs.perf_tier = body.perf_tier;
		}
		if (
			body.app_posture !== undefined &&
			(APP_POSTURE_PREFS as readonly string[]).includes(body.app_posture)
		) {
			uiPrefs.app_posture = body.app_posture;
			setResolvedPosture(body.app_posture);
			applyAllCaps();
		}
		if (
			body.gig_helper !== undefined &&
			(GIG_HELPER_PREFS as readonly string[]).includes(body.gig_helper)
		) {
			uiPrefs.gig_helper = body.gig_helper;
		}
		if (body.app_mode !== undefined) {
			const diskAppMode = body.app_mode;
			if (typeof diskAppMode === 'string') {
				if ((APP_MODE_PREF_IDS as readonly string[]).includes(diskAppMode)) {
					uiPrefs.app_mode = diskAppMode;
				}
			} else if (typeof diskAppMode === 'object' && diskAppMode !== null) {
				const modeId = diskAppMode.id;
				if (
					typeof modeId === 'string' &&
					(APP_MODE_PREF_IDS as readonly string[]).includes(modeId)
				) {
					uiPrefs.app_mode = modeId;
				}
				const lastGigAt = diskAppMode.last_gig_at;
				if (typeof lastGigAt === 'string') {
					writeBootStampMirror(lastGigAt);
				}
			}
		}
		if (typeof body.spotify_shrink_main_for_player === 'boolean') {
			uiPrefs.spotify_library = {
				...uiPrefs.spotify_library,
				shrink_main_for_player: body.spotify_shrink_main_for_player
			};
		}
		// A local AutoPlay write disk has not acknowledged, or one made while this
		// GET was in flight (Trackify's on-mount override), outranks the body
		// (#10717, review on PR #5907).
		const autoPlayWrittenDuringGet = localWriteRev('auto_play_enabled') !== autoPlayRevAtRequest;
		if (autoPlayClaimedAtRequest || autoPlayWrittenDuringGet || isDiskValueSuperseded('auto_play_enabled')) {
			if (!autoPlayWrittenDuringGet) onSupersededDiskValue('auto_play_enabled', body.auto_play_enabled);
		} else if (typeof body.auto_play_enabled === 'boolean') {
			uiPrefs.auto_play_enabled = body.auto_play_enabled;
		}
		for (const key of [
			'beat_sync_max',
			'deck_autocue',
			'auto_play_enforce_order',
			'auto_play_maximize_reach',
			'hide_broken_links',
			'next_only_filter',
			'remixes_filter',
			'vocals_filter',
			'available_offline_filter'
		] as const) {
			const value = body[key];
			if (typeof value === 'boolean') uiPrefs[key] = value;
		}
		// A malformed top-bar view pref keeps this browser's values and fails the
		// hydration once the rest has applied, like key_notation below.
		try {
			hydrateTopbarViewFromDisk(uiPrefs, body);
		} catch (exc) {
			contractError ??= new Error(
				`Top-bar view prefs on disk were refused, kept this browser's values: ${
					exc instanceof Error ? exc.message : String(exc)
				}`
			);
		}
		// Announced only on a real change: a fresh browser's decks were seeded
		// from the local default before this GET returned (SETSTUB-02).
		if (
			typeof body.deck_default_quantize === 'boolean' &&
			body.deck_default_quantize !== uiPrefs.deck_default_quantize
		) {
			uiPrefs.deck_default_quantize = body.deck_default_quantize;
			announceDeckDefaults(uiPrefs);
		}
		if (
			typeof body.deck_needle_lock === 'boolean' &&
			body.deck_needle_lock !== uiPrefs.deck_needle_lock
		) {
			uiPrefs.deck_needle_lock = body.deck_needle_lock;
			announceDeckDefaults(uiPrefs);
		}
		// DECKUX-10470: the disk copy wins over this browser's, as for other prefs.
		if (body.beat_jump_sizes !== undefined) {
			const sizes = beatJumpSizesFromPrefs(body.beat_jump_sizes);
			if (JSON.stringify(sizes) !== JSON.stringify(uiPrefs.beat_jump_sizes)) {
				uiPrefs.beat_jump_sizes = sizes;
				restoreBeatJumpSizes(sizes);
			}
		}
		if (body.library_density === 'compact' || body.library_density === 'cosy') {
			uiPrefs.library_density = body.library_density;
		}
		if (typeof body.sort_within_sections === 'boolean') {
			uiPrefs.sort_within_sections = body.sort_within_sections;
		}
		if (Array.isArray(body.collapsed_sections)) {
			uiPrefs.collapsed_sections = body.collapsed_sections;
		}
		// The contract requires a valid key_notation (review P1, PR #7483).
		// A missing or invalid one fails hydration: it is recorded here and
		// thrown once the rest of this response has hydrated, so one bad
		// field does not strand the others and the caller still sees it.
		if (isKeyNotation(body.key_notation)) {
			uiPrefs.key_notation = body.key_notation;
		} else {
			contractError = new Error(
				`GET /api/v1/ui-prefs: key_notation must be ${KEY_NOTATIONS.join('|')}, got ${JSON.stringify(body.key_notation)}`
			);
		}
		// Same contract rule as key_notation: the server always sends this list.
		try {
			uiPrefs.library_hidden_columns = validateHiddenColumns(
				body.library_hidden_columns,
				'GET /api/v1/ui-prefs'
			);
		} catch (error) {
			contractError ??= error instanceof Error ? error : new Error(String(error));
		}
		hydrateWheelSensitivityFromDisk(body);
		hydrateLibraryListWheelFromDisk(body.wheel_sensitivity);
		hydrateMidiEnabledFromDisk(body);
		hydrateSidebarArtFromDisk(body);
		if (typeof body.master_muted === 'boolean') {
			hydrateMasterMutedFromDisk(body.master_muted);
		}
		if (Array.isArray(body.library_watcher_folders)) {
			uiPrefs.library_watcher_folders = body.library_watcher_folders.filter(
				(p): p is string => typeof p === 'string'
			);
		}
		if (hydrateMidiSetupDismissedPorts(uiPrefs, body)) {
			void syncDiskPrefs({ midi_setup_dismissed_ports: uiPrefs.midi_setup_dismissed_ports });
		}
		hydrateCompatibleFilter(uiPrefs, body);
		hydrateRelatedCriteria(uiPrefs, body);
		try {
			hydrateLibraryKeyFilter(uiPrefs, body, isLibraryKeyFilterUnsaved);
		} catch (exc) {
			contractError ??= exc instanceof Error ? exc : new Error(String(exc));
		}
		persist();
		if (contractError !== null) throw contractError;
	};
}
