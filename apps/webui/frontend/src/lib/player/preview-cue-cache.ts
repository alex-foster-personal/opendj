/**
 * The preview cache's eviction policy, as a pure function.
 *
 * Mini-PRD
 * --------
 * R1 Given the resident tracks in least-recently-used order, a budget and the
 *    track that is currently sounding, decide which tracks to drop.
 * R2 The sounding track is never a candidate, whatever the budget says.
 * R3 Nothing is dropped while the total already fits.
 *
 *   [if] the total fits the budget [then] the result is empty ⛔️
 *   [if] the total is over budget [then] tracks are dropped from the
 *        least-recently-used end until it fits ⛔️
 *   [if] the least-recently-used track is the sounding one [then] it is
 *        skipped and the next one is dropped instead ⛔️
 *   [if] one track alone is larger than the whole budget and it is sounding
 *        [then] nothing is dropped, because there is no eviction that would
 *        help and dropping it would be audible ⛔️
 *
 * This lives apart from `preview-cue.svelte.ts` for the same reason
 * `preview-cue-policy.ts` does: the decision is worth testing without an
 * AudioContext, a browser or a rune.
 *
 * Status: ✔︎ ✅ 🎯
 */

/** One resident decoded track. `bytes` is its PCM cost, not its file size. */
export type PreviewCacheEntry = { stable_id: string; bytes: number };

/**
 * Which tracks to drop, in the order to drop them.
 *
 * `entries` MUST be in least-recently-used order (a `Map`'s own iteration
 * order gives that for free when every hit re-inserts).
 */
export function previewCacheEvictions(
	entries: readonly PreviewCacheEntry[],
	budget_bytes: number,
	playing_stable_id: string | null
): string[] {
	let total = entries.reduce((sum, entry) => sum + entry.bytes, 0);
	const drop: string[] = [];
	for (const entry of entries) {
		if (total <= budget_bytes) break;
		if (entry.stable_id === playing_stable_id) continue;
		drop.push(entry.stable_id);
		total -= entry.bytes;
	}
	return drop;
}
