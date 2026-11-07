/**
 * CUSTSCR (issue #9441, epic #9368) and Library (issue #9443): catalogue-backed customise
 * screens, each a second VIEW over `SETTINGS_CATALOG`, never a second copy of any preference.
 * Rows read through `readSettingValue` and write through `applySettingChange`.
 *
 * Registry shape (issue #9439): screens resolve at import via `defineCustomiseScreens`.
 * AB-1 (#9439) registers WAVEFORM_SCREEN first when it lands; Decks and Library follow until then.
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
 *
 * Do not import `apply.ts` here: node unit tests bundle registry modules without runes.
 */
import { SETTINGS_CATALOG } from './catalog';
import type { SettingDef } from './catalog-types';
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

/** Pinned order: spike `customise-screen-inventory` Decks (12), section 2.4. */
const DECKS_GROUPS: readonly CustomiseGroup[] = [
	{
		id: 'layout',
		label: 'Layout',
		rows: [
			{ kind: 'setting', id: 'deck_layout' },
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
		{ id: 'columns', label: 'Columns', rows: [{ kind: 'setting', id: 'lyrics_library_col' }] },
		{
			id: 'preview',
			label: 'Preview strip',
			rows: [
				{ kind: 'setting', id: 'lyrics_hover_scrub' },
				{ kind: 'setting', id: 'lyrics_load_strategy' },
				{ kind: 'setting', id: 'preview_beat_sync' }
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
		}
	]
};

const CUSTOMISE_SCREEN_DEFS = [DECKS_SCREEN, LIBRARY_SCREEN] as const;

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
