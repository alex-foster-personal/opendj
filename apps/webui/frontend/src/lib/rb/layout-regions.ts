/**
 * DL-2 (#9429): resizable layout regions, their bounds and their store.
 * Agent parity for every drag (PLAN.md section 2.3): set_layout_size refuses an
 * out-of-range width with the allowed range and never clamps it.
 */
import {
	PLAYLIST_TREE_WIDTH_DEFAULT,
	PLAYLIST_TREE_WIDTH_MAX,
	PLAYLIST_TREE_WIDTH_MIN,
	setPlaylistTreeWidth,
	uiPrefs
} from '$lib/rb/prefs.svelte';

export const LAYOUT_REGION_IDS = ['browser.tree'] as const;
export type LayoutRegionId = (typeof LAYOUT_REGION_IDS)[number];

export interface LayoutRegionView {
	px: number;
	min: number;
	max: number;
	collapsed: boolean;
}
export interface LayoutView {
	skin: string;
	regions: Record<LayoutRegionId, LayoutRegionView>;
}

interface Region {
	min: number;
	max: number;
	fallback: number;
	read(): number;
	write(px: number): void;
}

const REGIONS: Record<LayoutRegionId, Region> = {
	'browser.tree': {
		min: PLAYLIST_TREE_WIDTH_MIN,
		max: PLAYLIST_TREE_WIDTH_MAX,
		fallback: PLAYLIST_TREE_WIDTH_DEFAULT,
		read: () => uiPrefs.playlist_tree_width,
		write: setPlaylistTreeWidth
	}
};

export function parseLayoutRegion(value: unknown): LayoutRegionId {
	const text = String(value);
	for (const id of LAYOUT_REGION_IDS) {
		if (id === text) return id;
	}
	throw new TypeError(`region must be one of ${LAYOUT_REGION_IDS.join(', ')}; got ${text}`);
}

export function parseLayoutPx(region: LayoutRegionId, value: unknown): number {
	const row = REGIONS[region];
	const { min, max } = row;
	if (typeof value !== 'number' || !Number.isFinite(value)) {
		throw new RangeError(
			`px for ${region} must be a whole number within ${min}..${max}; got ${String(value)}`
		);
	}
	if (!Number.isInteger(value) || value < min || value > max) {
		throw new RangeError(
			`px for ${region} must be a whole number within ${min}..${max}; got ${String(value)}`
		);
	}
	return value;
}

export function setLayoutSize(region: LayoutRegionId, px: number): void {
	const validated = parseLayoutPx(region, px);
	REGIONS[region].write(validated);
}

export function resetLayout(region?: LayoutRegionId): void {
	const targets: LayoutRegionId[] = region === undefined ? [...LAYOUT_REGION_IDS] : [region];
	for (const id of targets) {
		REGIONS[id].write(REGIONS[id].fallback);
	}
}

export function layoutView(): LayoutView {
	const regions = {} as Record<LayoutRegionId, LayoutRegionView>;
	for (const id of LAYOUT_REGION_IDS) {
		const row = REGIONS[id];
		regions[id] = {
			px: row.read(),
			min: row.min,
			max: row.max,
			// No collapse UI for the playlist tree today; false is the real value, not a stub.
			collapsed: false
		};
	}
	return { skin: uiPrefs.ui_skin, regions };
}
