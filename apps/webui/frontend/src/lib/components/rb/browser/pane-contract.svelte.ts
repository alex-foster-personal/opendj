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

import type { PreviewStripData, StemSummary, Vocals } from '$lib/rb/api-rb';
import { matchesSearchQuery } from '$lib/rb/browser-search-query';
import type { RbMeta, TrackQuality } from '$lib/rb/library-types';

// ------------------------------------------------------------ row types

/** Sortable column keys (client-side sort - ordering is not server-provided). */
export type SortKey =
	| 'order'
	| 'plays'
	| 'title'
	| 'artist'
	| 'key'
	| 'bpm'
	| 'rating'
	| 'comments'
	| 'time'
	| 'energy'
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
	/** MIK energy on the display's explicit 1-9 scale. */
	energy: number | null;
	/** `mik` only. A future computed value must not masquerade as MIK. */
	energy_source: 'mik' | null;
	/** Honest reason shown when energy is null, never a fallback value. */
	energy_reason: string;
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
	/** Venue-rung quality, inline on every row from the SAME stat pass.
	 * null only for synthesized rows that never came off the wire. */
	quality: TrackQuality | null;
	/** Rekordbox DJPlayCount (0 when unknown / non-RB). */
	play_count: number;
	/** Inline streaming flag (playlist rows only, contract 4); null =
	 * not provided inline -> fall back to rb_meta. */
	is_streaming: boolean | null;
	/** Spotify-unmatched placeholder (light green row). True when the
	 * row is a synthetic spotify-pending track or wire spotify_pending. */
	spotify_pending?: boolean;
	/** Decoded 120-col preview strip; null = no ANLZ preview (real
	 * state, renders the explicit dash). */
	strip: PreviewStripData | null;
	/** Inline vocals from listing hydrate (PVDI or demucs cache); drives
	 * PreviewStrip blue bars without a per-row /anlz fetch. */
	vocals: Vocals;
	/** Inline demucs stem summary (V/I/D); null only for synthetic rows. */
	stems: StemSummary | null;
	/** Whether this track has a live rekordbox mapping (inline on every
	 * row, contract 1/4). False is a real library state - locally imported
	 * or djay-only - and gates the lazy fetch below off entirely: since
	 * #505 rb-meta would answer 200, but with nothing the row does not
	 * already carry, so rb_meta stays null and no request is made. */
	has_rb_mapping: boolean;
	/** Inline listing verdict. The artwork cell must not rely on visibility
	 * hydration, which can be absent for a virtualized row. */
	artwork_available: boolean | null;
	artwork_status: 'ok' | 'no_image_path' | 'unresolved' | 'file_missing';
	/** Lazy rb-meta (genre/streaming and analysis fallback);
	 * null until the row first scrolls into view, and permanently null when
	 * has_rb_mapping is false. */
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
	/**
	 * Locked tab. Selecting a playlist while this pane is sticky opens it in
	 * another pane rather than replacing what is loaded here. Pure client
	 * pane state like scroll_top, and deliberately NOT reset by beginLoad -
	 * the lock belongs to the tab, not to whatever it currently holds.
	 */
	sticky = $state(false);
	/** Whole-collection FTS5 search state, independent for every pane. */
	whole_collection = $state(false);
	search_results = $state<BrowserRow[]>([]);
	searching = $state(false);
	search_total = $state(0);
	/** Playlist-level ETag from the load's GET (add-remove-reorder-tracks
	 * node) - '' for the All Tracks / blank pane, which have no single
	 * playlist row to CAS against. Required If-Match for the next mutation. */
	etag = $state('');
	/** Page-granular load progress for the LibraryLoadIndicator (ad59ac).
	 * null when there is no load in flight, or once one has settled -
	 * a terminal state has nothing left to show progress toward.
	 * `total` is null until a track_count / health figure is known; the
	 * indicator must never fabricate a percentage in that case. Updated
	 * ONLY via updateLoadProgress under the current load's token, so a
	 * superseded page fetch can never paint over the pane that replaced it. */
	load_progress = $state<{ loaded: number; total: number | null } | null>(null);

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
		this.load_progress = null;
		return this.#load_seq;
	}

	/** True while `seq` is still the newest load on this pane. */
	isCurrentLoad(seq: number): boolean {
		return this.#load_seq === seq;
	}

	/** Publish one page's worth of load progress for `seq` (ad59ac). Stale
	 * tokens are a full no-op, same guard as completeLoad/failLoad - a
	 * superseded pane's late-arriving page must never paint over the pane
	 * that replaced it. `total` is null until a total is known; callers must
	 * not invent one. */
	updateLoadProgress(seq: number, loaded: number, total: number | null): boolean {
		if (!this.isCurrentLoad(seq)) return false;
		this.load_progress = { loaded, total };
		return true;
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
		this.load_progress = null;
		return true;
	}

	/** Record a load failure for `seq`; stale tokens are a full no-op. */
	failLoad(seq: number, error: string): boolean {
		if (!this.isCurrentLoad(seq)) return false;
		this.error = error;
		this.loading = false;
		this.load_progress = null;
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

	/**
	 * Row click selection.
	 *
	 * `extend` (cmd/ctrl-click) toggles one row in or out of the selection.
	 * `range` (shift-click) selects the contiguous span between the current
	 * row and the clicked one, which requires the caller to pass the ids in
	 * the order the user actually sees them (post filter and sort) -
	 * membership order would select a different span than the one on screen.
	 *
	 * The anchor is the previously selected row, and the clicked row becomes
	 * the new one, so successive shift-clicks grow or shrink from the last
	 * click. If either end is absent from `ordered_ids` there is no span the
	 * user could have meant, so this falls back to a plain single select
	 * rather than guessing one.
	 */
	select(
		stable_id: string,
		extend: boolean,
		range = false,
		ordered_ids: readonly string[] = []
	): void {
		if (range) {
			const anchor = this.selected_id;
			const from = anchor === null ? -1 : ordered_ids.indexOf(anchor);
			const to = ordered_ids.indexOf(stable_id);
			if (from !== -1 && to !== -1) {
				const lo = Math.min(from, to);
				const hi = Math.max(from, to);
				this.selected_ids = ordered_ids.slice(lo, hi + 1);
				this.selected_id = stable_id;
				return;
			}
		}
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

// ------------------------------------------------------- boot pane selection

/**
 * The remembered identity of a pane's playlist, small enough to persist.
 *
 * Deliberately NOT a whole PlaylistNode: track_count and children go stale
 * between sessions, and restoring a stale count would put a wrong number on
 * screen before the real load lands. Only the identity survives; everything
 * else is re-fetched.
 */
export interface BootPlaylistChoice {
	playlist_id: string;
	name: string;
	kind: 'all_tracks' | 'playlist';
}

/** The All Tracks identity. Synthesized client-side; the API never returns it. */
export const ALL_TRACKS_CHOICE: BootPlaylistChoice = {
	playlist_id: 'all',
	name: 'All Tracks',
	kind: 'all_tracks'
};

/**
 * What the first pane should show at boot.
 *
 * The pane used to boot at playlist_id=null, so every launch of /performance
 * opened on an empty track table until a human clicked a playlist. That blank
 * pane was indistinguishable from a broken load, which is the honest-state
 * failure this resolves.
 *
 * Rules, in order:
 * - Empty library -> null. There is nothing truthful to show, and defaulting
 *   to All Tracks would render an empty table that looks like a failed load.
 *   The caller keeps its explicit empty state.
 * - A remembered playlist that still exists -> that playlist.
 * - A remembered playlist that is gone (deleted or filtered out of the tree
 *   since last session) -> All Tracks, never a dangling id that would load
 *   into an error.
 * - Nothing remembered -> All Tracks.
 *
 * Pure so the decision is unit-testable without a DOM or a live library.
 */
export function resolveBootPlaylist(args: {
	remembered: BootPlaylistChoice | null;
	known_playlist_ids: readonly string[];
	all_tracks_count: number;
}): BootPlaylistChoice | null {
	if (!Number.isFinite(args.all_tracks_count) || args.all_tracks_count <= 0) return null;
	const remembered = args.remembered;
	if (remembered === null) return ALL_TRACKS_CHOICE;
	if (remembered.kind === 'all_tracks') return ALL_TRACKS_CHOICE;
	if (args.known_playlist_ids.includes(remembered.playlist_id)) return remembered;
	return ALL_TRACKS_CHOICE;
}

// -------------------------------------------- client search + sort pipeline

/** FR-1 hide-broken filter THEN the search grammar (browser-search-query.ts,
 * pin 7ca47b21ead7 / issue #936): case-insensitive substring search over
 * title/artist/comments/key/genre (genre falls back to lazy rb_meta),
 * layered with `field:operator:value` predicates over bpm, rating and key,
 * plus the pre-existing `genre:` / `genre:~` tag filters. See that module's
 * doc comment for the full grammar; this function only adapts a BrowserRow
 * into the pure module's SearchableTrack shape (resolving the rb_meta genre
 * fallback here, since that fallback is BrowserRow-specific).
 *
 * Streaming / Spotify-pending rows (`is_streaming`) stay visible under
 * hide-broken: they are intentional unmatched placeholders, not broken links. */
export function filterRows(rows: BrowserRow[], query: string, hideBroken: boolean): BrowserRow[] {
	// FR-1: hide-broken applies before search so both compose.
	const base = hideBroken
		? rows.filter((r) => r.file_exists || r.is_streaming === true || r.spotify_pending === true)
		: rows;
	if (query.trim() === '') return base;
	return base.filter((r) =>
		matchesSearchQuery(
			{
				title: r.title,
				artist: r.artist,
				comments: r.comments,
				key: r.key,
				genre: r.genre ?? r.rb_meta?.genre ?? null,
				bpm: r.bpm,
				rating: r.rating
			},
			query
		)
	);
}

/** Comparable cell value for a sort key (null = missing, sorts last). */
export function sortValue(row: BrowserRow, key: SortKey): string | number | null {
	if (key === 'order') return row.order;
	else if (key === 'plays') return row.play_count;
	else if (key === 'title') return row.title;
	else if (key === 'artist') return row.artist;
	else if (key === 'key') return row.key;
	else if (key === 'bpm') return row.bpm;
	else if (key === 'rating') return row.rating;
	else if (key === 'comments') return row.comments;
	else if (key === 'time') return row.duration_ms;
	else if (key === 'energy') return row.energy;
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

/** Writes a decoded strip into every matching row in every pane, ALWAYS overwriting - the audience-ambiguous sidecar (TECH-DEBT.md) never outranks /anlz's own audience-scoped decode. */
export function applyDecodedStripAcrossPanes(
	panes: readonly PaneStore[],
	stable_id: string,
	strip: PreviewStripData | null
): void {
	for (const pane of panes) {
		for (const row of pane.rows) {
			if (row.stable_id === stable_id) row.strip = strip;
		}
	}
}

// ---------------------------------------------------------------- pane tabs
// Tab reordering + new-tab placement moved to ./pane-tabs (plain array/index
// math, a distinct concern from the reactive PaneStore contract above).
// Re-exported here so BrowserPanel.svelte and the existing tests keep one
// import site for the pane vocabulary.
export { reorderPanesInPlace, resolveNewTabIndex } from './pane-tabs';

// ---------------------------------------------- playlist deck-context tints
// Pin 2ac3a0: the tint derivations + the tree's CURRENT fold control moved to
// ./playlist-context (a distinct concern from the pane store itself - pure
// derivations over already-hydrated pane state). Re-exported here so existing
// importers (BrowserPanel.svelte, PlaylistTree.svelte, tests) keep one import
// site for the pane vocabulary, matching the playlist-drag re-export below.
export {
	type PlaylistTint,
	playlistTintOf,
	derivePlaylistDeckMembership,
	derivePlaylistPaneOpenCounts,
	multiPanePlaylistIds,
	computeTreeCurrentFold
} from './playlist-context';

// -------------------------------------------------- playlist drag payload
// The codec itself lives in ./playlist-drag (pure, runeless, importless).
// The payload type stays exported here so BrowserPanel keeps ONE import
// site for the pane vocabulary, matching TrackTable's row re-exports.
export type { PlaylistDragPayload } from './playlist-drag';
