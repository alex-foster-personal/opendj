/**
 * customise-screen-def (issue #9439): shared types and resolvers for
 * data-driven customise screens. Kept free of value imports from
 * `waveform-quick-settings.ts` or `customise-screens.ts` so those modules can
 * import groups and call `resolveCustomiseSetting` without a TDZ cycle.
 *
 * Requirements (mini-PRD):
 *   ✔︎ every live row names exactly one Settings catalog entry.
 *     [if] a row id is missing, unimplemented, or not boolean, enum or number [then ⛔️]
 *   ✔︎ a screen has no empty groups and no duplicate ids.
 *     [if] duplicate group or row id, or empty group list [then ⛔️]
 */
import type { SettingDef } from './catalog-types';

/** One row of a customise screen: a live Settings catalog key, or an inert placeholder. */
export type CustomiseRow =
	| { kind: 'setting'; id: string }
	| { kind: 'inert'; id: string; label: string; detail: string };

export interface CustomiseGroup {
	id: string;
	label: string;
	rows: readonly CustomiseRow[];
}

/** A customise screen as data: every live row NAMES a Settings catalog entry. */
export interface CustomiseScreenDef {
	id: string;
	label: string;
	groups: readonly CustomiseGroup[];
}

export interface ResolvedCustomiseRow {
	row: CustomiseRow;
	def: SettingDef | null; // null exactly when row.kind === 'inert'
}

export interface ResolvedCustomiseGroup {
	id: string;
	label: string;
	rows: readonly ResolvedCustomiseRow[];
}

export interface ResolvedCustomiseScreen {
	id: string;
	label: string;
	groups: readonly ResolvedCustomiseGroup[];
}

/** Resolve a live row key to its catalog entry; missing, unimplemented or non boolean/enum/number throws. */
export function resolveCustomiseSetting(
	catalog: readonly SettingDef[],
	id: string,
	owner: string
): SettingDef {
	const def = catalog.find((entry) => entry.id === id);
	if (def === undefined) throw new Error(`${owner}: no Settings catalog entry ${id}`);
	if (!def.implemented) throw new Error(`${owner}: catalog entry ${id} is not implemented`);
	if (def.control.kind !== 'boolean' && def.control.kind !== 'enum' && def.control.kind !== 'number') {
		throw new Error(
			`${owner}: ${id} is a ${def.control.kind} control, only boolean, enum and number render here`
		);
	}
	return def;
}

function _seenDuplicate<T>(set: Set<T>, value: T, label: string): void {
	if (set.has(value)) throw new Error(label);
	set.add(value);
}

/** Resolve every row of a screen once; any bad key, empty group or duplicate id throws. */
export function resolveCustomiseScreen(
	catalog: readonly SettingDef[],
	screen: CustomiseScreenDef
): ResolvedCustomiseScreen {
	if (screen.groups.length === 0)
		throw new Error(`customise screen ${screen.id}: screen has no groups`);

	const ownerBase = `customise screen ${screen.id}`;
	const seenGroupIds = new Set<string>();
	const seenRowIds = new Set<string>();

	const groups: ResolvedCustomiseGroup[] = [];
	for (const group of screen.groups) {
		_seenDuplicate(seenGroupIds, group.id, `${ownerBase}: duplicate group id ${group.id}`);
		if (group.rows.length === 0)
			throw new Error(`${ownerBase}: group ${group.id} has no rows`);

		const rows: ResolvedCustomiseRow[] = [];
		for (const row of group.rows) {
			_seenDuplicate(seenRowIds, row.id, `${ownerBase}: duplicate row id ${row.id}`);
			if (row.kind === 'inert') {
				rows.push({ row, def: null });
			} else {
				const def = resolveCustomiseSetting(catalog, row.id, ownerBase);
				rows.push({ row, def });
			}
		}
		groups.push({ id: group.id, label: group.label, rows });
	}

	return { id: screen.id, label: screen.label, groups };
}
