/**
 * Per-skin layout size map: `ui_prefs.layout[skinId][regionId] = px`.
 * Pure helpers; no prefs store or disk wiring here.
 */

export type LayoutSizeMap = Record<string, Record<string, number>>;

export const TREE_REGION_ID = 'browser.tree';

export interface ParseResult {
	map: LayoutSizeMap;
	dropped: string[];
}

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isValidPx(value: unknown): value is number {
	return typeof value === 'number' && Number.isFinite(value) && value > 0;
}

/** Parse a raw layout map, dropping entries whose px is not a finite positive number. */
export function parseLayoutSizes(raw: unknown): ParseResult {
	const map: LayoutSizeMap = {};
	const dropped: string[] = [];
	if (!isRecord(raw)) return { map, dropped };
	for (const [skinId, regions] of Object.entries(raw)) {
		if (!isRecord(regions)) continue;
		const out: Record<string, number> = {};
		for (const [regionId, px] of Object.entries(regions)) {
			if (isValidPx(px)) out[regionId] = px;
			else dropped.push(regionId);
		}
		map[skinId] = out;
	}
	return { map, dropped };
}

export interface MigrateResult {
	layout: LayoutSizeMap;
	migrated: boolean;
}

/** Move legacy `playlist_tree_width` into `layout[skinId]['browser.tree']` once. */
export function migrateTreeWidth(prefs: unknown, skinId: string): MigrateResult {
	const source = isRecord(prefs) ? prefs : {};
	const { map } = parseLayoutSizes(source.layout);
	const skin = { ...(map[skinId] ?? {}) };
	const legacy = source.playlist_tree_width;
	if (skin[TREE_REGION_ID] !== undefined || !isValidPx(legacy)) {
		return { layout: map, migrated: false };
	}
	skin[TREE_REGION_ID] = legacy;
	return { layout: { ...map, [skinId]: skin }, migrated: true };
}

/** Size for a region in one skin; never falls back to another skin. */
export function sizeFor(map: LayoutSizeMap, skinId: string, regionId: string): number | undefined {
	const skin = map[skinId];
	if (skin === undefined || !Object.hasOwn(skin, regionId)) return undefined;
	return skin[regionId];
}
