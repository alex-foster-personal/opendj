/**
 * Browser pane contract - the typed read/write surface for the 4-pane
 * track browser (browser-surface unit, GATING WAVE).
 *
 * Lane members (multi-pane, virtualization, fts-search, column-view)
 * build against THIS module instead of hand-editing BrowserPanel.svelte.
 *
 * Surface:
 * - BrowserRow / SortKey / SortDir: the row + sort vocabulary (moved
 *   here from TrackTable's module script; TrackTable re-exports them
 *   for existing importers).
 * - PaneStore (+ createPaneStore): per-pane reactive state - playlist
 *   selection, load lifecycle with stale-response guard, search, sort,
 *   row selection, scroll cursor. One instance per pane; all 4 panes
 *   own independent stores.
 * - RowProvider (+ makeClientRowProvider): the read interface
 *   TrackTable consumes - { rows, total, truncated, fetchWindow }. The
 *   client provider materializes the full result set in memory (no more
 *   500-row fetch cap - BrowserPanel walks every cursor page); TrackTable
 *   DOM-virtualizes the render on top of it (browser/virtual-window.ts).
 * - filterRows / sortRows / sortValue: the pure client search + sort
 *   pipeline (extracted verbatim from BrowserPanel; the fts-search
 *   lane replaces filterRows' call site, not BrowserPanel internals).
 *
 * Rune module - the .svelte.ts extension is REQUIRED for $state.
 * Only $state is used (no $derived/$effect) so the node:test harness
 * (tests/unit/load-typescript.mjs) can load this module directly.
 */

import type { PreviewStripData } from '$lib/rb/api-rb';
import type { RbMeta } from '$lib/rb/types';

// ------------------------------------------------------------ row types

/** Sortable column keys (client-side sort - ordering is not server-provided). */
export type SortKey =
	| 'order'
	| 'title'
	| 'artist'
	| 'key'
	| 'bpm'
	| 'rating'
	| 'comments'
	| 'time'
	| 'genre';

/** Sort direction: 1 = ascending, -1 = descending. */
export type SortDir = 1 | -1;

/** One browser table row, hydrated INLINE from the listing payloads
 * (shared contract points 1 + 4). Owned by the browser unit; lives here
 * (not types.ts, which is a frozen contract between the original build
 * units). */
export interface BrowserRow {
	stable_id: string;
	/** 1-based membership position within the pane playlist (# column). */
	order: number;
	title: string | null;
	artist: string | null;
	key: string | null;
	bpm: number | null;
	rating: number | null;
	/** '' for All Tracks rows (listing carries no ETag) - rating edits
	 * lazily fetch one. Playlist rows carry it inline (contract 4). */
	etag: string;
	comments: string | null;
	duration_ms: number | null;
	/** Inline genre (playlist rows only, contract 4); null = not
	 * provided inline -> fall back to lazily fetched rb_meta. */
	genre: string | null;
	/** Disk truth from the bulk server-side stat pass (contract 1/4). */
	file_exists: boolean;
	/** Inline streaming flag (playlist rows only, contract 4); null =
	 * not provided inline -> fall back to rb_meta. */
	is_streaming: boolean | null;
	/** Decoded 120-col preview strip; null = no ANLZ preview (real
	 * state, renders the explicit dash). */
	strip: PreviewStripData | null;
	/** Lazy rb-meta (artwork_available + genre/streaming fallback);
	 * null until the row first scrolls into view. */
	rb_meta: RbMeta | null;
	/** Flipped true by the IntersectionObserver on first visibility -
	 * gates the one-time canvas draw (SPIKE-A2). */
	revealed: boolean;
	/** FTS match excerpt, populated only by whole-collection search. */
	match_context: string | null;
}

// ---------------------------------------------------------- row provider

/**
 * Read interface TrackTable consumes. v1: makeClientRowProvider wraps
 * the fully materialized filtered+sorted rows (rows === full window,
 * total === rows.length). The virtualization lane implements a windowed
 * provider (rows = current window, total = full result-set size,
 * fetchWindow = async page fetch) behind this same interface.
 */
export interface RowProvider {
	/** Rows materialized for rendering, already filtered + sorted. */
	readonly rows: BrowserRow[];
	/** Total rows in the result set this provider represents. For the
	 * v1 client provider this equals rows.length; a windowed provider
	 * reports the full server-side count. */
	readonly total: number;
	/** True when the SOURCE was capped short of the real result set (e.g.
	 * a safety ceiling on a pagination loop, see fetchAllPages) - drives
	 * the explicit truncation note. The client provider walks every page
	 * (browser-panel.svelte's _fetchAllRows/_fetchPlaylistRows), so this
	 * is false in the normal case. */
	readonly truncated: boolean;
	/** Resolve rows[start .. start+count). v1 slices the in-memory
	 * array; windowed providers fetch. Fail-fast on negative args. */
	fetchWindow(start: number, count: number): Promise<BrowserRow[]>;
}

/** v1 provider: everything already in memory. Getter-based so reads
 * inside TrackTable's template track the underlying $state/$derived
 * sources reactively. */
export function makeClientRowProvider(
	rowsOf: () => BrowserRow[],
	truncatedOf: () => boolean
): RowProvider {
	return {
		get rows(): BrowserRow[] {
			return rowsOf();
		},
		get total(): number {
			return rowsOf().length;
		},
		get truncated(): boolean {
			return truncatedOf();
		},
		fetchWindow(start: number, count: number): Promise<BrowserRow[]> {
			if (start < 0 || count < 0) {
				throw new Error(`fetchWindow: negative window (start=${start}, count=${count})`);
			}
			return Promise.resolve(rowsOf().slice(start, start + count));
		}
	};
}

// ------------------------------------------------------------ pane store

/**
 * Reactive per-pane state. All 4 browser panes own an independent
 * PaneStore, so pane 2-4 selection/search/sort/scroll survives tab
 * switches. Loads go through beginLoad/completeLoad/failLoad, which
 * carry the stale-response guard (rapid re-selection: only the newest
 * load may publish rows, clear loading, or set an error).
 */
export class PaneStore {
	/** Selected playlist id ('all' for All Tracks); null = blank pane. */
	playlist_id = $state<string | null>(null);
	/** Pane tab title (playlist name; 'blank list' when empty). */
	title = $state('blank list');
	/** Loaded rows in membership order (pre filter/sort). */
	rows = $state<BrowserRow[]>([]);
	loading = $state(false);
	error = $state<string | null>(null);
	/** Client search query (composes AFTER the FR-1 hide-broken filter). */
	search = $state('');
	/** stable_id of the selected row; null = no selection. */
	selected_id = $state<string | null>(null);
	/** Ordered multi-selection used by the edit-suite batch actions. */
	selected_ids = $state<string[]>([]);
	sort_key = $state<SortKey | null>(null);
	sort_dir = $state<SortDir>(1);
	/** True when the source fetch hit the row cap (truncation note). */
	truncated = $state(false);
	/** Scroll cursor: table-wrap scrollTop, restored on pane activation. */
	scroll_top = $state(0);
	/** Whole-collection FTS5 search state, independent for every pane. */
	whole_collection = $state(false);
	search_results = $state<BrowserRow[]>([]);
	searching = $state(false);
	search_total = $state(0);
	/** Playlist-level ETag from the load's GET (add-remove-reorder-tracks
	 * node) - '' for the All Tracks / blank pane, which have no single
	 * playlist row to CAS against. Required If-Match for the next mutation. */
	etag = $state('');

	/** Monotonic load token - deliberately NOT reactive. */
	#load_seq = 0;

	/** Start a playlist load: reset the pane to its loading state and
	 * return the token the eventual completeLoad/failLoad must present. */
	beginLoad(playlist_id: string, title: string): number {
		this.#load_seq += 1;
		this.playlist_id = playlist_id;
		this.title = title;
		this.rows = [];
		this.loading = true;
		this.error = null;
		this.selected_id = null;
		this.selected_ids = [];
		this.truncated = false;
		this.scroll_top = 0;
		this.etag = '';
		return this.#load_seq;
	}

	/** True while `seq` is still the newest load on this pane. */
	isCurrentLoad(seq: number): boolean {
		return this.#load_seq === seq;
	}

	/** Publish rows for load `seq`. Stale tokens are a full no-op
	 * (the newer load owns the pane) - returns whether it applied.
	 * `etag` defaults to '' (All Tracks / blank pane loads omit it). */
	completeLoad(seq: number, rows: BrowserRow[], truncated: boolean, etag = ''): boolean {
		if (!this.isCurrentLoad(seq)) return false;
		this.rows = rows;
		this.truncated = truncated;
		this.loading = false;
		this.etag = etag;
		return true;
	}

	/** Record a load failure for `seq`; stale tokens are a full no-op. */
	failLoad(seq: number, error: string): boolean {
		if (!this.isCurrentLoad(seq)) return false;
		this.error = error;
		this.loading = false;
		return true;
	}

	/** Header click cycle: new key asc → desc → clear (natural order). */
	toggleSort(key: SortKey): void {
		if (this.sort_key !== key) {
			this.sort_key = key;
			this.sort_dir = 1;
		} else if (this.sort_dir === 1) {
			this.sort_dir = -1;
		} else {
			this.sort_key = null;
			this.sort_dir = 1;
		}
	}

	select(stable_id: string, extend: boolean): void {
		this.selected_id = stable_id;
		if (extend) {
			this.selected_ids = this.selected_ids.includes(stable_id)
				? this.selected_ids.filter((id) => id !== stable_id)
				: [...this.selected_ids, stable_id];
		} else {
			this.selected_ids = [stable_id];
		}
	}

	setSearch(next: string): void {
		this.search = next;
	}

	setWholeCollection(next: boolean): void {
		this.whole_collection = next;
		if (!next) {
			this.search_results = [];
			this.search_total = 0;
			this.searching = false;
		}
	}

	rememberScroll(top: number): void {
		this.scroll_top = top;
	}
}

export function createPaneStore(): PaneStore {
	return new PaneStore();
}

/** A membership replacement must be built from the complete playlist. The
 * browser deliberately caps rendered rows at 500, so a truncated pane must
 * remain read-only until a full-membership write path exists. */
export function canMutatePlaylist(
	pane: Pick<PaneStore, 'playlist_id' | 'etag' | 'truncated' | 'whole_collection'>
): boolean {
	return pane.playlist_id !== null
		&& pane.playlist_id !== 'all'
		&& pane.etag !== ''
		&& !pane.truncated
		&& !pane.whole_collection;
}

// -------------------------------------------- client search + sort pipeline

/** FR-1 hide-broken filter THEN case-insensitive substring search over
 * title/artist/comments/key/genre (genre falls back to lazy rb_meta).
 *
 * Genre demos (search box / chip clicks):
 *   `genre:House`  - strict: a comma-split genre token equals the tag
 *   `genre:~House` - loose: genre field contains the tag as a substring
 * Plain queries still match across title/artist/comments/key/genre. */
export function filterRows(rows: BrowserRow[], query: string, hideBroken: boolean): BrowserRow[] {
	// FR-1: hide-broken applies before search so both compose.
	const base = hideBroken ? rows.filter((r) => r.file_exists) : rows;
	const raw = query.trim();
	if (raw === '') return base;

	const genreStrict = /^genre:(?!~)(.+)$/i.exec(raw);
	if (genreStrict !== null) {
		const tag = genreStrict[1].trim().toLowerCase();
		if (tag === '') return base;
		return base.filter((r) => _genreTokens(r).some((t) => t === tag));
	}
	const genreLoose = /^genre:~(.+)$/i.exec(raw);
	if (genreLoose !== null) {
		const tag = genreLoose[1].trim().toLowerCase();
		if (tag === '') return base;
		return base.filter((r) => {
			const g = (r.genre ?? r.rb_meta?.genre ?? '').toLowerCase();
			return g.includes(tag);
		});
	}

	const q = raw.toLowerCase();
	return base.filter((r) =>
		[r.title, r.artist, r.comments, r.key, r.genre ?? r.rb_meta?.genre ?? null].some(
			(field) => field !== null && field.toLowerCase().includes(q)
		)
	);
}

function _genreTokens(row: BrowserRow): string[] {
	const raw = row.genre ?? row.rb_meta?.genre ?? '';
	if (raw.trim() === '') return [];
	return raw
		.split(',')
		.map((t) => t.trim().toLowerCase())
		.filter((t) => t !== '');
}

/** Comparable cell value for a sort key (null = missing, sorts last). */
export function sortValue(row: BrowserRow, key: SortKey): string | number | null {
	if (key === 'order') return row.order;
	else if (key === 'title') return row.title;
	else if (key === 'artist') return row.artist;
	else if (key === 'key') return row.key;
	else if (key === 'bpm') return row.bpm;
	else if (key === 'rating') return row.rating;
	else if (key === 'comments') return row.comments;
	else if (key === 'time') return row.duration_ms;
	else return row.genre ?? row.rb_meta?.genre ?? null;
}

/** Stable client sort; nulls last regardless of direction. Returns a
 * new array (never mutates the pane's membership-ordered rows). */
export function sortRows(rows: BrowserRow[], key: SortKey | null, dir: SortDir): BrowserRow[] {
	if (key === null) return rows;
	return rows.slice().sort((a, b) => {
		const av = sortValue(a, key);
		const bv = sortValue(b, key);
		if (av === null && bv === null) return 0;
		else if (av === null) return 1; // nulls always last
		else if (bv === null) return -1;
		const base =
			typeof av === 'string' && typeof bv === 'string'
				? av.localeCompare(bv)
				: (av as number) - (bv as number);
		return base * dir;
	});
}

/** The full read pipeline a pane renders: FR-1 filter -> search -> sort. */
export function visibleRowsOf(pane: PaneStore, hideBroken: boolean): BrowserRow[] {
	return sortRows(filterRows(pane.rows, pane.search, hideBroken), pane.sort_key, pane.sort_dir);
}

// ---------------------------------------------------------------- pane tabs
// RECOVERED, not designed. `BrowserPanel.svelte` has imported both of these
// since cfbfe55 ("wip(spike)", Fri 24 Jul 2026 01:19) but they were never
// committed anywhere -- not in the branch, not in the Cursor backup snapshot
// 718cc81 -- so /performance could not build for anyone. Reconstructed from
// the two call sites; semantics are inferred, so treat as provisional and
// replace if the original turns up.

/**
 * Move a pane tab and return where the ACTIVE pane ended up.
 *
 * Mutates ``panes`` in place because the caller holds the same array
 * reference (hence "InPlace" in the name); returns the new active index
 * rather than mutating it, since the caller owns that state.
 */
export function reorderPanesInPlace<T>(
	panes: T[],
	from: number,
	to: number,
	activePane: number
): number {
	if (
		from === to ||
		from < 0 ||
		to < 0 ||
		from >= panes.length ||
		to >= panes.length
	) {
		return activePane;
	}
	const [moved] = panes.splice(from, 1);
	panes.splice(to, 0, moved);
	// Follow the dragged tab if it was the active one; otherwise shift only
	// when the move crossed the active index.
	if (activePane === from) return to;
	if (from < activePane && to >= activePane) return activePane - 1;
	if (from > activePane && to <= activePane) return activePane + 1;
	return activePane;
}

/**
 * Index of the tab a newly opened playlist should take, or null if none is free.
 *
 * Null means every tab is sticky (locked); the caller surfaces that as an
 * explicit toast rather than silently stealing a locked tab.
 */
export function resolveNewTabIndex(panes: { sticky?: boolean }[]): number | null {
	const free = panes.findIndex((p) => p.sticky !== true);
	return free === -1 ? null : free;
}
