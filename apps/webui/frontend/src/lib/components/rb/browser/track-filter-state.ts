/**
 * Issue #10043: the Track Filter bar state of one pane, every column funnel's criterion plus an
 * on/off switch that suspends them without forgetting them (rekordbox Track Filter, mimicked).
 * Pure, runeless and JSON-serialisable, so the `library_filter` performance command reads and
 * replaces exactly what the bar shows.
 */

import { canonicalCamelotKey } from '$lib/rb/key-notation';
import {
	EMPTY_TRACK_TABLE_COLUMN_FILTERS,
	REKORDBOX_TRACK_COLOR_IDS,
	TRACK_COLOUR_NONE,
	genreMatchValue,
	mytagMatchValue,
	trackColourName,
	type TrackTableColumnFilters
} from './track-table-column-filters';

/** One pane's filter criteria and whether they are suspended (rows shown unfiltered). */
export interface TrackFilterState extends TrackTableColumnFilters {
	suspended: boolean;
}

export type TrackFilterColumn = keyof TrackTableColumnFilters;

/** One chip on the bar: the column it edits and its one-line summary. */
export interface TrackFilterChip {
	column: TrackFilterColumn;
	label: string;
}

export const EMPTY_TRACK_FILTER_STATE: TrackFilterState = Object.freeze({
	...EMPTY_TRACK_TABLE_COLUMN_FILTERS,
	suspended: false
});

/** Each column funnel's trigger button in the table header, so a chip click can open it. */
export const TRACK_FILTER_FUNNEL_TRIGGERS: Readonly<Record<TrackFilterColumn, string>> = {
	colour: '.colour-funnel-trigger',
	bpm: '.bpm-funnel-trigger',
	key: '.key-funnel-trigger',
	genre: 'button[aria-label="Genre filter"]',
	rating: 'button[aria-label="Rating filter"]',
	mytag: 'button[aria-label="My Tag filter"]'
};

/** The criteria the table applies: none while suspended, the stored ones otherwise. */
export function effectiveColumnFilters(state: TrackFilterState): TrackTableColumnFilters {
	if (state.suspended) return EMPTY_TRACK_TABLE_COLUMN_FILTERS;
	const { suspended: _suspended, ...filters } = state;
	return filters;
}

function _bpmLabel(min: number | null, max: number | null): string {
	if (min !== null && max !== null) return `${min} to ${max} BPM`;
	if (min !== null) return `${min} BPM and up`;
	return `up to ${max} BPM`;
}

/** One chip per non-empty criterion, in column order, each with its summary ("120 to 128 BPM"). */
export function trackFilterChips(filters: TrackTableColumnFilters): TrackFilterChip[] {
	const chips: TrackFilterChip[] = [];
	if (filters.colour.selected.length > 0) {
		chips.push({
			column: 'colour',
			label: filters.colour.selected.map(trackColourName).join(', ')
		});
	}
	if (filters.bpm.min !== null || filters.bpm.max !== null) {
		chips.push({ column: 'bpm', label: _bpmLabel(filters.bpm.min, filters.bpm.max) });
	}
	if (filters.key.selected.length > 0) {
		chips.push({ column: 'key', label: filters.key.selected.join(', ') });
	}
	if (filters.genre.selected.length > 0) {
		chips.push({ column: 'genre', label: filters.genre.selected.join(', ') });
	}
	if (filters.rating.minStars > 0) {
		const stars = filters.rating.minStars;
		chips.push({ column: 'rating', label: `${stars} star${stars === 1 ? '' : 's'} and up` });
	}
	if (filters.mytag.selected.length > 0) {
		const joiner = filters.mytag.mode === 'and' ? ' and ' : ', ';
		chips.push({ column: 'mytag', label: filters.mytag.selected.join(joiner) });
	}
	return chips;
}

// ---------------------------------------------------------------------------
// Parsing the `library_filter` set payload: refuse, never guess.

type UnknownRecord = Record<string, unknown>;

function _record(path: string, value: unknown, keys: readonly string[]): UnknownRecord {
	if (typeof value !== 'object' || value === null || Array.isArray(value)) {
		throw new TypeError(`${path} must be an object`);
	}
	const record = value as UnknownRecord;
	const unknown = Object.keys(record).find((key) => !keys.includes(key));
	if (unknown !== undefined) throw new TypeError(`unknown field ${path}.${unknown}`);
	return record;
}

function _strings(
	path: string,
	value: unknown,
	normalise: (raw: string) => string | null
): string[] {
	if (!Array.isArray(value)) throw new TypeError(`${path} must be an array of strings`);
	const out = new Set<string>();
	for (const raw of value) {
		const normalised = typeof raw === 'string' ? normalise(raw) : null;
		if (normalised === null || normalised === '') {
			throw new TypeError(`${path} has an invalid entry ${JSON.stringify(raw)}`);
		}
		out.add(normalised);
	}
	return [...out];
}

function _bpmBound(path: string, value: unknown): number | null {
	if (value === null) return null;
	if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) {
		throw new TypeError(`${path} must be null or a number of 0 or more`);
	}
	return value;
}

const _COLOUR_IDS: readonly number[] = [TRACK_COLOUR_NONE, ...REKORDBOX_TRACK_COLOR_IDS];

/** A whole pane state from JSON: a missing column is empty; an unknown field or min above max BPM throws. */
export function parseTrackFilterState(value: unknown): TrackFilterState {
	const columns = Object.keys(EMPTY_TRACK_FILTER_STATE);
	const state = _record('state', value, columns);
	const out: TrackFilterState = { ...EMPTY_TRACK_FILTER_STATE };
	if (state.suspended !== undefined) {
		if (typeof state.suspended !== 'boolean')
			throw new TypeError('state.suspended must be a boolean');
		out.suspended = state.suspended;
	}
	if (state.colour !== undefined) {
		const colour = _record('state.colour', state.colour, ['selected']);
		const ids = colour.selected;
		if (!Array.isArray(ids) || !ids.every((id) => _COLOUR_IDS.includes(id as number))) {
			throw new TypeError('state.colour.selected must hold colour ids 0 to 8');
		}
		out.colour = { selected: [...new Set(ids as number[])] };
	}
	if (state.bpm !== undefined) {
		const bpm = _record('state.bpm', state.bpm, ['min', 'max']);
		const min = _bpmBound('state.bpm.min', bpm.min ?? null);
		const max = _bpmBound('state.bpm.max', bpm.max ?? null);
		if (min !== null && max !== null && min > max)
			throw new TypeError('state.bpm.min is above state.bpm.max');
		out.bpm = { min, max };
	}
	if (state.key !== undefined) {
		const key = _record('state.key', state.key, ['selected']);
		out.key = { selected: _strings('state.key.selected', key.selected, canonicalCamelotKey) };
	}
	if (state.genre !== undefined) {
		const genre = _record('state.genre', state.genre, ['selected']);
		out.genre = { selected: _strings('state.genre.selected', genre.selected, genreMatchValue) };
	}
	if (state.rating !== undefined) {
		const rating = _record('state.rating', state.rating, ['minStars']);
		const stars = rating.minStars;
		if (typeof stars !== 'number' || !Number.isInteger(stars) || stars < 0 || stars > 5) {
			throw new TypeError('state.rating.minStars must be an integer 0 to 5');
		}
		out.rating = { minStars: stars };
	}
	if (state.mytag !== undefined) {
		const mytag = _record('state.mytag', state.mytag, ['selected', 'mode']);
		const mode = mytag.mode ?? 'or';
		if (mode !== 'and' && mode !== 'or') throw new TypeError('state.mytag.mode must be and or or');
		out.mytag = {
			selected: _strings('state.mytag.selected', mytag.selected, mytagMatchValue),
			mode
		};
	}
	return out;
}
