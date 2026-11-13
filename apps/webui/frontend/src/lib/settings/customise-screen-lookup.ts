/**
 * CUSTLOOK-13878 (epic #9368): reverse lookup from a Settings catalog id to its
 * customise home, for the Settings overlay "Edit in Customise" link and for agents
 * explaining placement. Pure; reads the registry in customise-screens.ts.
 *
 * Requirements (mini-PRD):
 *   ✔︎ a screen row id returns its screen id and group id.
 *     [if] layout_override.performance is not layout / presets [then ⛔️]
 *   ✔︎ a SETTINGS_ONLY id returns its reason.
 *     [if] audio_engine does not return its SETTINGS_ONLY reason [then ⛔️]
 *   ✔︎ an id not in SETTINGS_CATALOG throws.
 *     [if] an unknown id returns [then ⛔️]
 *
 * The waveform quick-settings groups stand in for the Waveform screen (#9439) as screen
 * `waveform`, matching the CUSTSCR-10206 coverage test.
 */
import { SETTINGS_CATALOG } from './catalog';
import { CUSTOMISE_SCREENS, SETTINGS_ONLY } from './customise-screens';
import { WAVE_QS_GROUPS } from './waveform-quick-settings';

export type CustomiseHome = { screen: string; group: string } | { settingsOnly: string };

interface GroupLike {
	id: string;
	rows: readonly ({ kind: string; id: string } | { row: { kind: string; id: string } })[];
}

function findIn(screen: string, groups: readonly GroupLike[], id: string): CustomiseHome | null {
	for (const g of groups) {
		for (const r of g.rows) {
			const row = 'row' in r ? r.row : r;
			if (row.kind === 'setting' && row.id === id) return { screen, group: g.id };
		}
	}
	return null;
}

/** Where a catalog id lives: `{ screen, group }`, `{ settingsOnly: reason }`, or throws. */
export function customiseHomeFor(id: string): CustomiseHome {
	if (!SETTINGS_CATALOG.some((entry) => entry.id === id)) {
		throw new Error(`customise lookup: no Settings catalog entry ${id}`);
	}
	for (const s of CUSTOMISE_SCREENS) {
		const hit = findIn(s.id, s.groups, id);
		if (hit) return hit;
	}
	const wave = findIn('waveform', WAVE_QS_GROUPS, id);
	if (wave) return wave;
	const only = SETTINGS_ONLY.find((r) => r.id === id);
	if (only) return { settingsOnly: only.reason };
	throw new Error(`customise lookup: catalog id ${id} has no customise home`);
}
