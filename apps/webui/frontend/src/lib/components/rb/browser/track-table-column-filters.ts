/**
 * Per-column track-table funnel filters (issue #10041 colour; composes with the
 * other column funnels). Pure and runeless for node:test.
 *
 * OR within one column (pick several colours -> any match). AND across columns.
 * Empty / inactive constraint returns the input array unchanged (same reference).
 */

import { canonicalCamelotKey } from '$lib/rb/key-notation';
import type { BrowserRow } from './pane-contract.svelte';

/** Rekordbox track colour labels 1..8; 0 = no colour. */
export const REKORDBOX_TRACK_COLOR_IDS = [1, 2, 3, 4, 5, 6, 7, 8] as const;

export type RekordboxTrackColorId = (typeof REKORDBOX_TRACK_COLOR_IDS)[number];

export const TRACK_COLOUR_NONE = 0;

/** Issue #9737: rekordbox track colour labels by ColorID (rekordbox XML Colour values, mimicked). */
const COLOUR_SWATCH: Record<number, string> = {
	1: '#ff007f',
	2: '#ff0000',
	3: '#ffa500',
	4: '#ffff00',
	5: '#00ff00',
	6: '#25fde9',
	7: '#0000ff',
	8: '#660099'
};

/** Issue #9737: the rekordbox name of each track colour label, by ColorID. */
export const TRACK_COLOUR_NAMES: Readonly<Record<RekordboxTrackColorId, string>> = {
	1: 'Pink',
	2: 'Red',
	3: 'Orange',
	4: 'Yellow',
	5: 'Green',
	6: 'Aqua',
	7: 'Blue',
	8: 'Purple'
};

/** The hover name of a row's colour label: its rekordbox name, or "No colour". */
export function trackColourName(colorId: number | null | undefined): string {
	const id = normalizeTrackColorId(colorId);
	return TRACK_COLOUR_NAMES[id as RekordboxTrackColorId] ?? 'No colour';
}

/** Issue #9737: the Colour column sort value, the ColorID; uncoloured rows are null and sort last. */
export function colourSortValue(row: Pick<BrowserRow, 'color_id'>): number | null {
	const id = normalizeTrackColorId(row.color_id);
	return id === TRACK_COLOUR_NONE ? null : id;
}

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

/** Issue #9738: the BPM funnel range, inclusive at both ends; a null bound is open. */
export interface BpmColumnFilter {
	min: number | null;
	max: number | null;
}

export const EMPTY_BPM_FILTER: BpmColumnFilter = Object.freeze({ min: null, max: null });

export type BpmRangeParse = { ok: true; filter: BpmColumnFilter } | { ok: false; reason: string };

/** Issue #9739: the Key funnel picks, canonical Camelot keys (8A), so a notation switch keeps them. */
export interface KeyColumnFilter {
	selected: readonly string[];
}

export const EMPTY_KEY_FILTER: KeyColumnFilter = Object.freeze({ selected: Object.freeze([]) });

/** Issue #9740: the Genre funnel picks, trimmed and lower-cased, so letter case never splits a genre. */
export interface GenreColumnFilter {
	selected: readonly string[];
}

export const EMPTY_GENRE_FILTER: GenreColumnFilter = Object.freeze({ selected: Object.freeze([]) });

/** Issue #9740: the Rating funnel minimum, 1 to 5 stars; 0 = no minimum. */
export interface RatingColumnFilter {
	minStars: number;
}

export const EMPTY_RATING_FILTER: RatingColumnFilter = Object.freeze({ minStars: 0 });

/** One genre the Genre funnel offers: its match value, the first spelling seen, and its row count. */
export interface GenreFacet {
	value: string;
	label: string;
	count: number;
}

/** Issue #9742: how several My Tag picks combine: ``or`` keeps a row holding any, ``and`` one holding all. */
export type MyTagMatchMode = 'and' | 'or';

/** Issue #9742: the My Tag funnel picks, each a normalised tag name (the catalog's ``name_norm``). */
export interface MyTagColumnFilter {
	selected: readonly string[];
	mode: MyTagMatchMode;
}

export const EMPTY_MYTAG_FILTER: MyTagColumnFilter = Object.freeze({
	selected: Object.freeze([]),
	mode: 'or'
});

/** Stub slots for other column funnels; inactive until their issues land. */
export interface TrackTableColumnFilters {
	colour: ColourColumnFilter;
	bpm: BpmColumnFilter;
	genre: GenreColumnFilter;
	key: KeyColumnFilter;
	rating: RatingColumnFilter;
	mytag: MyTagColumnFilter;
}

export const EMPTY_TRACK_TABLE_COLUMN_FILTERS: TrackTableColumnFilters = Object.freeze({
	colour: EMPTY_COLOUR_FILTER,
	bpm: EMPTY_BPM_FILTER,
	genre: EMPTY_GENRE_FILTER,
	key: EMPTY_KEY_FILTER,
	rating: EMPTY_RATING_FILTER,
	mytag: EMPTY_MYTAG_FILTER
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

export function bpmFilterActive(filter: BpmColumnFilter): boolean {
	return filter.min !== null || filter.max !== null;
}

/** The two funnel inputs as a range: blank is an open bound; junk, a negative or min above max is refused. */
export function parseBpmRangeInputs(minText: string, maxText: string): BpmRangeParse {
	const bound = (text: string): number | null | undefined => {
		const trimmed = text.trim();
		if (trimmed === '') return null;
		const value = Number(trimmed);
		return Number.isFinite(value) && value >= 0 ? value : undefined;
	};
	const min = bound(minText);
	const max = bound(maxText);
	if (min === undefined || max === undefined) {
		return { ok: false, reason: 'BPM must be a number of 0 or more' };
	}
	if (min !== null && max !== null && min > max) {
		return { ok: false, reason: 'Min BPM is above max BPM' };
	}
	return { ok: true, filter: min === null && max === null ? EMPTY_BPM_FILTER : { min, max } };
}

function rowMatchesBpm(row: Pick<BrowserRow, 'bpm'>, bpm: TrackTableColumnFilters['bpm']): boolean {
	if (!bpmFilterActive(bpm)) return true;
	if (row.bpm === null || !Number.isFinite(row.bpm)) return false;
	if (bpm.min !== null && row.bpm < bpm.min) return false;
	if (bpm.max !== null && row.bpm > bpm.max) return false;
	return true;
}

/** The match value of a genre: trimmed and lower-cased, the same rule the tracks API applies. */
export function genreMatchValue(genre: string | null | undefined): string {
	return (genre ?? '').trim().toLowerCase();
}

export function genreFilterActive(filter: GenreColumnFilter): boolean {
	return filter.selected.length > 0;
}

/** Pick or unpick one genre by its match value; a blank genre leaves the picks as they are. */
export function toggleGenreSelection(filter: GenreColumnFilter, genre: string): GenreColumnFilter {
	const value = genreMatchValue(genre);
	if (value === '') return filter;
	const selected = new Set(filter.selected);
	if (selected.has(value)) selected.delete(value);
	else selected.add(value);
	return selected.size === 0 ? EMPTY_GENRE_FILTER : { selected: [...selected] };
}

/** The genres present in a list with their row counts, most rows first, then by name. */
export function genreFacets(rows: readonly Pick<BrowserRow, 'genre'>[]): GenreFacet[] {
	const byValue = new Map<string, GenreFacet>();
	for (const row of rows) {
		const value = genreMatchValue(row.genre);
		if (value === '') continue;
		const facet = byValue.get(value);
		if (facet) facet.count++;
		else byValue.set(value, { value, label: (row.genre ?? '').trim(), count: 1 });
	}
	return [...byValue.values()].sort((a, b) => b.count - a.count || a.label.localeCompare(b.label));
}

function rowMatchesGenre(
	row: Pick<BrowserRow, 'genre'>,
	genre: TrackTableColumnFilters['genre']
): boolean {
	if (!genreFilterActive(genre)) return true;
	return genre.selected.includes(genreMatchValue(row.genre));
}

export function ratingFilterActive(filter: RatingColumnFilter): boolean {
	return filter.minStars > 0;
}

/** A minimum of ``stars`` (1 to 5); anything else, or the minimum already set, clears it. */
export function ratingFilterFor(filter: RatingColumnFilter, stars: number): RatingColumnFilter {
	if (!Number.isInteger(stars) || stars < 1 || stars > 5 || filter.minStars === stars) {
		return EMPTY_RATING_FILTER;
	}
	return { minStars: stars };
}

export function keyFilterActive(filter: KeyColumnFilter): boolean {
	return filter.selected.length > 0;
}

/** Pick or unpick one key, stored canonical; a key in no notation leaves the picks as they are. */
export function toggleKeySelection(filter: KeyColumnFilter, rawKey: string): KeyColumnFilter {
	const key = canonicalCamelotKey(rawKey);
	if (key === null) return filter;
	const selected = new Set(filter.selected);
	if (selected.has(key)) selected.delete(key);
	else selected.add(key);
	return selected.size === 0 ? EMPTY_KEY_FILTER : { selected: [...selected] };
}

function rowMatchesKey(row: Pick<BrowserRow, 'key'>, key: TrackTableColumnFilters['key']): boolean {
	if (!keyFilterActive(key)) return true;
	const canonical = canonicalCamelotKey(row.key);
	return canonical !== null && key.selected.includes(canonical);
}

function rowMatchesRating(
	row: Pick<BrowserRow, 'rating'>,
	rating: TrackTableColumnFilters['rating']
): boolean {
	if (!ratingFilterActive(rating)) return true;
	const stars = row.rating ?? 0;
	return stars >= rating.minStars;
}

/** A My Tag name as the tag catalog compares it: NFKC, lower-cased and trimmed, like the tracks API. */
export function mytagMatchValue(name: string): string {
	return name.normalize('NFKC').toLowerCase().trim();
}

export function mytagFilterActive(filter: MyTagColumnFilter): boolean {
	return filter.selected.length > 0;
}

/** Pick or unpick one My Tag by name; a blank name leaves the picks as they are. */
export function toggleMytagSelection(filter: MyTagColumnFilter, name: string): MyTagColumnFilter {
	const value = mytagMatchValue(name);
	if (value === '') return filter;
	const selected = new Set(filter.selected);
	if (selected.has(value)) selected.delete(value);
	else selected.add(value);
	return { selected: [...selected], mode: filter.mode };
}

/** The same picks matched the other way (any of them, or all of them). */
export function mytagFilterWithMode(
	filter: MyTagColumnFilter,
	mode: MyTagMatchMode
): MyTagColumnFilter {
	return filter.mode === mode ? filter : { selected: filter.selected, mode };
}

/** True when a row's tags hold any (``or``) or all (``and``) of the picks (no picks: every row). */
export function tagsMatchMytagFilter(tags: readonly string[], filter: MyTagColumnFilter): boolean {
	if (!mytagFilterActive(filter)) return true;
	const held = new Set(tags.map(mytagMatchValue));
	return filter.mode === 'and'
		? filter.selected.every((pick) => held.has(pick))
		: filter.selected.some((pick) => held.has(pick));
}

/** The tags a row holds; the table passes the live tag assignments, tests the row's own list. */
export type RowTagsLookup = (row: BrowserRow) => readonly string[];

const ROW_OWN_TAGS: RowTagsLookup = (row) => row.tags ?? [];

export function columnFiltersActiveCount(filters: TrackTableColumnFilters): number {
	let n = 0;
	if (colourFilterActive(filters.colour)) n++;
	if (bpmFilterActive(filters.bpm)) n++;
	if (genreFilterActive(filters.genre)) n++;
	if (keyFilterActive(filters.key)) n++;
	if (ratingFilterActive(filters.rating)) n++;
	if (mytagFilterActive(filters.mytag)) n++;
	return n;
}

export function columnFiltersTitle(filters: TrackTableColumnFilters): string {
	const parts: string[] = [];
	if (colourFilterActive(filters.colour)) {
		parts.push(`Colour: ${filters.colour.selected.map(String).join(', ')}`);
	}
	if (bpmFilterActive(filters.bpm)) {
		parts.push(`BPM: ${filters.bpm.min ?? '...'} - ${filters.bpm.max ?? '...'}`);
	}
	if (filters.genre.selected.length > 0) parts.push(`Genre: ${filters.genre.selected.join(', ')}`);
	if (filters.key.selected.length > 0) parts.push(`Key: ${filters.key.selected.join(', ')}`);
	if (filters.rating.minStars > 0) parts.push(`Rating: >= ${filters.rating.minStars}`);
	if (mytagFilterActive(filters.mytag)) {
		parts.push(`My Tag (${filters.mytag.mode}): ${filters.mytag.selected.join(', ')}`);
	}
	if (parts.length === 0) return 'Column filter - not connected in this view.';
	return parts.join(' · ');
}

export function applyTrackTableColumnFilters<T extends BrowserRow>(
	rows: readonly T[],
	filters: TrackTableColumnFilters,
	tagsOf: RowTagsLookup = ROW_OWN_TAGS
): readonly T[] {
	if (columnFiltersActiveCount(filters) === 0) return rows;
	return rows.filter(
		(row) =>
			rowMatchesColourFilter(row, filters.colour) &&
			rowMatchesBpm(row, filters.bpm) &&
			rowMatchesGenre(row, filters.genre) &&
			rowMatchesKey(row, filters.key) &&
			rowMatchesRating(row, filters.rating) &&
			tagsMatchMytagFilter(tagsOf(row), filters.mytag)
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
