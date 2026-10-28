/**
 * Disk-backed ui-prefs hydration + PUT merge, split out of prefs.svelte.ts
 * so the reactive singleton stays under the 600-line file-size gate.
 */
import type { components } from '../api-types';
import { api, unwrap } from '../api/client';
import {
	DECK_LAYOUT_DURATIONS_MS,
	type DeckLayoutDurationMs,
	type DeckLayoutMode
} from './deck-layout-prefs';
import { announceDeckDefaults } from './deck-default-prefs';
import { makeDiskWriteChain } from './disk-write-chain';
import { LYRICS_BOOLEAN_KEYS, LYRICS_LOAD_STRATEGIES, type LyricsLoadStrategy } from './lyrics-prefs';
import { APP_MODE_PREF_IDS, type AppModePrefs, type DiskAppModePatch } from './app-mode-prefs';
import { APP_POSTURE_PREFS, type AppPosturePref } from './app-posture-prefs';
import { GIG_HELPER_PREFS, type GigHelperPref } from './gig-helper-prefs';
import { applyAllCaps } from '$lib/rb/cache-caps-registry';
import { setResolvedPosture } from './app-posture';
import { PERF_TIER_PREFS, type PerfTierPref } from './perf-tier-prefs';
import { parseAutoSync, parseLevelCalibration } from './prefs-fields';
import { writeBootStampMirror } from './gig-stamp-mirror';
import { hydrateMidiEnabledFromDisk } from '../components/rb/midi/midi-enabled-choice';
import {
	hydrateWheelSensitivityFromDisk,
	type WheelSensitivityDisk
} from './wheel-adjust';
import { hydrateMasterMutedFromDisk } from '../player/master-mute.svelte';
import type { CompatibleFilterPrefs } from './compatible-filter-prefs';
import type { DiskConfirmPatch } from './verified-pref-writes';
import { hydrateTopbarViewFromDisk, type PerfView } from './topbar-view-prefs';
import type { AutoSyncPrefs, LastPlaylistPref, LevelCalibrationPrefs } from './prefs-types';
import { isKeyNotation, KEY_NOTATIONS, type KeyNotation } from './key-notation';

export type UiTheme = 'dark' | 'light';

export type TopbarDiskPrefKey =
	| 'beat_sync_max'
	| 'auto_play_enabled'
	| 'auto_play_enforce_order'
	| 'auto_play_maximize_reach';

export type LibraryDensity = 'compact' | 'cosy';

export type LibraryBrowserDiskPrefKey =
	| 'hide_broken_links'
	| 'library_density'
	| 'key_notation'
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
	show_sections?: boolean;
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
	auto_play_enabled?: boolean;
	auto_play_enforce_order?: boolean;
	auto_play_maximize_reach?: boolean;
	master_muted?: boolean;
	deck_default_quantize?: boolean;
	hide_broken_links?: boolean;
	library_density?: LibraryDensity;
	key_notation?: KeyNotation;
	next_only_filter?: boolean;
	remixes_filter?: boolean;
	vocals_filter?: boolean;
	available_offline_filter?: boolean;
	wheel_sensitivity?: WheelSensitivityDisk;
	midi_enabled?: boolean;
	deck_right_mirror?: boolean;
	playlist_tree_view?: 'tree' | 'column';
	app_mode?: DiskAppModePatch;
	compatible_filter?: CompatibleFilterPrefs;
	library_watcher_folders?: string[];
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
	if (
		wheel_sensitivity !== undefined &&
		wheel_sensitivity.mouse !== undefined &&
		wheel_sensitivity.trackpad !== undefined
	) {
		wire.wheel_sensitivity = {
			mouse: wheel_sensitivity.mouse,
			trackpad: wheel_sensitivity.trackpad
		};
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
	show_sections: boolean;
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
	auto_play_enabled: boolean;
	auto_play_enforce_order: boolean;
	auto_play_maximize_reach: boolean;
	deck_default_quantize: boolean;
	hide_broken_links: boolean;
	library_density: LibraryDensity;
	key_notation: KeyNotation;
	next_only_filter: boolean;
	remixes_filter: boolean;
	vocals_filter: boolean;
	available_offline_filter: boolean;
	library_watcher_folders: string[];
	compatible_filter: CompatibleFilterPrefs;
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
}

/** Pull on-disk confirm + theme prefs once (daemon may have remembered choices). */
export function makePrefsHydrator(deps: PrefsHydrateDeps): () => Promise<void> {
	const { uiPrefs, persist, applyThemeDom, storageKey, defaults } = deps;
	const isConfirmUnsaved = deps.isConfirmUnsaved ?? (() => false);
	return async function hydrateConfirmPrefsFromDisk(): Promise<void> {
		// This PR's confirm and compatible-filter hydration loads with the GET, not
		// at first paint (library bundle budget, PR #4014).
		const late = import('./verified-pref-writes');
		// No blanket catch (review P1, PR #7483): a failed GET, an unreadable
		// body or a contract violation rejects, so the boot path can show it
		// instead of running on stale or default prefs without saying so.
		let contractError: Error | null = null;
		const body = (await unwrap(api.GET('/api/v1/ui-prefs'))) as DiskPrefsPatch;
		const { hydrateConfirmFromDisk, hydrateCompatibleFilter } = await late;
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
		if (typeof body.show_sections === 'boolean') {
			uiPrefs.show_sections = body.show_sections;
		}
		if (typeof body.jog_radial_waveform === 'boolean') {
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
		for (const key of [
			'beat_sync_max',
			'auto_play_enabled',
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
		if (body.library_density === 'compact' || body.library_density === 'cosy') {
			uiPrefs.library_density = body.library_density;
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
		hydrateWheelSensitivityFromDisk(body);
		hydrateMidiEnabledFromDisk(body);
		if (typeof body.master_muted === 'boolean') {
			hydrateMasterMutedFromDisk(body.master_muted);
		}
		if (Array.isArray(body.library_watcher_folders)) {
			uiPrefs.library_watcher_folders = body.library_watcher_folders.filter(
				(p): p is string => typeof p === 'string'
			);
		}
		hydrateCompatibleFilter(uiPrefs, body);
		persist();
		if (contractError !== null) throw contractError;
	};
}

