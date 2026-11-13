/**
 * LAYOUTSNAP-13869: a named layout snapshot (PLAN.md section 2.2 "layout preset"): one skin's
 * surface presets plus region px, saved and re-applied later. Pure data model only; no bus
 * verb, persistence or UI yet. Parsing is strict and never clamps.
 *
 * Requirements (mini-PRD):
 *   ? parseLayoutSnapshot: strict parse reusing parseLayoutOverride / parseLayoutRegion / parseLayoutPx.
 *     [if] a snapshot names an unknown region id [then] it throws naming the allowed region ids
 *     [if] a region px is outside its min..max [then] it throws with the allowed range and does not clamp
 *     [if] the layout block holds an unknown preset [then] it throws with the layoutPresetChoices list
 *     [if] parseLayoutSnapshot(JSON round trip of s) differs from s [then] the round-trip test fails
 */
import { parseLayoutPx, parseLayoutRegion, type LayoutRegionId } from '$lib/rb/layout-regions';
import { parseLayoutOverride, type LayoutOverride } from '$lib/rb/skin-layout';
import { parseUiSkin, type UiSkin } from '$lib/rb/ui-skin';

export type LayoutSnapshot = Readonly<{
	name: string;
	skin: UiSkin;
	layout: LayoutOverride;
	regions: Readonly<Partial<Record<LayoutRegionId, number>>>;
}>;

const SNAPSHOT_KEYS = ['name', 'skin', 'layout', 'regions'] as const;

function parseName(raw: unknown): string {
	if (typeof raw !== 'string' || raw.trim() === '') {
		throw new Error('layout snapshot name must be a non-empty string');
	}
	return raw;
}

function parseSkin(raw: unknown): UiSkin {
	const skin = parseUiSkin(raw);
	if (skin === undefined) throw new Error('layout snapshot skin is required');
	return skin;
}

function parseRegions(raw: unknown): Partial<Record<LayoutRegionId, number>> {
	if (raw === null || typeof raw !== 'object' || Array.isArray(raw)) {
		throw new Error('layout snapshot regions must be an object');
	}
	const out: Partial<Record<LayoutRegionId, number>> = {};
	for (const [key, px] of Object.entries(raw as Record<string, unknown>)) {
		const id = parseLayoutRegion(key);
		out[id] = parseLayoutPx(id, px);
	}
	return out;
}

export function parseLayoutSnapshot(raw: unknown): LayoutSnapshot {
	if (raw === null || typeof raw !== 'object' || Array.isArray(raw)) {
		throw new Error('layout snapshot must be an object');
	}
	const obj = raw as Record<string, unknown>;
	for (const k of Object.keys(obj)) {
		if (!SNAPSHOT_KEYS.includes(k as (typeof SNAPSHOT_KEYS)[number])) {
			throw new Error(`layout snapshot keys must be ${SNAPSHOT_KEYS.join(', ')}; got ${k}`);
		}
	}
	return {
		name: parseName(obj.name),
		skin: parseSkin(obj.skin),
		layout: parseLayoutOverride(obj.layout),
		regions: parseRegions(obj.regions)
	};
}

/** Build a snapshot from current state values; validates like parseLayoutSnapshot. */
export function snapshotFromState(
	name: string,
	skin: UiSkin,
	layout: LayoutOverride,
	regionsPx: Readonly<Partial<Record<LayoutRegionId, number>>>
): LayoutSnapshot {
	return parseLayoutSnapshot({ name, skin, layout: { ...layout }, regions: { ...regionsPx } });
}
