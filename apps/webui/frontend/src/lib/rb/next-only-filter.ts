/**
 * Library "next-only" appropriateness: Camelot family + BPM window.
 * Direct ±SWEET_PCT matches suggest-next; half/double within BPM_HALF_ABS
 * stay eligible (fun transitions, not "wrong" BPM).
 */
import { camelotKeysAreCompatible } from '$lib/rb/audio-engine.svelte';
import { BPM_HALF_ABS, BPM_SWEET_PCT } from '$lib/rb/bpm-heat';

/** Match apps.shared.harmonic.MAX_BPM_DIFF_PCT / candidates.bpm_window_pct. */
export const NEXT_BPM_WINDOW_PCT = BPM_SWEET_PCT;

/** Pin 007fed0da025 (the maintainer, Wed 2 Sep 2026): only a tiny exact search result
 * may be shown outside active filters. */
export const SEARCH_FILTER_FALLBACK_MAX_ROWS = 2;

export interface SearchFilterFallback<Row> {
	rows: Row[];
	ignoredFilters: string[];
}

/**
 * Preserve a tiny, exact search result when active filters hide every match.
 * Three or more matches remain hidden so filters retain their normal meaning.
 */
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

/** Recover only a precise search hidden by the compatible filter, never a broad result set. */
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

export function bpmInNextWindow(candidateBpm: number | null, refBpm: number | null): boolean {
	if (
		candidateBpm === null ||
		refBpm === null ||
		!(candidateBpm > 0) ||
		!(refBpm > 0)
	) {
		return false;
	}
	const lo = Math.min(candidateBpm, refBpm);
	const hi = Math.max(candidateBpm, refBpm);
	if (hi / lo <= 1 + NEXT_BPM_WINDOW_PCT / 100) return true;
	// Half / double of master within absolute BPM_HALF_ABS (e.g. 180↔90).
	for (const fold of [0.5, 2] as const) {
		if (Math.abs(candidateBpm - refBpm * fold) <= BPM_HALF_ABS) return true;
	}
	return false;
}

/** True when the row is an appropriate next track vs the reference (master). */
export function isAppropriateNext(
	row: { key: string | null; bpm: number | null },
	ref: NextOnlyRef
): boolean {
	return camelotKeysAreCompatible(row.key, ref.key) && bpmInNextWindow(row.bpm, ref.bpm);
}
