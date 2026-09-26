/**
 * Library "next-only" appropriateness: Camelot family + BPM window.
 */
import { camelotKeysWithinSteps } from '$lib/player/key/camelot';
import { BPM_HALF_ABS } from '$lib/rb/bpm-heat';
import type { CompatibleFilterPrefs } from '$lib/rb/compatible-filter-prefs';
import { COMPATIBLE_FILTER_DEFAULTS } from '$lib/rb/compatible-filter-prefs';

/** Same-BPM mode: one-decimal presentation tolerance, not the symmetric ±window. */
export const SAME_BPM_TOLERANCE = 0.1;

/** Pin 007fed0da025 (the maintainer, Wed 2 Sep 2026): only a tiny exact search result
 * may be shown outside active filters. */
export const SEARCH_FILTER_FALLBACK_MAX_ROWS = 2;

export interface SearchFilterFallback<Row> {
	rows: Row[];
	ignoredFilters: string[];
}

export function resolveSearchFilterFallback<Row>(
	filteredRows: Row[],
	unfilteredRows: Row[],
	activeFilterNames: string[]
): SearchFilterFallback<Row> {
	if (
		activeFilterNames.length > 0 &&
		filteredRows.length === 0 &&
		unfilteredRows.length > 0 &&
		unfilteredRows.length <= SEARCH_FILTER_FALLBACK_MAX_ROWS
	) {
		return { rows: unfilteredRows, ignoredFilters: activeFilterNames };
	}
	return { rows: filteredRows, ignoredFilters: [] };
}

export function selectSearchFilterFallback<T>(
	query: string,
	filteredRows: readonly T[],
	rows: T[],
	complete: boolean
): T[] | null {
	if (!complete || query.trim() === '' || filteredRows.length !== 0) return null;
	return rows.length >= 1 && rows.length <= 2 ? rows : null;
}

export interface NextOnlyRef {
	key: string | null;
	bpm: number | null;
}

export interface NextOnlyDeckSlice {
	is_master: boolean;
	playing: boolean;
	stable_id: string | null;
	key: string | null;
	bpm: number | null;
}

/** Reference for next-only: master, else playing loaded, else any loaded with key+BPM. */
export function computeNextOnlyRef(states: readonly NextOnlyDeckSlice[]): NextOnlyRef | null {
	const ordered = [
		...states.filter((s) => s.is_master && s.stable_id !== null),
		...states.filter((s) => s.playing && s.stable_id !== null),
		...states.filter((s) => s.stable_id !== null)
	];
	for (const s of ordered) {
		if (s.key !== null && s.bpm !== null && s.bpm > 0) {
			return { key: s.key, bpm: s.bpm };
		}
	}
	return null;
}

export function bpmMatchesCompatiblePrefs(
	candidateBpm: number | null,
	refBpm: number | null,
	prefs: CompatibleFilterPrefs
): boolean {
	if (!prefs.bpm_enabled) return true;
	if (
		candidateBpm === null ||
		refBpm === null ||
		!(candidateBpm > 0) ||
		!(refBpm > 0)
	) {
		return false;
	}
	if (prefs.bpm_direction === 'same') {
		return Math.abs(candidateBpm - refBpm) <= SAME_BPM_TOLERANCE;
	}
	const window = prefs.bpm_window_bpm;
	const delta = candidateBpm - refBpm;
	let within = false;
	if (prefs.bpm_direction === 'above') {
		within = delta >= 0 && delta <= window;
	} else if (prefs.bpm_direction === 'below') {
		within = delta <= 0 && -delta <= window;
	} else {
		within = Math.abs(delta) <= window;
	}
	if (within) return true;
	if (!prefs.allow_half_double) return false;
	for (const fold of [0.5, 2] as const) {
		if (Math.abs(candidateBpm - refBpm * fold) <= BPM_HALF_ABS) return true;
	}
	return false;
}

export { COMPATIBLE_FILTER_DEFAULTS };

export function isAppropriateNext(
	row: { key: string | null; bpm: number | null },
	ref: NextOnlyRef,
	prefs: CompatibleFilterPrefs = COMPATIBLE_FILTER_DEFAULTS
): boolean {
	return (
		camelotKeysWithinSteps(row.key, ref.key, prefs.camelot_steps) &&
		bpmMatchesCompatiblePrefs(row.bpm, ref.bpm, prefs)
	);
}
