/**
 * Library "next-only" appropriateness: Camelot family + BPM window.
 * Direct ±SWEET_PCT matches suggest-next; half/double within BPM_HALF_ABS
 * stay eligible (fun transitions, not "wrong" BPM).
 */
import { camelotKeysAreCompatible } from '$lib/rb/audio-engine.svelte';
import { BPM_HALF_ABS, BPM_SWEET_PCT } from '$lib/rb/bpm-heat';

/** Match apps.shared.harmonic.MAX_BPM_DIFF_PCT / candidates.bpm_window_pct. */
export const NEXT_BPM_WINDOW_PCT = BPM_SWEET_PCT;

export interface NextOnlyRef {
	key: string | null;
	bpm: number | null;
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
