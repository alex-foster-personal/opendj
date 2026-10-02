/**
 * Library browse instrumentation + the local filter debounce (PERF-R5 Q9).
 *
 * Two problems this module exists for.
 *
 * 1. THE PER-KEYSTROKE FILTER. BrowserPanel's default Cmd+F mode wrote every
 *    keystroke straight into `pane.search`, and `pane.search` feeds a $derived
 *    that runs `filterRows` + `sortRows` over the whole `pane.rows` array -
 *    up to ~8k rows on All Tracks, plus an O(n log n) sort whenever a column
 *    sort is active. Typing a 10-character query was 10 full passes over the
 *    collection. `createFilterDebounce` collapses a burst into one recompute.
 *    The whole-collection FTS path already debounced at 250 ms; this one is
 *    deliberately shorter because it is local compute, not a round trip.
 *
 * 2. NO NUMBERS. Nothing on the library side was timed - not the All Tracks
 *    cursor walk, not the filter recompute, not the FTS query, not the
 *    row-select prefetches. "The browser feels slow" had no measurement behind
 *    it. The recorders below emit into the existing perf ring
 *    (`$lib/rb/perf-event-log`), through its PUBLIC api only.
 *
 * EMISSION RATE is the whole design constraint: the ring holds 40 rows, so a
 * chatty emitter destroys the log it writes to. One row per completed
 * interaction (settled filter, finished load, finished search); prefetches are
 * sampled 1 in {@link PREFETCH_SAMPLE_EVERY} because a scroll-and-select sweep
 * fires them in bursts.
 *
 * Ring row kinds owned here:
 *   library-filter              - one settled local filter interaction
 *   library-load-all-tracks     - one completed All Tracks cursor walk
 *   library-load-playlist       - one completed playlist hydrate
 *   library-playlist-tree-ready - playlist tree first paint after boot
 *   library-switch-first-rows   - click to first tbody row on switch
 *   library-search-collection   - one completed whole-collection FTS query
 *   library-prefetch-anlz       - sampled row-select /anlz warm
 *   library-prefetch-audio      - sampled row-select audio ArrayBuffer warm
 */
import { recordPerfTiming } from '$lib/rb/perf-event-log';
import { completeLibraryUsable } from '$lib/client-telemetry';

// ------------------------------------------------------- filter debounce

/**
 * How long a keystroke burst has to settle before the pane refilters.
 *
 * Shorter than the 250 ms whole-collection debounce on purpose: that one hides
 * a network round trip, this one only has to outlive the gap between two
 * keypresses. Typing cadence is roughly 100-200 ms per character, so 80 ms
 * coalesces a fast burst without the filter visibly trailing the caret.
 */
export const LOCAL_FILTER_DEBOUNCE_MS = 80;

/** What one coalesced keystroke burst resolved to. */
export interface FilterSettle {
	/** The query as of the LAST keystroke in the burst. */
	query: string;
	/** Keystrokes folded into this single recompute (always >= 1). */
	keystrokes: number;
	/** First keystroke of the burst to this settle, in ms. */
	coalescedMs: number;
}

export interface FilterDebounce {
	/** Record one keystroke and (re)arm the settle. */
	push(query: string): void;
	/** Drop a pending settle - a programmatic write superseded the burst. */
	cancel(): void;
	/** Settle now if one is pending (blur, Enter, forced apply). */
	flush(): void;
	/** True while a settle is armed. */
	readonly pending: boolean;
}

/**
 * Coalesce a keystroke burst into a single settle.
 *
 * Trailing-edge only: the point is that the intermediate queries are never
 * filtered on, so there is no leading call. `now` is injected so the coalesced
 * span is measurable under mocked timers.
 */
export function createFilterDebounce(
	onSettle: (settle: FilterSettle) => void,
	delayMs: number = LOCAL_FILTER_DEBOUNCE_MS,
	now: () => number = () => Date.now()
): FilterDebounce {
	let timer: ReturnType<typeof setTimeout> | null = null;
	let keystrokes = 0;
	let firstKeyAt = 0;
	let latest = '';

	function _settle(): void {
		timer = null;
		if (keystrokes === 0) return;
		const burst: FilterSettle = {
			query: latest,
			keystrokes,
			coalescedMs: Math.max(0, Math.round(now() - firstKeyAt))
		};
		// Cleared BEFORE the callback so a keystroke arriving during it opens a
		// new interaction rather than inflating the one just reported.
		keystrokes = 0;
		onSettle(burst);
	}

	return {
		get pending(): boolean {
			return timer !== null;
		},
		push(query: string): void {
			if (keystrokes === 0) firstKeyAt = now();
			keystrokes += 1;
			latest = query;
			if (timer !== null) clearTimeout(timer);
			timer = setTimeout(_settle, delayMs);
		},
		cancel(): void {
			if (timer !== null) {
				clearTimeout(timer);
				timer = null;
			}
			keystrokes = 0;
		},
		flush(): void {
			if (timer === null) return;
			clearTimeout(timer);
			_settle();
		}
	};
}

// ------------------------------------------------------------- recorders

/**
 * One settled local filter interaction.
 *
 * `keystrokes` is what makes the row readable: without it a 6-keypress burst
 * and a single keypress look identical, and the whole point of the debounce is
 * that they cost the same.
 */
export function recordFilterTiming(args: {
	keystrokes: number;
	coalescedMs: number;
	/** Wall time of the filterRows + sortRows pass the user waited on. */
	computeMs: number;
	/** Pane rows the pass started from (the real denominator). */
	rowsIn: number;
	rowsOut: number;
}): void {
	recordPerfTiming('library-filter', {
		keystrokes: args.keystrokes,
		coalesced_ms: Math.round(args.coalescedMs),
		compute_ms: Math.round(args.computeMs),
		rows_in: args.rowsIn,
		rows_out: args.rowsOut
	});
}

/** One completed pane load. Split by source: an 8k-row cursor walk and a
 * 35-track playlist hydrate are different animals and must stay tellable
 * apart when the ring is read back. */
export function recordLibraryLoadTiming(
	source: 'all-tracks' | 'playlist',
	args: { fetchMs: number; rows: number }
): void {
	recordPerfTiming(`library-load-${source}`, {
		fetch_ms: Math.round(args.fetchMs),
		rows: args.rows
	});
	completeLibraryUsable({ source });
}

/** Playlist tree ready after boot prefetch (PERF-UI-05). */
export function recordPlaylistTreeReadyMs(ms: number): void {
	recordPerfTiming('library-playlist-tree-ready', {
		ready_ms: Math.round(ms)
	});
}

/** Click to first visible rows on playlist or All Tracks switch (PERF-UI-05). */
export function recordPlaylistSwitchFirstRowsMs(
	source: 'playlist' | 'all-tracks',
	ms: number,
	decomposition?: { fetchMs: number; paintMs: number }
): void {
	const stages: Record<string, number> = { first_rows_ms: Math.round(ms) };
	if (decomposition !== undefined) {
		stages.fetch_ms = Math.round(decomposition.fetchMs);
		stages.paint_ms = Math.round(decomposition.paintMs);
	}
	recordPerfTiming('library-switch-first-rows', stages, null, { source });
}

/** One completed whole-collection FTS query. `total` rides along because the
 * result set is capped: hits alone hides how much actually matched. */
export function recordCollectionSearchTiming(args: {
	queryMs: number;
	hits: number;
	total: number;
}): void {
	recordPerfTiming('library-search-collection', {
		query_ms: Math.round(args.queryMs),
		hits: args.hits,
		total: args.total
	});
}

// ------------------------------------------------------ prefetch sampling

/**
 * One row-select prefetch in this many is timed.
 *
 * Row selection fires on arrow-key scroll, so an unsampled emitter would push
 * a quarter of the 40-row ring out on a single sweep down a playlist. Sampling
 * keeps the shape of the distribution while leaving room for the loads and
 * failures the ring exists for.
 */
export const PREFETCH_SAMPLE_EVERY = 5;

let _anlzSeen = 0;
let _audioSeen = 0;

/** True on the 1st, 6th, 11th ... call - the first prefetch of a session is
 * always timed, because a cold cache is the interesting one. */
function _sampled(seen: number): boolean {
	return seen % PREFETCH_SAMPLE_EVERY === 1;
}

/**
 * A finished row-select /anlz warm. Returns whether the row was emitted, so
 * the sampling contract is observable rather than implied.
 */
export function recordAnlzPrefetchSampled(fetchMs: number, outcome: 'ready' | 'error'): boolean {
	_anlzSeen += 1;
	if (!_sampled(_anlzSeen)) return false;
	recordPerfTiming('library-prefetch-anlz', {
		fetch_ms: Math.round(fetchMs),
		ok: outcome === 'ready' ? 1 : 0,
		sample_of: PREFETCH_SAMPLE_EVERY
	});
	return true;
}

/** A finished row-select audio ArrayBuffer warm. Size rides along: without it
 * a slow prefetch cannot be told apart from a big one. */
export function recordAudioPrefetchSampled(fetchMs: number, bytes: number): boolean {
	_audioSeen += 1;
	if (!_sampled(_audioSeen)) return false;
	recordPerfTiming('library-prefetch-audio', {
		fetch_ms: Math.round(fetchMs),
		mib: Math.round(bytes / (1024 * 1024)),
		sample_of: PREFETCH_SAMPLE_EVERY
	});
	return true;
}
