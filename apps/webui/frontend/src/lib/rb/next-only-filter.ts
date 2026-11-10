/**
 * Library "next-only" appropriateness: Camelot family + BPM window + energy rule.
 */
import {
	compatibleKeyShift,
	effectiveCamelotKey,
	type CompatibleKeyShift
} from '$lib/player/key/camelot';
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
	/** SETPREP-11947: the reference track's MIK energy; null/absent = unknown, energy rule skipped. */
	energy?: number | null;
}

export interface NextOnlyDeckSlice {
	is_master: boolean;
	playing: boolean;
	stable_id: string | null;
	key: string | null;
	/** The track's own (tag) BPM. */
	bpm: number | null;
	/** COMPATBPM-01: the deck's tempo ratio (1 = unpitched). The reference is the
	 * tempo the deck is heard at, `bpm * pitch`, not the track's own BPM. */
	pitch: number;
	/** COMPATBPM-03: the loaded track's library BPM confidence. 0 marks a
	 * placeholder grid (unknown tempo), so that deck is not a BPM reference.
	 * null means none published (rekordbox) and the BPM is trusted. */
	bpm_confidence: number | null;
	/** KEYSHIFT-03: a deck loaded shifted plays in its shifted key. Required: a
	 * caller that omits it fails loudly in computeNextOnlyRef, never as 0. */
	key_shift_semitones: number;
}

/** COMPATBPM-03: a BPM is known only when it is a positive number and its
 * confidence is not 0 (the placeholder grid of an unanalysed track publishes
 * 125.0 at confidence 0). Unknown BPMs never match a BPM window. */
export function bpmIsKnown(bpm: number | null | undefined, confidence: number | null | undefined): bpm is number {
	return typeof bpm === 'number' && bpm > 0 && confidence !== 0;
}

/** COMPATBPM-03: the library's BPM confidence for a loaded deck track, from the
 * held rows (library index or pane rows); null when the track is not in them. */
export function bpmConfidenceIn(
	stableId: string | null,
	rows: readonly { stable_id: string; bpm_confidence?: number | null }[] | null | undefined
): number | null {
	if (stableId === null || !rows) return null;
	return rows.find((r) => r.stable_id === stableId)?.bpm_confidence ?? null;
}

/** Reference for next-only: master, else playing loaded, else any loaded with key+BPM.
 * The reference BPM is the deck's heard tempo (bpm * pitch, COMPATBPM-01); a deck
 * whose BPM is unknown (COMPATBPM-03) is passed over like a deck with no BPM.
 * With no Camelot rule ticked the key is not checked, so a keyless deck can be the
 * reference (`needsKey: false`); otherwise a keyless master would hand the BPM
 * check to another deck's tempo. */
export function computeNextOnlyRef(
	states: readonly NextOnlyDeckSlice[],
	needsKey: boolean = true
): NextOnlyRef | null {
	const s = nextOnlyReferenceDeck(states, needsKey);
	if (s === null || !bpmIsKnown(s.bpm, s.bpm_confidence)) return null;
	if (!(typeof s.pitch === 'number' && s.pitch > 0)) {
		throw new Error(`computeNextOnlyRef: reference deck ${s.stable_id} has no tempo ratio; pass pitch`);
	}
	const bpm = s.bpm * s.pitch;
	if (s.key === null) return { key: null, bpm };
	// Match against the key the deck is heard in, not its source key.
	const shift = s.key_shift_semitones;
	if (!Number.isInteger(shift)) {
		throw new Error(
			`computeNextOnlyRef: reference deck ${s.stable_id} has no key shift; pass key_shift_semitones`
		);
	}
	return { key: shift === 0 ? s.key : effectiveCamelotKey(s.key, shift), bpm };
}

/** The deck computeNextOnlyRef takes as the reference, or null. */
export function nextOnlyReferenceDeck(
	states: readonly NextOnlyDeckSlice[],
	needsKey: boolean = true
): NextOnlyDeckSlice | null {
	const ordered = [
		...states.filter((s) => s.is_master && s.stable_id !== null),
		...states.filter((s) => s.playing && s.stable_id !== null),
		...states.filter((s) => s.stable_id !== null)
	];
	for (const s of ordered) {
		if ((s.key !== null || !needsKey) && bpmIsKnown(s.bpm, s.bpm_confidence)) return s;
	}
	return null;
}

/** SETPREP-11947: an energy is known when it is a finite number. */
export function energyIsKnown(e: number | null | undefined): e is number {
	return typeof e === 'number' && Number.isFinite(e);
}

/** SETPREP-11947: the library's MIK energy for a loaded deck track, from the held rows. */
export function energyIn(
	stableId: string | null,
	rows: readonly { stable_id: string; energy?: number | null }[] | null | undefined
): number | null {
	if (stableId === null || !rows) return null;
	const e = rows.find((r) => r.stable_id === stableId)?.energy;
	return energyIsKnown(e) ? e : null;
}

/** SETPREP-11947: the energy rule. Off (or absent) and an unknown reference
 * energy keep every row; a row with no energy is excluded otherwise. */
export function energyMatchesCompatiblePrefs(
	rowEnergy: number | null | undefined,
	refEnergy: number | null | undefined,
	prefs: CompatibleFilterPrefs
): boolean {
	const mode = prefs.energy_mode;
	if (mode === undefined || mode === 'off') return true;
	if (!energyIsKnown(refEnergy)) return true;
	if (!energyIsKnown(rowEnergy)) return false;
	const d = rowEnergy - refEnergy;
	if (mode === 'within_1') return d >= -1 && d <= 1;
	if (mode === 'boost') return d >= 1 && d <= 2;
	if (mode === 'drop') return d >= -2 && d <= -1;
	throw new Error(`energyMatchesCompatiblePrefs: unknown energy_mode ${String(mode)}`);
}

export const ENERGY_RULE_SKIPPED_NOTE = 'Energy rule skipped: the reference track has no energy';

/** SETPREP-11947: the panel note for the energy rule, or null when there is nothing to say. */
export function compatibleEnergyNote(
	rows: readonly { energy?: number | null }[],
	ref: NextOnlyRef | null,
	prefs: CompatibleFilterPrefs
): string | null {
	const mode = prefs.energy_mode;
	if (mode === undefined || mode === 'off' || ref === null) return null;
	if (!energyIsKnown(ref.energy)) return ENERGY_RULE_SKIPPED_NOTE;
	const n = rows.filter((r) => !energyIsKnown(r.energy)).length;
	if (n === 0) return null;
	return n === 1 ? '1 row has no energy' : `${n} rows have no energy`;
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
	// COMPATBPM-02: the fold is judged at the reference's tempo with the user's
	// own window (a 64 BPM row against 128 is heard as 128). It used to be a
	// fixed 15 BPM around ref * fold, borrowed from the BPM heat colours, which
	// at a 125 master let 47.5 to 77.5 BPM rows through (up to 24% off).
	for (const fold of [0.5, 2] as const) {
		if (prefs.bpm_direction === 'above' ? fold < 1 : prefs.bpm_direction === 'below' && fold > 1) continue;
		if (Math.abs(candidateBpm / fold - refBpm) <= window) return true;
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
	row: { key: string | null; bpm: number | null; bpm_confidence?: number | null; energy?: number | null },
	ref: NextOnlyRef,
	prefs: CompatibleFilterPrefs = COMPATIBLE_FILTER_DEFAULTS
): boolean {
	return (
		// No rule ticked means the key is not checked: BPM alone decides.
		(!compatibleKeyChecked(prefs) || compatibleRowKeyShift(row.key, ref.key, prefs) !== null) &&
		// COMPATBPM-03: a placeholder BPM (confidence 0) is unknown, never a match.
		bpmMatchesCompatiblePrefs(bpmIsKnown(row.bpm, row.bpm_confidence) ? row.bpm : null, ref.bpm, prefs) &&
		energyMatchesCompatiblePrefs(row.energy, ref.energy, prefs)
	);
}
