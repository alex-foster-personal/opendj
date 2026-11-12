/**
 * Per-column track-table funnel filters (issue #10041 colour; composes with the
 * other column funnels). Pure and runeless for node:test.
 *
 * OR within one column (pick several colours -> any match). AND across columns.
 * Empty / inactive constraint returns the input array unchanged (same reference).
 */

import type { BrowserRow } from './pane-contract.svelte';

/** Rekordbox track colour labels 1..8; 0 = no colour. */
export const REKORDBOX_TRACK_COLOR_IDS = [1, 2, 3, 4, 5, 6, 7, 8] as const;

export type RekordboxTrackColorId = (typeof REKORDBOX_TRACK_COLOR_IDS)[number];

export const TRACK_COLOUR_NONE = 0;

const COLOUR_SWATCH: Record<number, string> = {
	1: '#ff0000',
	2: '#ff8c00',
	3: '#ffff00',
	4: '#00ff00',
	5: '#00c8ff',
	6: '#0000ff',
	7: '#ff00ff',
	8: '#ffffff'
};

export function trackColourSwatch(colorId: number | null | undefined): string | null {
	const id = normalizeTrackColorId(colorId);
	return id === TRACK_COLOUR_NONE ? null : (COLOUR_SWATCH[id] ?? null);
}

export function normalizeTrackColorId(colorId: number | null | undefined): number {
	if (colorId === null || colorId === undefined || !Number.isFinite(colorId) || colorId <= 0) {
		return TRACK_COLOUR_NONE;
	}
	return Math.trunc(colorId);
}

export interface ColourColumnFilter {
	/** Picked ColorIDs including 0 for "No colour"; empty = no colour constraint. */
	selected: readonly number[];
}

export const EMPTY_COLOUR_FILTER: ColourColumnFilter = Object.freeze({
	selected: Object.freeze([])
});

/** Stub slots for other column funnels; inactive until their issues land. */
export interface TrackTableColumnFilters {
	colour: ColourColumnFilter;
	bpm: { min: number | null; max: number | null };
	genre: { selected: readonly string[] };
	key: { selected: readonly string[] };
	rating: { minStars: number };
	mytag: { tag: string | null };
}

export const EMPTY_TRACK_TABLE_COLUMN_FILTERS: TrackTableColumnFilters = Object.freeze({
	colour: EMPTY_COLOUR_FILTER,
	bpm: Object.freeze({ min: null, max: null }),
	genre: Object.freeze({ selected: Object.freeze([]) }),
	key: Object.freeze({ selected: Object.freeze([]) }),
	rating: Object.freeze({ minStars: 0 }),
	mytag: Object.freeze({ tag: null })
});

export function colourFilterActive(filter: ColourColumnFilter): boolean {
	return filter.selected.length > 0;
}

export function rowMatchesColourFilter(
	row: Pick<BrowserRow, 'color_id'>,
	filter: ColourColumnFilter
): boolean {
	if (!colourFilterActive(filter)) return true;
	const id = normalizeTrackColorId(row.color_id);
	return filter.selected.includes(id);
}

function rowMatchesBpm(row: Pick<BrowserRow, 'bpm'>, bpm: TrackTableColumnFilters['bpm']): boolean {
	if (bpm.min === null && bpm.max === null) return true;
	if (row.bpm === null || !Number.isFinite(row.bpm)) return false;
	if (bpm.min !== null && row.bpm < bpm.min) return false;
	if (bpm.max !== null && row.bpm > bpm.max) return false;
	return true;
}

function rowMatchesGenre(
	row: Pick<BrowserRow, 'genre'>,
	genre: TrackTableColumnFilters['genre']
): boolean {
	if (genre.selected.length === 0) return true;
	const g = (row.genre ?? '').trim().toLowerCase();
	return genre.selected.some((pick) => pick === g);
}

function rowMatchesKey(row: Pick<BrowserRow, 'key'>, key: TrackTableColumnFilters['key']): boolean {
	if (key.selected.length === 0) return true;
	const k = (row.key ?? '').trim();
	return key.selected.includes(k);
}

function rowMatchesRating(
	row: Pick<BrowserRow, 'rating'>,
	rating: TrackTableColumnFilters['rating']
): boolean {
	if (rating.minStars <= 0) return true;
	const stars = row.rating ?? 0;
	return stars >= rating.minStars;
}

function rowMatchesMytag(
	row: Pick<BrowserRow, 'tags'>,
	mytag: TrackTableColumnFilters['mytag']
): boolean {
	if (mytag.tag === null || mytag.tag === '') return true;
	return (row.tags ?? []).includes(mytag.tag);
}

export function columnFiltersActiveCount(filters: TrackTableColumnFilters): number {
	let n = 0;
	if (colourFilterActive(filters.colour)) n++;
	if (filters.bpm.min !== null || filters.bpm.max !== null) n++;
	if (filters.genre.selected.length > 0) n++;
	if (filters.key.selected.length > 0) n++;
	if (filters.rating.minStars > 0) n++;
	if (filters.mytag.tag) n++;
	return n;
}

export function columnFiltersTitle(filters: TrackTableColumnFilters): string {
	const parts: string[] = [];
	if (colourFilterActive(filters.colour)) {
		parts.push(`Colour: ${filters.colour.selected.map(String).join(', ')}`);
	}
	if (filters.bpm.min !== null || filters.bpm.max !== null) {
		parts.push(`BPM: ${filters.bpm.min ?? '...'} - ${filters.bpm.max ?? '...'}`);
	}
	if (filters.genre.selected.length > 0) parts.push(`Genre: ${filters.genre.selected.join(', ')}`);
	if (filters.key.selected.length > 0) parts.push(`Key: ${filters.key.selected.join(', ')}`);
	if (filters.rating.minStars > 0) parts.push(`Rating: >= ${filters.rating.minStars}`);
	if (filters.mytag.tag) parts.push(`My Tag: ${filters.mytag.tag}`);
	if (parts.length === 0) return 'Column filter - not connected in this view.';
	return parts.join(' · ');
}

export function applyTrackTableColumnFilters<T extends BrowserRow>(
	rows: readonly T[],
	filters: TrackTableColumnFilters
): readonly T[] {
	if (columnFiltersActiveCount(filters) === 0) return rows;
	return rows.filter(
		(row) =>
			rowMatchesColourFilter(row, filters.colour) &&
			rowMatchesBpm(row, filters.bpm) &&
			rowMatchesGenre(row, filters.genre) &&
			rowMatchesKey(row, filters.key) &&
			rowMatchesRating(row, filters.rating) &&
			rowMatchesMytag(row, filters.mytag)
	);
}

export function toggleColourSelection(
	filter: ColourColumnFilter,
	colorId: number
): ColourColumnFilter {
	const id = normalizeTrackColorId(colorId);
	const selected = new Set(filter.selected);
	if (selected.has(id)) selected.delete(id);
	else selected.add(id);
	return { selected: [...selected].sort((a, b) => a - b) };
}

export function clearColourFilter(): ColourColumnFilter {
	return EMPTY_COLOUR_FILTER;
}
