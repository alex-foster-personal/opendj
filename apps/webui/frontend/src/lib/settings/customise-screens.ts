/**
 * CUSTSCR (issue #9441, epic #9368) and Library (issue #9443): catalogue-backed customise
 * screens, each a second VIEW over `SETTINGS_CATALOG`, never a second copy of any preference.
 * Rows read through `readSettingValue` and write through `applySettingChange`.
 *
 * Registry shape (issue #9439): screens resolve at import via `defineCustomiseScreens`.
 * AB-1 (#9439) registers WAVEFORM_SCREEN first when it lands; Decks, Library and Layout follow until then.
 *
 * Requirements (mini-PRD):
 *   ✔︎ every live row resolves at registry import; an unknown catalog key fails loading this module.
 *     [if] a row names a missing or invalid entry [then ⛔️]
 *   ✔︎ every live row id is a key `applySettingChange` accepts (CUSTSCR-9441).
 *     [if] a row writes a key the Settings path refuses [then ⛔️]
 *   ✔︎ duplicate screen ids are rejected.
 *     [if] two definitions share an id [then ⛔️]
 *   ✔︎ the library density row is the library_density enum (compact|cosy); no row px value is a setting.
 *     [if] a px/number row-height control appears [then ⛔️]
 *   ✔︎ every implemented catalog id is in exactly one screen or in SETTINGS_ONLY with a reason (#10206).
 *     [if] an id is in none, two places, or the bucket names a non-catalog id or has an empty reason [then ⛔️]
 *
 * Do not import `apply.ts` here: node unit tests bundle registry modules without runes.
 */
import { SETTINGS_CATALOG } from './catalog';
import type { SettingDef } from './catalog-types';
import { VARIANT_REGISTRY } from './variant-setting';
import {
	resolveCustomiseSetting as resolveCustomiseSettingCore,
	resolveCustomiseScreen,
	type CustomiseGroup,
	type CustomiseRow,
	type CustomiseScreenDef,
	type ResolvedCustomiseScreen
} from './customise-screen-def';

export type { CustomiseGroup, CustomiseRow, CustomiseScreenDef, ResolvedCustomiseScreen };
export { resolveCustomiseScreen };

/** Resolve a list of screen definitions; a duplicate screen id or any bad row key throws. */
export function defineCustomiseScreens(
	catalog: readonly SettingDef[],
	defs: readonly CustomiseScreenDef[]
): readonly ResolvedCustomiseScreen[] {
	const seen = new Set<string>();
	const out: ResolvedCustomiseScreen[] = [];
	for (const def of defs) {
		if (seen.has(def.id)) throw new Error(`customise screens: duplicate screen id ${def.id}`);
		seen.add(def.id);
		out.push(resolveCustomiseScreen(catalog, def));
	}
	return out;
}

/** Pinned order: spike `customise-screen-inventory` Decks (11 after deck_layout moved to Layout, #10266), section 2.4. */
const DECKS_GROUPS: readonly CustomiseGroup[] = [
	{
		id: 'layout',
		label: 'Layout',
		rows: [
			{ kind: 'setting', id: 'deck_layout_animate' },
			{ kind: 'setting', id: 'deck_layout_duration_ms' }
		]
	},
	{
		id: 'mirror',
		label: 'Mirroring',
		rows: [
			{ kind: 'setting', id: 'deck_left_mirror' },
			{ kind: 'setting', id: 'deck_right_mirror' }
		]
	},
	{
		id: 'chrome',
		label: 'Deck chrome',
		rows: [
			{ kind: 'setting', id: 'deck_hover_link' },
			{ kind: 'setting', id: 'compact_decks' },
			{ kind: 'setting', id: 'lyrics_deck_line' }
		]
	},
	{
		id: 'playback',
		label: 'Sync and AutoPlay',
		rows: [
			{ kind: 'setting', id: 'deck_default_quantize' },
			{ kind: 'setting', id: 'beat_sync_max' },
			{ kind: 'setting', id: 'auto_play_enabled' },
			{ kind: 'setting', id: 'auto_play_enforce_order' },
			{ kind: 'setting', id: 'auto_play_maximize_reach' }
		]
	}
] as const;

/** Decks screen (CUSTSCR-9441). Data only. */
export const DECKS_SCREEN: CustomiseScreenDef = {
	id: 'decks',
	label: 'Decks',
	groups: DECKS_GROUPS
} as const;

/** Library screen (#9443): spike #9438's Library keys, plus the preview-strip rows. Data only. */
export const LIBRARY_SCREEN: CustomiseScreenDef = {
	id: 'library',
	label: 'Library',
	groups: [
		{
			id: 'table',
			label: 'Track table',
			rows: [
				{ kind: 'setting', id: 'library_density' },
				{ kind: 'setting', id: 'key_notation' },
				{ kind: 'setting', id: 'hide_broken_links' }
			]
		},
		{
			id: 'columns',
			label: 'Columns',
			rows: [
				{ kind: 'setting', id: 'lyrics_library_col' },
				{ kind: 'setting', id: 'sections_library_structure_column' }
			]
		},
		{
			id: 'preview',
			label: 'Preview strip',
			rows: [
				{ kind: 'setting', id: 'lyrics_hover_scrub' },
				{ kind: 'setting', id: 'lyrics_load_strategy' },
				{ kind: 'setting', id: 'preview_beat_sync' },
				{ kind: 'setting', id: 'sections_preview_cursor_label' }
			]
		},
		{
			id: 'filters',
			label: 'Filters',
			rows: [
				{ kind: 'setting', id: 'next_only_filter' },
				{ kind: 'setting', id: 'compatible_filter.mode' },
				{ kind: 'setting', id: 'compatible_filter.mashup_relative' },
				{ kind: 'setting', id: 'compatible_filter.key_shift_max' },
				{ kind: 'setting', id: 'compatible_filter.energy_mode' },
				{ kind: 'setting', id: 'remixes_filter' },
				{ kind: 'setting', id: 'vocals_filter' },
				{ kind: 'setting', id: 'available_offline_filter' }
			]
		},
		{
			id: 'sidebar',
			label: 'Sidebar',
			rows: [
				{ kind: 'setting', id: 'library_sidebar_art' },
				{ kind: 'setting', id: 'playlist_tree_view' }
			]
		},
		{
			id: 'usb',
			label: 'USB',
			rows: [
				{ kind: 'setting', id: 'usb_toast_enabled' },
				{ kind: 'setting', id: 'usb_auto_open_panel' }
			]
		}
	]
};

/** Look-variant keys (PLAN.md look variants); listed only when the catalog holds them. */
export const LAYOUT_LOOK_VARIANT_IDS = [
	'layout_variant.two_deck_wide',
	'layout_variant.library_first',
	'layout_variant.pad_grid',
	'layout_variant.vertical_waves',
	'layout_variant.four_across'
] as const;

/** Mode keys that may land after the Layout screen; deck_layout is required today. */
export const LAYOUT_MODE_OPTIONAL_IDS = ['perf_view', 'show_pads'] as const;

function catalogHas(catalog: readonly SettingDef[], id: string): boolean {
	return catalog.some((entry) => entry.id === id);
}

function settingRowsPresent(
	catalog: readonly SettingDef[],
	ids: readonly string[]
): CustomiseRow[] {
	return ids
		.filter((id) => catalogHas(catalog, id))
		.map((id) => ({ kind: 'setting' as const, id }));
}

/**
 * Layout screen (#10266): performance and library presets, look variants when
 * catalogued, and modes (deck_layout moved here from Decks; perf_view and
 * show_pads when catalogued). Guarded like #9442 so missing variant keys do
 * not block the screen. Data only.
 */
export function layoutScreenDef(catalog: readonly SettingDef[]): CustomiseScreenDef {
	const presets: CustomiseRow[] = [
		{ kind: 'setting', id: 'layout_override.performance' },
		{ kind: 'setting', id: 'layout_override.library' }
	];
	const look = settingRowsPresent(catalog, LAYOUT_LOOK_VARIANT_IDS);
	const modes: CustomiseRow[] = [
		{ kind: 'setting', id: 'deck_layout' },
		{ kind: 'setting', id: 'feature_icon_single_click' },
		...settingRowsPresent(catalog, LAYOUT_MODE_OPTIONAL_IDS)
	];
	const groups: CustomiseGroup[] = [
		{ id: 'presets', label: 'Presets', rows: presets },
		...(look.length > 0 ? [{ id: 'look', label: 'Look', rows: look }] : []),
		{ id: 'modes', label: 'Modes', rows: modes }
	];
	return { id: 'layout', label: 'Layout', groups };
}

export const LAYOUT_SCREEN: CustomiseScreenDef = layoutScreenDef(SETTINGS_CATALOG);


/** The side-mixer placement key (#9434, epic #9371); listed only once the catalog has it. */
export const MIXER_PLACEMENT_SETTING_ID = 'mixer_placement';

/**
 * Mixer screen (#9442): the stem dials, the crossfader curve, and `mixer_placement` when
 * `catalog` holds that key (#9434 lands it), so this screen does not wait for it. Data only.
 */
export function mixerScreenDef(catalog: readonly SettingDef[]): CustomiseScreenDef {
	const placement = catalog.some((entry) => entry.id === MIXER_PLACEMENT_SETTING_ID)
		? [{ kind: 'setting', id: MIXER_PLACEMENT_SETTING_ID } as const]
		: [];
	return {
		id: 'mixer',
		label: 'Mixer',
		groups: [
			{
				id: 'channels',
				label: 'Channels',
				rows: [{ kind: 'setting', id: 'mixer_stems' }, ...placement]
			},
			{ id: 'crossfader', label: 'Crossfader', rows: [{ kind: 'setting', id: 'crossfade_curve' }] }
		]
	};
}

export const MIXER_SCREEN: CustomiseScreenDef = mixerScreenDef(SETTINGS_CATALOG);

/** Colours screen (#13880, spike §2.8): theme and reduce motion. `ui_skin` stays in WAVE_QS_GROUPS until #9439. Data only. */
export const COLOURS_SCREEN: CustomiseScreenDef = {
	id: 'colours',
	label: 'Colours',
	groups: [
		{
			id: 'appearance',
			label: 'Appearance',
			rows: [
				{ kind: 'setting', id: 'theme' },
				{ kind: 'setting', id: 'reduce_motion' }
			]
		}
	]
} as const;

const CUSTOMISE_SCREEN_DEFS = [
	DECKS_SCREEN,
	LIBRARY_SCREEN,
	LAYOUT_SCREEN,
	MIXER_SCREEN,
	COLOURS_SCREEN
] as const;

/** A catalog id that no customisation screen shows, with the one-line reason (spike #9438 §3, §4 (a)). */
export interface SettingsOnlyRow {
	id: string;
	reason: string;
}

/**
 * Settings-only bucket (#10206). Every implemented SETTINGS_CATALOG id is in exactly one
 * customise screen OR here, never both; the registry test in customise-screens.test.mjs enforces it.
 * Rows under "Awaiting a screen" move out of this list when that screen's definition lands.
 */
export const SETTINGS_ONLY: ReadonlyArray<SettingsOnlyRow> = [
	// Spike #9438 §3: Settings overlay only (19 rows).
	{ id: 'confirm.dblclick_load_play', reason: 'Confirmation behaviour, not display.' },
	{ id: 'confirm.delete_playlist', reason: 'Confirmation behaviour, not display.' },
	{ id: 'confirm.playlist_drop_mode', reason: 'Remembered prompt policy, not display.' },
	{ id: 'auto_sync', reason: 'Per-target writeback destinations (multi_bool).' },
	{ id: 'sync.run_ratings_writeback', reason: 'Ratings sync dry run action (#12988).' },
	{ id: 'cloudsync.machines', reason: 'Link into the CloudSync policy surface.' },
	{ id: 'cloudsync.playlist_pins', reason: 'Link into CloudSync.' },
	{ id: 'cloudsync.overview', reason: 'Link into CloudSync.' },
	{ id: 'perf_tier', reason: 'Machine capability scaling, read out in the Settings overlay.' },
	{ id: 'app_posture', reason: 'App-wide runtime posture.' },
	{ id: 'gig_helper', reason: 'Operator aid, not display.' },
	{ id: 'audio_engine', reason: 'Audio device selection with restart semantics.' },
	{ id: 'rb.midi_enabled', reason: 'Hardware enablement.' },
	{ id: 'speaker_auto_recover', reason: 'Device recovery policy.' },
	{ id: 'show_dev_ui', reason: 'Developer switch.' },
	{ id: 'hide_todo_settings', reason: 'Developer switch (devOnly).' },
	// Taste variants register through variant-families.ts (catalog.ts, imported above, evaluates it).
	...VARIANT_REGISTRY.map((def) => ({
		id: def.id,
		reason: 'Taste variant (devOnly, epic #10045).'
	})),
	{ id: 'pin_visible_days', reason: 'Feedback-pin age, with no deck or library chrome.' },
	{ id: 'wave_stem_follow', reason: 'Awaiting a screen: stem waveform follow (STEMPERF).' },
	{
		id: 'lyrics_global',
		reason: 'Master switch for every lyric overlay, so it belongs on no single screen.'
	},
	{ id: 'technically_working_animate', reason: 'Cross-surface banner animation.' },
	// Not a boolean/enum row: customise rows refuse other control kinds.
	{
		id: 'library_watcher_folders',
		reason: 'path_lines control; customise rows are boolean or enum only.'
	},
	{
		id: 'library.title_fix',
		reason: 'Link control; customise rows are boolean or enum only.'
	},
	// Added after spike #9438; no placement decided yet.
	{ id: 'sync.ratings_sync_status', reason: 'Ratings sync status action (#12988), not display.' },
	{
		id: 'deck_number_art',
		reason:
			'Added by #9649 after the spike. It draws on the deck header, waveform gutter and mixer column, so it has no single screen yet.'
	},
	{
		id: 'deck_needle_lock',
		reason:
			'Added by #9767 after the spike. It changes how the overview strip responds to clicks, not what a screen displays; no screen placement decided yet.'
	},
	{
		id: 'keyboard_preset',
		reason:
			'Added by #10351 after the spike. Performance keyboard layout preset in Settings only, with no customise screen yet.'
	},
	{
		id: 'auto_analyze_grid',
		reason:
			'Added by #10259. Analyze-on-import lane toggle in the Settings Analysis group; no customise screen yet.'
	},
	{
		id: 'auto_analyze_key',
		reason:
			'Added by #10259. Analyze-on-import lane toggle in the Settings Analysis group; no customise screen yet.'
	},
	{
		id: 'auto_analyze_sections',
		reason:
			'Added by #10259. Analyze-on-import lane toggle in the Settings Analysis group; no customise screen yet.'
	},
	{
		id: 'auto_analyze_vocals',
		reason:
			'Added by #10259. Analyze-on-import lane toggle in the Settings Analysis group; no customise screen yet.'
	},
	{
		id: 'sections_main_style',
		reason:
			'Added by #12787 (segment display). Waveform section styling; awaiting the Waveform screen (#9439), not in the WAVE_QS_GROUPS stand-in.'
	},
	{
		id: 'sections_sticky_title',
		reason:
			'Added by #12787 (segment display). Waveform section styling; awaiting the Waveform screen (#9439), not in the WAVE_QS_GROUPS stand-in.'
	},
	{
		id: 'sections_next_countdown',
		reason:
			'Added by #12787 (segment display). Waveform section styling; awaiting the Waveform screen (#9439), not in the WAVE_QS_GROUPS stand-in.'
	},
	{
		id: 'sections_overview_style',
		reason:
			'Added by #12787 (segment display). Waveform section styling; awaiting the Waveform screen (#9439), not in the WAVE_QS_GROUPS stand-in.'
	},
	{
		id: 'sections_uncertain',
		reason:
			'Added by #12787 (segment display). Waveform section styling; awaiting the Waveform screen (#9439), not in the WAVE_QS_GROUPS stand-in.'
	},
	// Awaiting a screen: move the row onto that screen when its definition lands.
	{
		id: 'wave_eq_reflect',
		reason: 'Awaiting the Waveform screen (#9439, spike §2.3); not in the WAVE_QS_GROUPS stand-in.'
	},
	{
		id: 'show_tempo_lane',
		reason: 'Awaiting the Waveform screen (#9439, spike §2.3); not in the WAVE_QS_GROUPS stand-in.'
	},
	{
		id: 'tempo_lane_scale',
		reason: 'Awaiting the Waveform screen (#9439, spike §2.3); not in the WAVE_QS_GROUPS stand-in.'
	},
	{ id: 'horizontal_wheel_knob', reason: 'Mixer screen exists; placement owned by #9442 (epic #9368, spike §2.5).' },
	{ id: 'auto_gain.enabled', reason: 'Mixer screen exists; placement owned by #9442 (epic #9368, spike §2.5).' },
	{ id: 'auto_gain.target_lufs', reason: 'Mixer screen exists; placement owned by #9442 (epic #9368, spike §2.5).' },
	{ id: 'wheel_sensitivity.mouse', reason: 'Mixer screen exists; placement owned by #9442 (epic #9368, spike §2.5).' },
	{ id: 'wheel_sensitivity.trackpad', reason: 'Mixer screen exists; placement owned by #9442 (epic #9368, spike §2.5).' },
	{
		id: 'wheel_sensitivity.library_mouse',
		reason: 'Library list scroll distance; Settings sliders until Library customise supports numeric rows (LIBM-12633).'
	},
	{
		id: 'wheel_sensitivity.library_trackpad',
		reason: 'Library list scroll distance; Settings sliders until Library customise supports numeric rows (LIBM-12633).'
	},
	{ id: 'drag_sensitivity.base', reason: 'Mixer screen exists; placement owned by #9442 (epic #9368, spike §2.5).' },
	{ id: 'drag_sensitivity.falloff_px', reason: 'Mixer screen exists; placement owned by #9442 (epic #9368, spike §2.5).' }
];


/** Every customise screen, in menu order. Resolved at import: an unknown key fails loading this module. */
export const CUSTOMISE_SCREENS: readonly ResolvedCustomiseScreen[] = defineCustomiseScreens(
	SETTINGS_CATALOG,
	CUSTOMISE_SCREEN_DEFS
);

/** The resolved screen with this id; an unknown id throws. */
export function customiseScreen(id: string): ResolvedCustomiseScreen {
	const found = CUSTOMISE_SCREENS.find((s) => s.id === id);
	if (found === undefined) throw new Error(`customise screens: unknown screen ${id}`);
	return found;
}

/** Unresolved screen definition (for renderers that resolve at runtime, e.g. CustomiseScreen.svelte). */
export function customiseScreenDef(id: string): CustomiseScreenDef {
	const found = CUSTOMISE_SCREEN_DEFS.find((s) => s.id === id);
	if (found === undefined) throw new Error(`customise screens: unknown screen ${id}`);
	return found;
}

/** Every live (`kind: 'setting'`) row id of a screen, in display order. */
export function customiseSettingIds(
	def: CustomiseScreenDef | ResolvedCustomiseScreen
): string[] {
	return def.groups.flatMap((g) =>
		g.rows
			.map((r) => ('row' in r ? r.row : r))
			.filter((row): row is Extract<CustomiseRow, { kind: 'setting' }> => row.kind === 'setting')
			.map((row) => row.id)
	);
}

/** Resolve one row; error messages use the CUSTSCR-9441 `customise screen <screenId>:` prefix. */
export function resolveCustomiseSetting(
	catalog: readonly SettingDef[],
	screenId: string,
	id: string
): SettingDef {
	return resolveCustomiseSettingCore(catalog, id, `customise screen ${screenId}`);
}
