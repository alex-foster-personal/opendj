/**
 * Library "next-only" appropriateness: Camelot family + BPM window.
 */
import {
	compatibleKeyShift,
	effectiveCamelotKey,
	type CompatibleKeyShift
} from '$lib/player/key/camelot';
import { BPM_HALF_ABS } from '$lib/rb/bpm-heat';
import type { CompatibleFilterPrefs } from '$lib/rb/compatible-filter-prefs';
import {
	COMPATIBLE_FILTER_DEFAULTS,
	compatibleKeyChecked,
	compatibleKeyRule
} from '$lib/rb/compatible-filter-prefs';

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
	/** KEYSHIFT-03: a deck loaded shifted plays in its shifted key. Required: a
	 * caller that omits it fails loudly in computeNextOnlyRef, never as 0. */
	key_shift_semitones: number;
}

/** Reference for next-only: master, else playing loaded, else any loaded with key+BPM.
 * With no Camelot rule ticked the key is not checked, so a keyless deck can be the
 * reference (`needsKey: false`); otherwise a keyless master would hand the BPM
 * check to another deck's tempo. */
export function computeNextOnlyRef(
	states: readonly NextOnlyDeckSlice[],
	needsKey: boolean = true
): NextOnlyRef | null {
	const ordered = [
		...states.filter((s) => s.is_master && s.stable_id !== null),
		...states.filter((s) => s.playing && s.stable_id !== null),
		...states.filter((s) => s.stable_id !== null)
	];
	for (const s of ordered) {
		if ((s.key !== null || !needsKey) && s.bpm !== null && s.bpm > 0) {
			if (s.key === null) return { key: null, bpm: s.bpm };
			// Match against the key the deck is heard in, not its source key.
			const shift = s.key_shift_semitones;
			if (!Number.isInteger(shift)) {
				throw new Error(
					`computeNextOnlyRef: reference deck ${s.stable_id} has no key shift; pass key_shift_semitones`
				);
			}
			return { key: shift === 0 ? s.key : effectiveCamelotKey(s.key, shift), bpm: s.bpm };
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
	// Direction compares RAW tempo (PR #4014): a half-time candidate is below
	// the reference and a double-time one above, so `above` keeps only the
	// double fold and `below` only the half fold.
	for (const fold of [0.5, 2] as const) {
		if (prefs.bpm_direction === 'above' ? fold < 1 : prefs.bpm_direction === 'below' && fold > 1) continue;
		if (Math.abs(candidateBpm - refBpm * fold) <= BPM_HALF_ABS) return true;
	}
	return false;
}

export { COMPATIBLE_FILTER_DEFAULTS, compatibleKeyChecked };

/** The row's key match against the reference under the active mode and the
 * allowed key shift (KEYSHIFT-01..03), or null when it is not key-compatible
 * or the key is not checked at all (compatibleKeyChecked). */
export function compatibleRowKeyShift(
	rowKey: string | null,
	refKey: string | null,
	// Required: a caller that forgot the active prefs must not get the default
	// Transition rules and a 1 semitone shift (Sol P1, review 4395715251).
	prefs: CompatibleFilterPrefs
): CompatibleKeyShift | null {
	// No rule ticked in Transition: the key is not checked, so no shift is ever
	// proposed. Callers that filter gate on compatibleKeyChecked first.
	if (!compatibleKeyChecked(prefs)) return null;
	return compatibleKeyShift(rowKey, refKey, compatibleKeyRule(prefs), prefs.key_shift_max);
}

export function isAppropriateNext(
	row: { key: string | null; bpm: number | null },
	ref: NextOnlyRef,
	prefs: CompatibleFilterPrefs = COMPATIBLE_FILTER_DEFAULTS
): boolean {
	return (
		// No rule ticked means the key is not checked: BPM alone decides.
		(!compatibleKeyChecked(prefs) || compatibleRowKeyShift(row.key, ref.key, prefs) !== null) &&
		bpmMatchesCompatiblePrefs(row.bpm, ref.bpm, prefs)
	);
}
