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

import type { BpmAlternative, BpmAlternativeSource } from '$lib/rb/bpm-dispute';
import type {
	CloudTransferWire,
	OffloadWire,
	PreviewStripData,
	StemSummary,
	Vocals
} from '$lib/rb/api-rb';
import type { FileAvailabilityStatus } from '$lib/rb/file-availability';
import { gridFlagSortValue, type GridQualityRow } from '$lib/rb/analysis-issues';
import { worksSortValue } from '$lib/rb/library-works';
import { viaWorkHits, worksView } from '$lib/rb/works-overlay.svelte';
import { matchesSearchQuery } from '$lib/rb/browser-search-query';
import { searchableTrackFromBrowserRow } from './track-table-search';
import { normalizeRating } from '$lib/rating';
import { fileNameSortValue, locationSortValue } from '$lib/rb/file-path-cells';
import { listingGenre } from '$lib/rb/genre-source';
import { sortRowsByAutoPlayOrder } from '$lib/rb/auto-play';
import type { PlaylistNode, RbMeta, TrackQuality, TrackRow } from '$lib/rb/library-types';
import type { LyricsRowSummary } from '$lib/rb/lyrics/types';
import type { TempoCurveData } from '$lib/components/rb/wave/tempo-marks';
import { rowRendersUnavailable } from './browser-row-wire';
import type { PlayFacts } from './last-played';
import { lyricsSortValue } from './lyric-column';
import { applySelect } from './pane-row-selection';
import type { SortDir, SortKey } from './browser-sort-ipc';
export { installBrowserSortIpc } from './browser-sort-ipc';
export { reorderBlockedReason } from './playlist-reorder-hint';
export type { SortDir, SortKey } from './browser-sort-ipc';

// ------------------------------------------------------------ row types

/** One browser table row, hydrated INLINE from the listing payloads
 * (shared contract points 1 + 4). Owned by the browser unit; lives here
 * (not types.ts, which is a frozen contract between the original build
 * units). */
export interface BrowserRow extends Pick<
	TrackRow,
	| 'key_status'
	| 'key_reason'
	| 'key_guess'
	| 'key_guess_confidence'
	| 'key_source'
	| 'bpm_status'
	| 'bpm_reason'
	| 'loudness_status'
	| 'loudness_reason'
> {
	stable_id: string;
	bpm_source?: string | null;
	bpm_method?: string | null;
	bpm_confidence?: number | null;
	bpm_confidence_error?: string | null;
	/** GRID-OCTAVE-02 (see api-rb.ts). */
	bpm_disputed?: boolean;
	bpm_alternatives?: BpmAlternative[];
	bpm_pick?: BpmAlternativeSource | null;
	/** v13 playlist_memberships.item_id; null outside playlist detail. */
	item_id: string | null;
	edit_id?: string | null;
	in_ms?: number | null;
	out_ms?: number | null;
	fx?: Record<string, unknown> | null;
	edit_label?: string | null;
	created_by?: string | null;
	is_edit?: boolean;
	valid?: boolean;
	/** 1-based membership position within the pane playlist (# column). */
	order: number;
	title: string | null;
	artist: string | null;
	/** Album title when the listing carries it (LIBUX-9732); null when absent. */
	album?: string | null;
	key: string | null;
	bpm: number | null;
	rating: number | null;
	/** Rekordbox ColorID 1..8 on the wire; null or absent means no colour (0). */
	color_id?: number | null;
	/** Inline listing tags for column funnels and the Tags column. */
	tags?: readonly string[];
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
	/** NOWP-11487: redact identity in tracklist exports when true. */
	tracklist_hidden?: boolean;
	duration_ms: number | null;
	/** Inline genre when the listing or playlist row carries it; null =
	 * fall back to lazily fetched rb_meta. */
	genre: string | null;
	/** Explains an empty genre cell (no file tag, no rekordbox genre, etc.). */
	genre_reason?: string | null;
	/** GENRE-04: which provider the shown genre came from (user, beatport, odj-fused, library-tag...). */
	genre_source?: string | null;
	/** LIBUX-06: canonical family slug for the shown genre, when one matches. */
	genre_family?: string | null;
	genre_family_color?: string | null;
	/** GENRE-02: a JEV genre-family guess; shown only while genre is empty. */
	genre_guess?: { family: string; confidence: number; source: 'jev' } | null;
	/** Disk truth (contract 1/4, PERF-RB-01); null ONLY while pending. */
	file_exists: boolean | null;
	/** Path the listing probed (state file_path, or rekordbox FolderPath). */
	file_path?: string | null;
	/** Typed disk-truth lane; pending rows are neither playable nor broken. */
	file_availability: FileAvailabilityStatus;
	/** Venue-rung quality, inline on every row from the SAME stat pass.
	 * null only for synthesized rows that never came off the wire. */
	quality: TrackQuality | null;
	/** Rekordbox DJPlayCount plus Open DJ plays (PLAYS-01); 0 when neither. */
	play_count: number;
	/** Per-source play dates and counts when the listing already carried them. */
	plays?: PlayFacts | null;
	/** Inline streaming flag (playlist rows only, contract 4); null =
	 * not provided inline -> fall back to rb_meta. */
	is_streaming: boolean | null;
	is_remote?: boolean;
	/** Durable cloud presence; unlike is_remote, stays true for local+cloud. */
	has_remote_copy?: boolean;
	/** Remote stem bundle index fact from the listing wire (CHROME-03). */
	remote_stem_status?: 'ready' | 'not-run' | 'unknown';
	/** Issue #11290: server verdict that deck load would fail on this host. */
	deck_load_refusal?: 'streaming' | 'cloud_policy_unconfigured' | null;
	/** Live CloudSync bytes from the same listing snapshot as this row. */
	cloud_transfer?: CloudTransferWire | null;
	/** OFFLOAD: an offloaded row stays listed and reads as in the cloud, not missing. */
	offload?: OffloadWire | null;
	/** Spotify-unmatched placeholder (light green row). True when the
	 * row is a synthetic spotify-pending track or wire spotify_pending. */
	spotify_pending?: boolean;
	/** Inline streaming provider (playlist and /tracks rows); the only source for
	 * a row with no rekordbox mapping, whose rb_meta never loads. */
	streaming_provider?: 'spotify' | 'tidal' | 'soundcloud' | 'unknown' | null | undefined;
	/** Decoded 120-col preview strip; null = no ANLZ preview (real
	 * state, renders the explicit dash). */
	strip: PreviewStripData | null;
	/** Inline vocals from listing hydrate (PVDI or demucs cache); drives
	 * PreviewStrip blue bars without a per-row /anlz fetch. */
	vocals: Vocals;
	/** Row strip tempo curve from POST /library/row-assets (issue #7879); null or
	 * absent when steady, never judged, or not fetched yet: the strip paints nothing. */
	tempo_curve?: TempoCurveData | null;
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
	/** Listing-row lyric summary; null = pipeline never ran. */
	lyrics: LyricsRowSummary | null;
	/** Stored beatgrid verdict (GRIDFLAG-02): the server's scan wrote it, the
	 * Err column draws it. Absent or null on synthetic rows and older
	 * payloads, which then show no beatgrid flag state at all. */
	grid_quality?: GridQualityRow | null;
	/** Title-marker remix heuristic (backend is_remix); null on synthetic rows. */
	is_remix: boolean | null;
	/** Radio edits are length trims, not remixes - separate tag. */
	is_radio_edit: boolean | null;
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
	/** Smartlist: loaded row count vs rule match total for N-of-M copy; null when N/A. */
	readonly matchSnapshot: { loaded: number; total: number } | null;
	/** Resolve rows[start .. start+count). v1 slices the in-memory
	 * array; windowed providers fetch. Fail-fast on negative args. */
	fetchWindow(start: number, count: number): Promise<BrowserRow[]>;
}

/** v1 provider: everything already in memory. Getter-based so reads
 * inside TrackTable's template track the underlying $state/$derived
 * sources reactively. */
export function makeClientRowProvider(
	rowsOf: () => BrowserRow[],
	truncatedOf: () => boolean,
	matchOf?: () => { loaded: number; total: number } | null
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
		get matchSnapshot(): { loaded: number; total: number } | null {
			return matchOf?.() ?? null;
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
	/** The loaded node's kind (PlaylistNode's union); null = blank pane. */
	kind = $state<PlaylistNode['kind'] | null>(null);
	/** Pane tab title (playlist name; 'blank list' when empty). */
	title = $state('blank list');
	/** Loaded rows in membership order (pre filter/sort). Raw array: each row
	 * edit replaces the whole row so ~20 fields are not deep-proxied. */
	rows = $state.raw<BrowserRow[]>([]);
	loading = $state(false);
	/** False until this pane's current load has returned or failed. This keeps
	 * consumers from mistaking the deliberate empty pre-mount state for an
	 * empty library result. */
	has_settled_result = $state(false);
	error = $state<string | null>(null);
	/** Client search query (composes AFTER the FR-1 hide-broken filter). */
	search = $state('');
	/** stable_id of the selected row; null = no selection. */
	selected_id = $state<string | null>(null);
	/** Ordered multi-selection used by the edit-suite batch actions. */
	selected_ids = $state<string[]>([]);
	/** 1-based membership slot of the anchor row (issue #2075). */
	selected_order = $state<number | null>(null);
	/** Positional multi-selection for row highlight (unique per pane). */
	selected_orders = $state<number[]>([]);
	sort_key = $state<SortKey | null>(null);
	sort_dir = $state<SortDir>(1);
	/** True when the source fetch hit the row cap (truncation note). */
	truncated = $state(false);
	/** Smartlist rule match total from GET .../tracks when known; null otherwise. */
	match_total = $state<number | null>(null);
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
	search_results = $state.raw<BrowserRow[]>([]);
	searching = $state(false);
	search_total = $state(0);
	search_result_query = $state('');
	/** Whole-collection search failure detail; null when unsettled or succeeded. */
	search_error = $state<string | null>(null);
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
	beginLoad(
		playlist_id: string,
		title: string,
		kind: PlaylistNode['kind'] = playlist_id === 'all' ? 'all_tracks' : 'playlist'
	): number {
		this.#load_seq += 1;
		this.playlist_id = playlist_id;
		this.kind = kind;
		this.title = title;
		this.rows = [];
		this.loading = true;
		this.has_settled_result = false;
		this.error = null;
		this.selected_id = null;
		this.selected_ids = [];
		this.selected_order = null;
		this.selected_orders = [];
		this.truncated = false;
		this.match_total = null;
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
	completeLoad(
		seq: number,
		rows: BrowserRow[],
		truncated: boolean,
		etag = '',
		matchTotal: number | null = null
	): boolean {
		if (!this.isCurrentLoad(seq)) return false;
		this.rows = rows;
		this.truncated = truncated;
		this.match_total = matchTotal;
		this.loading = false;
		this.has_settled_result = true;
		this.etag = etag;
		this.load_progress = null;
		return true;
	}

	/** Record a load failure for `seq`; stale tokens are a full no-op. */
	failLoad(seq: number, error: string): boolean {
		if (!this.isCurrentLoad(seq)) return false;
		this.error = error;
		this.loading = false;
		this.has_settled_result = true;
		this.load_progress = null;
		return true;
	}

	/** Header click cycle: ordinary keys asc → desc → clear; AutoPlay asc → clear. */
	/** Back to membership order (W33 "Restore playlist order"). */
	clearSort(): void {
		this.sort_key = null;
		this.sort_dir = 1;
	}

	toggleSort(key: SortKey): void {
		if (key === 'autoplay') {
			if (this.sort_key === 'autoplay') {
				this.sort_key = null;
				this.sort_dir = 1;
			} else {
				this.sort_key = 'autoplay';
				this.sort_dir = 1;
			}
			return;
		}
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
		extend = false,
		range = false,
		ordered_ids: readonly string[] = [],
		order?: number,
		ordered_rows?: readonly { stable_id: string; order: number }[]
	): void {
		const ordered =
			ordered_rows ??
			ordered_ids.map((id, i) => {
				const row = this.rows.find((r) => r.stable_id === id);
				return row ?? { stable_id: id, order: i + 1 };
			});
		applySelect(this, stable_id, extend, range, ordered, order);
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
			this.search_error = null;
		}
	}

	rememberScroll(top: number): void {
		this.scroll_top = top;
	}
}

export function createPaneStore(): PaneStore {
	return new PaneStore();
}

/** Settled whole-collection empty-state copy; search failures beat zero-hit copy. */
export function collectionSearchEmptyMessage(
	searchError: string | null,
	visibleCount: number
): string | null {
	if (searchError !== null) return `search failed: ${searchError}`;
	if (visibleCount === 0) return 'no tracks match the search';
	return null;
}

/** A membership replacement must be built from the complete playlist. The
 * browser deliberately caps rendered rows at 500, so a truncated pane must
 * remain read-only until a full-membership write path exists. */
export function canMutatePlaylist(
	pane: Pick<PaneStore, 'playlist_id' | 'etag' | 'truncated' | 'whole_collection'>
): boolean {
	return (
		pane.playlist_id !== null &&
		pane.playlist_id !== 'all' &&
		pane.etag !== '' &&
		!pane.truncated &&
		!pane.whole_collection
	);
}

/** Remixes checkbox: only an explicit wire true passes; null (synthetic rows)
 * is not a claimed remix and must not pass. */
export function rowIsRemix(row: BrowserRow): boolean {
	return row.is_remix === true;
}

// Vocals checkbox (VOCALS-FILTER-01): the three-way has-vocals rule lives in
// ./vocals-filter; applyLibraryFilters below applies it.
export { VOCALS_FILTER_MIN_LINES, vocalsFilterCounts, vocalsFilterTitle } from './vocals-filter';

/** Available-offline filter: local audio present, not cloud-only or streaming. */
export function rowIsLocallyAvailable(row: BrowserRow): boolean {
	if (row.is_streaming === true || row.spotify_pending === true) return false;
	return row.file_exists === true;
}

import { applyLibraryKeyFilter } from '$lib/rb/library-key-filter';
import { rowHasVocals } from './vocals-filter';
export { applyLibraryKeyFilter };

/** The selected row's stored key, looked up in each pool in order (search
 * results, then the pane rows a search fallback may be showing), so a visible
 * selected row is always found (Sol P1, #7743). Null when nothing matches. */
export function selectedRowKeyIn(
	selectedId: string | null,
	pools: readonly (readonly BrowserRow[])[]
): string | null {
	if (selectedId === null) return null;
	for (const pool of pools) {
		const row = pool.find((r) => r.stable_id === selectedId);
		if (row !== undefined) return row.key;
	}
	return null;
}

/** The library header's row filters (remixes, vocals, offline, keys) in one pass. */
export function applyLibraryFilters(
	rows: BrowserRow[],
	prefs: {
		remixes_filter: boolean;
		vocals_filter: boolean;
		available_offline_filter: boolean;
		library_key_filter: readonly string[];
	}
): BrowserRow[] {
	let out = rows;
	if (prefs.remixes_filter) out = out.filter(rowIsRemix);
	if (prefs.vocals_filter) out = out.filter(rowHasVocals);
	if (prefs.available_offline_filter) out = out.filter(rowIsLocallyAvailable);
	return applyLibraryKeyFilter(out, prefs.library_key_filter);
}

// ------------------------------------------------------- boot pane selection

// Moved to ./boot-pane-selection (pure, runeless; the karaoke lyric column
// plus its re-exports pushed this file past the 600-line file-size gate),
// re-exported so BrowserPanel.svelte and the boot-pane suites are unaffected.
// Only the two decisions BrowserPanel calls are re-exported: the All Tracks
// identity and its type are read straight from ./boot-pane-selection by the
// one suite that asserts on them, so no unreferenced export lands here.
export {
	resolveBootPlaylist,
	shouldRetryBootPane,
	parseLv1,
	writeLv1
} from './boot-pane-selection';

// ------------------------------------------------ boot health-read retries

// Moved to `$lib/rb/health-boot-retry` (adding these two functions here
// pushed this file past the 600-line file-size gate); re-exported so
// BrowserPanel.svelte's import of this barrel file is unaffected.
export {
	getHealthAtBoot,
	getHealthFreshWithRetry,
	reconcileBootSnapshot
} from '$lib/rb/health-boot-retry';

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
 * Spotify-pending placeholders stay visible under hide-broken. A streaming
 * service URI is a broken link and hides with the other unavailable rows
 * (absent, or unchecked / pending). */
export function filterRows(rows: BrowserRow[], query: string, hideBroken: boolean): BrowserRow[] {
	// FR-1: hide-broken applies before search so both compose.
	// The Broken checkbox is the inverse of hideBroken: unticked hides these rows.
	const base = hideBroken ? rows.filter((r) => !rowRendersUnavailable(r)) : rows;
	if (query.trim() === '') return base;
	const viaWork = viaWorkHits(query);
	return base.filter(
		(r) => viaWork.has(r.stable_id) || matchesSearchQuery(searchableTrackFromBrowserRow(r), query)
	);
}

/** Comparable cell value for a sort key (null = missing, sorts last). */
export function sortValue(row: BrowserRow, key: SortKey): string | number | null {
	const works = worksSortValue(worksView.overlay, worksView.classicalLayout, row, key);
	if (works !== undefined) return works;
	if (key === 'order') return row.order;
	else if (key === 'plays') return row.play_count;
	else if (key === 'title') return row.title;
	else if (key === 'artist') return row.artist;
	// KEY-GUESS-01: an ambiguous key sorts with its best guess, not with the blanks.
	else if (key === 'key') return row.key ?? row.key_guess ?? null;
	else if (key === 'bpm') return row.bpm;
	else if (key === 'rating') return normalizeRating(row.rating);
	else if (key === 'comments') return row.comments;
	else if (key === 'time') return row.duration_ms;
	else if (key === 'energy') return row.energy;
	else if (key === 'genre') return listingGenre(row);
	else if (key === 'filename') return fileNameSortValue(row);
	else if (key === 'location') return locationSortValue(row);
	else if (key === 'lyrics') return lyricsSortValue(row.lyrics);
	else if (key === 'grid') return gridFlagSortValue(row);
	throw new Error('AutoPlay ranks are not cell values');
}

/** Stable client sort; nulls last regardless of direction. Returns a
 * new array (never mutates the pane's membership-ordered rows). */
export function sortRows(
	rows: BrowserRow[],
	key: SortKey | null,
	dir: SortDir,
	autoPlayRankOf: ReadonlyMap<string, number> = new Map()
): BrowserRow[] {
	if (key === null) return rows;
	if (key === 'autoplay') return sortRowsByAutoPlayOrder(rows, autoPlayRankOf);
	return rows.slice().sort((a, b) => {
		const av = sortValue(a, key);
		const bv = sortValue(b, key);
		if (av === null && bv === null) return 0;
		else if (av === null)
			return 1; // nulls always last
		else if (bv === null) return -1;
		const base =
			typeof av === 'string' && typeof bv === 'string'
				? av.localeCompare(bv)
				: (av as number) - (bv as number);
		return base * dir;
	});
}

/** The full read pipeline a pane renders: FR-1 filter -> search -> sort. */
export function visibleRowsOf(
	pane: PaneStore,
	hideBroken: boolean,
	autoPlayRankOf: ReadonlyMap<string, number> = new Map()
): BrowserRow[] {
	return sortRows(
		filterRows(pane.rows, pane.search, hideBroken),
		pane.sort_key,
		pane.sort_dir,
		autoPlayRankOf
	);
}

/** Replace one row in a pane's rows and search_results (raw $state arrays). */
export function replacePaneRow(pane: PaneStore, stable_id: string, next: BrowserRow): void {
	let rowsChanged = false;
	const rows = pane.rows.map((row) => {
		if (row.stable_id !== stable_id) return row;
		rowsChanged = true;
		return next;
	});
	if (rowsChanged) pane.rows = rows;

	let searchChanged = false;
	const search = pane.search_results.map((row) => {
		if (row.stable_id !== stable_id) return row;
		searchChanged = true;
		return next;
	});
	if (searchChanged) pane.search_results = search;
}

/** Map one row everywhere it appears in a pane (membership and FTS results). */
export function updatePaneRow(
	pane: PaneStore,
	stable_id: string,
	map: (row: BrowserRow) => BrowserRow
): void {
	const fromRows = pane.rows.find((row) => row.stable_id === stable_id);
	const fromSearch = pane.search_results.find((row) => row.stable_id === stable_id);
	const seed = fromRows ?? fromSearch;
	if (seed === undefined) return;
	const next = map(seed);
	replacePaneRow(pane, stable_id, next);
}

/** The same row update across every open pane (strip adoption, shared cache). */
export function updateRowAcrossPanes(
	panes: readonly PaneStore[],
	stable_id: string,
	map: (row: BrowserRow) => BrowserRow
): void {
	for (const pane of panes) updatePaneRow(pane, stable_id, map);
}

/** Writes a decoded strip into every matching row in every pane, ALWAYS overwriting - the audience-ambiguous sidecar (TECH-DEBT.md) never outranks /anlz's own audience-scoped decode. */
export function applyDecodedStripAcrossPanes(
	panes: readonly PaneStore[],
	stable_id: string,
	strip: PreviewStripData | null
): void {
	updateRowAcrossPanes(panes, stable_id, (row) => ({ ...row, strip }));
}

// ---------------------------------------------------------------- pane tabs
// Tab reordering + new-tab placement moved to ./pane-tabs (plain array/index
// math, a distinct concern from the reactive PaneStore contract above).
// Re-exported here so BrowserPanel.svelte and the existing tests keep one
// import site for the pane vocabulary.
export {
	MAX_PANE_SLOTS,
	canAddPaneSlot,
	reorderPanesInPlace,
	resolveNewTabIndex
} from './pane-tabs';

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
