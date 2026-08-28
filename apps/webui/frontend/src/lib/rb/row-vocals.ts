/**
 * Vocals for the library PreviewStrip blue bars, resolved WITHOUT a fetch.
 *
 * H20 / shared contract point 1: the browser table can show thousands of rows,
 * so rows render from the listing payload alone. This resolver reads three
 * already-in-memory sources in precedence order and never reaches the network:
 *
 *   1. `row.vocals`, hydrated into the listing payload - the base for every row
 *   2. a loaded deck's ANLZ, which is fetched on deck load anyway
 *   3. an ANLZ entry already in the client cache (select warms exactly one)
 *
 * A cache MISS is a cache miss: the strip renders without bars rather than
 * fetching to find out. This regressed once already ("All Tracks stays on
 * loading... for ages") and one `ensureAnlz` inside a row render brings it back.
 *
 * Lives outside BrowserPanel.svelte so the rule is unit-testable at all.
 */

import type { Vocals } from '$lib/rb/api-rb';

/** The only reads this resolver is allowed to make. Both are pure lookups. */
export interface RowVocalsSources {
	/** Listing-hydrated vocals per row, in render order. */
	rows: { stable_id: string; vocals: Vocals }[];
	/** Vocals derived from a loaded deck's already-fetched ANLZ. */
	deckVocals: { stable_id: string; vocals: Vocals }[];
	/**
	 * Cached client ANLZ vocals by stable_id. MUST be a pure read of what is
	 * already cached - passing a function that can start a fetch reintroduces
	 * the per-row fan-out this contract exists to prevent.
	 */
	cachedVocals: (stable_id: string) => Vocals | undefined;
}

/**
 * Vocals per stable_id for the currently rendered rows.
 *
 * A deck or cache entry only WINS when it is actually analyzed: an unanalyzed
 * overwrite would throw away a listing-hydrated answer for a worse one.
 */
export function resolveRowVocals(sources: RowVocalsSources): Record<string, Vocals> {
	const out: Record<string, Vocals> = {};
	for (const row of sources.rows) out[row.stable_id] = row.vocals;

	const preferAnalyzed = (stable_id: string, vocals: Vocals): void => {
		if (vocals.status !== 'not_analyzed') out[stable_id] = vocals;
	};
	for (const deck of sources.deckVocals) preferAnalyzed(deck.stable_id, deck.vocals);
	for (const row of sources.rows) {
		const cached = sources.cachedVocals(row.stable_id);
		if (cached !== undefined) preferAnalyzed(row.stable_id, cached);
	}
	return out;
}
