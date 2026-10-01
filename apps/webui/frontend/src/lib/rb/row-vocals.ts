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

import type { AnlzData } from '$lib/rb/anlz-types';
import { decodePreviewStrip, type PreviewStripData, type Vocals } from '$lib/rb/api-rb';

/** Pure cache read for preview-strip resolution (no fetch entry points). */
export type RowStripCacheEntry =
	| { status: 'loading' }
	| {
			status: 'ready';
			data: {
				local_waveform?: {
					status: string;
					preview_b64: string | null;
					preview_max: number | null;
				} | null;
			};
	  }
	| { status: 'error'; code: string };

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

/** The only reads the strip marker resolver is allowed to make. All pure. */
export interface RowMarkerAnlzSources {
	/** Rendered rows, in render order. */
	rows: { stable_id: string }[];
	/** Each loaded deck's DISPLAYED ANLZ (already resolved against the cache). */
	decks: { stable_id: string; playing: boolean; anlz: AnlzData | null }[];
	/**
	 * READY cached ANLZ by stable_id. MUST be a pure read - a function that can
	 * start a fetch reintroduces the per-row fan-out (H20).
	 */
	cachedAnlz: (stable_id: string) => AnlzData | undefined;
}

/**
 * Cue/phrase marker ANLZ per stable_id for the library PreviewStrip (LIBUX-12),
 * sourced from the exact object the main waveform renders. A playing deck wins
 * over a paused one; a ready cache entry answers for a row on no deck. A row
 * with neither is ABSENT (a markerless strip), never a lookup that could fetch.
 */
export function resolveRowMarkerAnlz(sources: RowMarkerAnlzSources): Record<string, AnlzData> {
	const out: Record<string, AnlzData> = {};
	const playing = new Set<string>();
	for (const deck of sources.decks) {
		if (deck.anlz === null || playing.has(deck.stable_id)) continue;
		if (deck.playing) {
			out[deck.stable_id] = deck.anlz;
			playing.add(deck.stable_id);
		} else if (out[deck.stable_id] === undefined) {
			out[deck.stable_id] = deck.anlz;
		}
	}
	for (const row of sources.rows) {
		if (out[row.stable_id] !== undefined) continue;
		const cached = sources.cachedAnlz(row.stable_id);
		if (cached !== undefined) out[row.stable_id] = cached;
	}
	return out;
}

/** The only reads the preview-strip resolver is allowed to make. All pure. */
export interface RowPreviewStripSources {
	rows: { stable_id: string; strip: PreviewStripData | null }[];
	cachedAnlzEntry: (stable_id: string) => RowStripCacheEntry | undefined;
}

/**
 * Preview-strip bytes per stable_id for the library PreviewStrip column,
 * without per-row fetches. Listing-hydrated `row.strip` wins; otherwise a
 * ready cache entry with a decoded local waveform is decoded here.
 */
export function resolveRowPreviewStrip(sources: RowPreviewStripSources): Record<string, PreviewStripData | null> {
	const out: Record<string, PreviewStripData | null> = {};
	for (const row of sources.rows) {
		if (row.strip !== null) {
			out[row.stable_id] = row.strip;
			continue;
		}
		const entry = sources.cachedAnlzEntry(row.stable_id);
		if (
			entry?.status === 'ready' &&
			entry.data.local_waveform?.status === 'decoded'
		) {
			out[row.stable_id] = decodePreviewStrip(
				entry.data.local_waveform.preview_b64,
				entry.data.local_waveform.preview_max
			);
		} else {
			out[row.stable_id] = null;
		}
	}
	return out;
}

/**
 * Loading spinner state per stable_id for PreviewStrip. True only while the
 * shared cache entry is actively loading; absent cache or ready/error stays
 * not-loading so virtual rows never fan out /anlz themselves.
 */
export function resolveRowStripLoading(sources: RowPreviewStripSources): Record<string, boolean> {
	const out: Record<string, boolean> = {};
	for (const row of sources.rows) {
		if (row.strip !== null) {
			out[row.stable_id] = false;
			continue;
		}
		const entry = sources.cachedAnlzEntry(row.stable_id);
		out[row.stable_id] = entry?.status === 'loading';
	}
	return out;
}
