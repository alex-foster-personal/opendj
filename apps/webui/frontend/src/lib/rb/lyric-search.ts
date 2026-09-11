/** Debounced, order-safe lyric-only search state (Part 3 of #935, issue #1344).
 *
 * Fires only after the primary metadata search has SETTLED (the caller
 * passes `primarySettled`), and debounces on top of that
 * (`LYRIC_SEARCH_DEBOUNCE_MS`) - so primary title/artist results are never
 * delayed by the lyric fetch, and a fast typist queues at most one lyric
 * fetch per settle rather than one per keystroke. Mirrors the stale-response
 * guard BrowserPanel's whole-collection search already uses
 * (isCurrentBrowserSearch in browser-search.ts): a response is applied only
 * if nothing has superseded the query it answers by the time it lands.
 */

/** Same debounce window as the whole-collection metadata search
 * (BrowserPanel's SEARCH_DEBOUNCE_MS): it hides a network round trip, so it
 * only has to outlive typing cadence, not visibly trail the caret. */
export const LYRIC_SEARCH_DEBOUNCE_MS = 250;

export interface LyricSearchHit {
	stable_id: string;
	title: string | null;
	artist: string | null;
	/** The lyric line that best carries the query, never the full transcript
	 * or a bare title. Reuses the same field name the metadata search's
	 * match excerpt uses (SearchHitWire.match_context) - a hit is a hydrated
	 * row plus an excerpt of what matched, whatever the source of the match. */
	match_context: string;
}

export interface LyricSearchFetchResult {
	items: LyricSearchHit[];
	total: number;
}

export type LyricSearchFetcher = (query: string) => Promise<LyricSearchFetchResult>;

export interface LyricSearchState {
	query: string;
	items: LyricSearchHit[];
	total: number;
}

/** The honest "nothing to show" state: no divider renders for this, see
 * LyricSearchResults.svelte. */
export const EMPTY_LYRIC_SEARCH_STATE: LyricSearchState = { query: '', items: [], total: 0 };

export interface LyricSearchController {
	/** Call whenever the query, primary-search-settled flag, or pane
	 * active-ness changes. Schedules a debounced fetch only when ALL of:
	 * lyric search is active for this pane, the query is non-empty, and the
	 * primary metadata search has settled - otherwise clears to the empty
	 * state immediately. */
	update(query: string, primarySettled: boolean, active: boolean): void;
	/** Drop any pending timer and ignore any in-flight response - the owner
	 * (pane/component) is gone. */
	cancel(): void;
}

/**
 * `onChange` receives the settled state (or the empty state) synchronously
 * from `update` when nothing is fetched, and asynchronously once a debounced
 * fetch resolves. `onError` is called instead of `onChange` for a failed
 * fetch that nothing has superseded yet - this is a best-effort secondary
 * result, so a failure clears to empty rather than crashing the primary
 * search UI, but it is surfaced rather than swallowed.
 */
export function createLyricSearchController(
	fetcher: LyricSearchFetcher,
	onChange: (state: LyricSearchState) => void,
	onError: (message: string) => void,
	delayMs: number = LYRIC_SEARCH_DEBOUNCE_MS
): LyricSearchController {
	let timer: ReturnType<typeof setTimeout> | null = null;
	// Bumped on every update()/cancel() so a fetch that resolves after being
	// superseded (a newer query, or the pane going inactive) is detected and
	// dropped rather than rendered.
	let revision = 0;

	function _clearTimer(): void {
		if (timer !== null) {
			clearTimeout(timer);
			timer = null;
		}
	}

	async function _fire(query: string, atRevision: number): Promise<void> {
		let result: LyricSearchFetchResult;
		try {
			result = await fetcher(query);
		} catch (exc) {
			if (atRevision === revision) onError(`lyric search failed: ${String(exc)}`);
			return;
		}
		if (atRevision !== revision) return;
		onChange({ query, items: result.items, total: result.total });
	}

	return {
		update(query: string, primarySettled: boolean, active: boolean): void {
			_clearTimer();
			revision += 1;
			const trimmed = query.trim();
			if (!active || trimmed === '' || !primarySettled) {
				onChange(EMPTY_LYRIC_SEARCH_STATE);
				return;
			}
			const atRevision = revision;
			timer = setTimeout(() => void _fire(trimmed, atRevision), delayMs);
		},
		cancel(): void {
			_clearTimer();
			revision += 1;
		}
	};
}
