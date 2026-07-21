<script lang="ts">
	// Build unit: browser (COMPONENT-MAP 1.5, SCREENSHOT-SPEC 5).
	// Layout: icon rail (inert) | playlist tree (real: /playlists + /health
	// counts - OUR numbers) | 4-pane track list. Rows arrive FULLY HYDRATED
	// from the listing payloads (shared contract 1/4 - no more 29x per-row
	// GET fan-out, no per-row /anlz for strips; /anlz is deck-load only).
	// Lazy per-visible-row fetch remains ONLY for rb-meta (artwork flag +
	// genre/streaming fallback on All Tracks rows). Editable ratings via
	// PATCH + If-Match; client-side search + sort; FR-1 broken-link
	// graying + 'Hide broken links' toggle persisted in prefs.svelte.ts.
	import { onMount } from 'svelte';
	import {
		ConflictError,
		RbApiError,
		decodePreviewStrip,
		fetchRbMeta,
		getHealth,
		getPlaylistHydrated,
		getTrack,
		listPlaylistsHydrated,
		listTracksHydrated,
		patchTrack,
		vocalsOf
	} from '$lib/rb/api-rb';
	import type {
		PlaylistSummaryHydrated,
		PlaylistTrackRowWire,
		TrackListItemWire,
		Vocals
	} from '$lib/rb/api-rb';
	import type { DeckId, PlaylistNode } from '$lib/rb/types';
	// Engine contract (build unit audio-engine): $lib/rb/audio-engine.svelte.ts
	// exports `engine` (AudioEngine impl) and `deckStates` (Record<DeckId,
	// DeckState> rune state, aliased to `decks` here). If the engine unit ever
	// moves, this import is the single line to fix.
	import { deckStates as decks, engine } from '$lib/rb/audio-engine.svelte';
	import { setHideBrokenLinks, uiPrefs } from '$lib/rb/prefs.svelte';
	import { pushToast } from '$lib/stores.svelte';
	import IconRail from './browser/IconRail.svelte';
	import PaneTabs from './browser/PaneTabs.svelte';
	import type { PaneTabInfo } from './browser/PaneTabs.svelte';
	import PlaylistTree from './browser/PlaylistTree.svelte';
	import SearchBox from './browser/SearchBox.svelte';
	import TrackTable from './browser/TrackTable.svelte';
	import type { BrowserRow, SortKey } from './browser/TrackTable.svelte';
	import { getAnlzEntry } from './wave/anlz-cache.svelte';

	// No virtualization at v1: the existing VirtualTable is library-page
	// specific (hardcoded columns + route navigation), so panes cap at 500
	// rows with an explicit truncation note (PARITY-TODO).
	const MAX_ROWS = 500;
	const DECK_IDS: DeckId[] = [1, 2, 3, 4];

	interface PaneState {
		playlist_id: string | null;
		title: string;
		rows: BrowserRow[];
		loading: boolean;
		error: string | null;
		search: string;
		selected_id: string | null;
		sort_key: SortKey | null;
		sort_dir: 1 | -1;
		truncated: boolean;
		load_seq: number;
	}

	function _blankPane(): PaneState {
		return {
			playlist_id: null,
			title: 'blank list',
			rows: [],
			loading: false,
			error: null,
			search: '',
			selected_id: null,
			sort_key: null,
			sort_dir: 1,
			truncated: false,
			load_seq: 0
		};
	}

	let panes = $state<PaneState[]>([_blankPane(), _blankPane(), _blankPane(), _blankPane()]);
	let activePane = $state(0);
	let playlists = $state<PlaylistSummaryHydrated[]>([]);
	let allTracksCount = $state<number | null>(null);
	const _inflight = new Set<string>();

	const pane = $derived(panes[activePane]);
	const loadedIds = $derived(
		new Set(DECK_IDS.map((d) => decks[d].stable_id).filter((v): v is string => v !== null))
	);
	// Vocals ALREADY known client-side for the visible pane's rows: loaded
	// decks (engine-populated anlz) + the wavestack's anlz cache. v1 SCOPE
	// DECISION (documented per task brief): table strips do NOT issue their
	// own /anlz fetches - a ?fields=vocals slim endpoint is NOT in the
	// shared contract, and a full per-visible-row /anlz would reinstate
	// exactly the fan-out this change removes. Bars therefore appear on
	// strips only for tracks whose analysis is already in memory.
	const vocalsById = $derived.by((): Record<string, Vocals> => {
		const out: Record<string, Vocals> = {};
		for (const d of DECK_IDS) {
			const st = decks[d];
			if (st.stable_id !== null && st.anlz !== null) out[st.stable_id] = vocalsOf(st.anlz);
		}
		for (const row of pane.rows) {
			if (out[row.stable_id] !== undefined) continue;
			const entry = getAnlzEntry(row.stable_id);
			if (entry !== undefined && entry.status === 'ready') {
				out[row.stable_id] = vocalsOf(entry.data);
			}
		}
		return out;
	});
	const treeNodes = $derived(
		playlists
			.slice()
			// FR-1: with 'Hide broken links' ON, playlists with zero available
			// (on-disk) tracks vanish from the tree.
			.filter((p) => !uiPrefs.hide_broken_links || p.available_count > 0)
			// Rekordbox custom tree order (djmdPlaylist Seq walk, SCREENSHOT-SPEC
			// 5b) - NOT alphabetical. Playlists without a rekordbox order (seq
			// null) sink below the ordered ones, name-sorted among themselves.
			.sort((a, b) => {
				if (a.seq !== null && b.seq !== null) return a.seq - b.seq;
				else if (a.seq !== null) return -1;
				else if (b.seq !== null) return 1;
				else return a.name.localeCompare(b.name);
			})
			.map(
				(p): PlaylistNode => ({
					playlist_id: p.playlist_id,
					name: p.name,
					track_count: p.track_count,
					kind: 'playlist',
					children: []
				})
			)
	);
	const tabs = $derived(
		panes.map(
			(p): PaneTabInfo => ({
				title: p.playlist_id === null ? 'blank list' : p.title,
				count: p.playlist_id === null || p.loading ? null : p.rows.length,
				loading: p.loading
			})
		)
	);
	const visibleRows = $derived(
		_sort(
			_filter(pane.rows, pane.search, uiPrefs.hide_broken_links),
			pane.sort_key,
			pane.sort_dir
		)
	);
	const emptyMessage = $derived.by((): string | null => {
		if (pane.loading) return 'loading...';
		else if (pane.error !== null) return `load failed: ${pane.error}`;
		else if (pane.playlist_id === null) return 'blank list - choose a playlist in the tree';
		else if (visibleRows.length === 0 && pane.search.trim() !== '') return 'no tracks match the search';
		else if (visibleRows.length === 0 && pane.rows.length > 0 && uiPrefs.hide_broken_links)
			return 'all tracks in this list are broken links (hidden by Hide broken links)';
		else if (visibleRows.length === 0) return 'empty playlist';
		else return null;
	});

	onMount(() => {
		void _init();
	});

	async function _init(): Promise<void> {
		try {
			const [healthRes, lists] = await Promise.all([getHealth(), listPlaylistsHydrated()]);
			allTracksCount = healthRes.health.state_db.tracks;
			playlists = lists;
		} catch (exc) {
			pushToast(`browser init failed: ${String(exc)}`, 'error');
			throw exc;
		}
	}

	// ------------------------------------------------- pane playlist loading

	function selectPlaylist(node: PlaylistNode): void {
		void _loadPane(panes[activePane], node);
	}

	async function _loadPane(p: PaneState, node: PlaylistNode): Promise<void> {
		const seq = ++p.load_seq; // stale-response guard for rapid re-selection
		p.playlist_id = node.playlist_id;
		p.title = node.name;
		p.rows = [];
		p.loading = true;
		p.error = null;
		p.selected_id = null;
		p.truncated = false;
		try {
			const result =
				node.kind === 'all_tracks'
					? await _fetchAllRows()
					: await _fetchPlaylistRows(node.playlist_id);
			if (p.load_seq !== seq) return; // superseded by a newer selection
			p.rows = result.rows;
			p.truncated = result.truncated;
		} catch (exc) {
			if (p.load_seq === seq) p.error = String(exc);
			pushToast(`playlist load failed: ${String(exc)}`, 'error');
		} finally {
			if (p.load_seq === seq) p.loading = false;
		}
	}

	function _rowFromPlaylistWire(wire: PlaylistTrackRowWire, order: number): BrowserRow {
		if (typeof wire.stable_id !== 'string' || typeof wire.file_exists !== 'boolean') {
			throw new Error(
				`hydrated playlist row ${order} malformed - backend contract point 4 not met`
			);
		}
		return {
			stable_id: wire.stable_id,
			order,
			title: wire.title,
			artist: wire.artist,
			key: wire.key,
			bpm: wire.bpm,
			rating: wire.rating,
			etag: wire.etag,
			comments: wire.comments,
			duration_ms: wire.duration_ms,
			genre: wire.genre,
			file_exists: wire.file_exists,
			is_streaming: wire.is_streaming,
			strip: decodePreviewStrip(wire.preview_b64, wire.preview_max),
			rb_meta: null,
			revealed: false
		};
	}

	function _rowFromListWire(track: TrackListItemWire, order: number): BrowserRow {
		if (typeof track.stable_id !== 'string') {
			throw new Error(`tracks endpoint returned a non-track payload at row ${order}`);
		}
		return {
			stable_id: track.stable_id,
			order,
			title: track.title,
			artist: track.artist,
			key: track.key,
			bpm: track.bpm,
			rating: track.rating,
			// List items carry no ETag; rating edits lazily fetch one.
			etag: '',
			comments: track.notes,
			duration_ms: track.duration_ms ?? null,
			// genre/is_streaming are NOT in the listing contract (point 1) -
			// null here means 'fall back to lazily fetched rb-meta'.
			genre: null,
			file_exists: track.file_exists,
			is_streaming: null,
			strip: decodePreviewStrip(track.preview_b64, track.preview_max),
			rb_meta: null,
			revealed: false
		};
	}

	async function _fetchAllRows(): Promise<{ rows: BrowserRow[]; truncated: boolean }> {
		// All Tracks: one page of MAX_ROWS with inline preview/file_exists
		// (contract 1). ?available stays server-default 'all': FR-1 hiding
		// is client-side so the toggle flips instantly on loaded panes; the
		// API filter exists for agent parity, not for this UI path.
		const page = await listTracksHydrated({ limit: MAX_ROWS });
		return {
			rows: page.items.map((t, i) => _rowFromListWire(t, i + 1)),
			truncated: page.next_cursor !== null
		};
	}

	async function _fetchPlaylistRows(
		id: string
	): Promise<{ rows: BrowserRow[]; truncated: boolean }> {
		// Hydrated detail (contract 4): full rows in membership order, one
		// request - the 29x per-row GET fan-out is gone.
		const detail = await getPlaylistHydrated(id);
		const rows = detail.tracks
			.slice(0, MAX_ROWS)
			.map((wire, i) => _rowFromPlaylistWire(wire, i + 1));
		return { rows, truncated: detail.tracks.length > MAX_ROWS };
	}

	// -------------------------------------------- lazy per-row hydration
	// Only rb-meta remains lazy (artwork_available + genre/streaming
	// fallback for All Tracks rows). Strips/file_exists arrive inline; the
	// observer also flips row.revealed for the one-time canvas draw.

	function rowVisible(row: BrowserRow): void {
		row.revealed = true;
		void _hydrateRowMeta(row);
	}

	async function _hydrateRowMeta(row: BrowserRow): Promise<void> {
		if (row.rb_meta !== null || _inflight.has(row.stable_id)) return;
		_inflight.add(row.stable_id);
		try {
			row.rb_meta = await fetchRbMeta(row.stable_id);
		} catch (exc) {
			if (exc instanceof RbApiError && exc.status === 404) {
				// No rekordbox vendor mapping for this track - a real library
				// state: no meta, no artwork.
				return;
			}
			// Loud but non-modal: a toast per row would spam during scrolling.
			console.error(`rb-meta hydration failed for ${row.stable_id}:`, exc);
		} finally {
			_inflight.delete(row.stable_id);
		}
	}

	// ------------------------------------------------------- rating edits

	function rateRow(row: BrowserRow, next: number): void {
		void _patchRating(row, next);
	}

	async function _patchRating(row: BrowserRow, next: number): Promise<void> {
		try {
			let etag = row.etag;
			if (etag === '') {
				// All Tracks rows arrive without an ETag - fetch one first.
				etag = (await getTrack(row.stable_id)).etag;
			}
			const { track, etag: fresh } = await patchTrack(row.stable_id, etag, { rating: next });
			row.rating = track.rating;
			row.etag = fresh;
		} catch (exc) {
			if (exc instanceof ConflictError) {
				row.rating = exc.current.rating;
				row.etag = exc.etag;
				pushToast('rating conflict: track changed elsewhere - showing current value', 'error');
				return;
			}
			pushToast(`rating update failed: ${String(exc)}`, 'error');
		}
	}

	// -------------------------------------------------------- deck loading

	function loadRow(row: BrowserRow, deck: DeckId | null): void {
		void _loadOntoDeck(row, deck);
	}

	async function _loadOntoDeck(row: BrowserRow, deck: DeckId | null): Promise<void> {
		if (row.is_streaming ?? row.rb_meta?.is_streaming ?? false) {
			pushToast('streaming track - deck load not implemented (see PARITY-TODO)', 'error');
			return;
		}
		if (!row.file_exists) {
			// FR-1: broken-link rows stay selectable but never load.
			pushToast('cannot load: audio file missing on disk (broken link)', 'error');
			return;
		}
		const target = deck ?? _lowestFreeDeck();
		if (target === null) {
			pushToast('no free deck: all 4 decks are loaded', 'error');
			return;
		}
		try {
			await engine.load(target, row.stable_id);
		} catch (exc) {
			// Backend refusals (AUDIO_FILE_MISSING / AUDIO_IS_STREAMING_URI /
			// TRACK_NOT_FOUND) surface here - loud, never silent.
			pushToast(`deck ${target} load failed: ${String(exc)}`, 'error');
		}
	}

	function _lowestFreeDeck(): DeckId | null {
		for (const d of DECK_IDS) {
			if (decks[d].stable_id === null) return d;
		}
		return null;
	}

	// ---------------------------------------------- client sort + search

	function _filter(rows: BrowserRow[], query: string, hideBroken: boolean): BrowserRow[] {
		// FR-1: hide-broken applies before search so both compose.
		const base = hideBroken ? rows.filter((r) => r.file_exists) : rows;
		const q = query.trim().toLowerCase();
		if (q === '') return base;
		return base.filter((r) =>
			[r.title, r.artist, r.comments, r.key, r.genre ?? r.rb_meta?.genre ?? null].some(
				(field) => field !== null && field.toLowerCase().includes(q)
			)
		);
	}

	function _sortVal(row: BrowserRow, key: SortKey): string | number | null {
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

	function _sort(rows: BrowserRow[], key: SortKey | null, dir: 1 | -1): BrowserRow[] {
		if (key === null) return rows;
		return rows.slice().sort((a, b) => {
			const av = _sortVal(a, key);
			const bv = _sortVal(b, key);
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

	function sortBy(key: SortKey): void {
		const p = panes[activePane];
		if (p.sort_key === key) {
			p.sort_dir = p.sort_dir === 1 ? -1 : 1;
		} else {
			p.sort_key = key;
			p.sort_dir = 1;
		}
	}

	function selectRow(row: BrowserRow): void {
		panes[activePane].selected_id = row.stable_id;
	}

	function setSearch(next: string): void {
		panes[activePane].search = next;
	}
</script>

<section class="rb-browser">
	<IconRail />
	<div class="tree-panel">
		<PlaylistTree
			nodes={treeNodes}
			{allTracksCount}
			selectedId={pane.playlist_id}
			onselect={selectPlaylist}
		/>
	</div>
	<div class="list-panel">
		<div class="pane-header">
			<PaneTabs {tabs} active={activePane} onactivate={(i) => (activePane = i)} />
			<div class="header-right">
				<button
					class="rb-lit-button rb-inert master-dd"
					disabled
					title="not implemented - see PARITY-TODO"
				>
					MASTER <span class="caret">▾</span>
				</button>
				<button
					class="icon-btn rb-inert"
					disabled
					title="not implemented - see PARITY-TODO"
					aria-label="compact row density"
				>
					<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
						<path d="M2 3h12v1.5H2zM2 6h12v1.5H2zM2 9h12v1.5H2zM2 12h12v1.5H2z" fill="currentColor" />
					</svg>
				</button>
				<button
					class="icon-btn rb-inert"
					disabled
					title="not implemented - see PARITY-TODO"
					aria-label="tall row density"
				>
					<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
						<path d="M2 3h12v3H2zM2 8h12v3H2z" fill="currentColor" />
					</svg>
				</button>
				<button
					class="icon-btn rb-inert"
					disabled
					title="not implemented - see PARITY-TODO"
					aria-label="single column layout"
				>
					<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
						<path d="M2 2h12v12H2z" fill="none" stroke="currentColor" stroke-width="1.4" />
					</svg>
				</button>
				<button
					class="icon-btn rb-inert"
					disabled
					title="not implemented - see PARITY-TODO"
					aria-label="split column layout"
				>
					<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
						<path
							d="M2 2h12v12H2zM8 2v12"
							fill="none"
							stroke="currentColor"
							stroke-width="1.4"
						/>
					</svg>
				</button>
				<label
					class="hide-broken"
					title="FR-1: hide tracks whose audio file is missing on disk; also hides playlists with zero available tracks from the tree"
				>
					<input
						type="checkbox"
						checked={uiPrefs.hide_broken_links}
						onchange={(e) => setHideBrokenLinks(e.currentTarget.checked)}
					/>
					<span>Hide broken links</span>
				</label>
				<SearchBox value={pane.search} oninput={setSearch} />
			</div>
		</div>
		<TrackTable
			rows={visibleRows}
			selectedId={pane.selected_id}
			{loadedIds}
			{vocalsById}
			sortKey={pane.sort_key}
			sortDir={pane.sort_dir}
			truncated={pane.truncated}
			{emptyMessage}
			onsort={sortBy}
			onselectrow={selectRow}
			onloadrow={loadRow}
			onrate={rateRow}
			onrowvisible={rowVisible}
		/>
	</div>
	<div class="bottom-bar">
		<button
			class="icon-btn rb-inert"
			disabled
			title="not implemented - see PARITY-TODO"
			aria-label="export/eject"
		>
			<svg viewBox="0 0 16 16" width="11" height="11" aria-hidden="true">
				<path d="M8 3l5 6H3zM3 11h10v2H3z" fill="currentColor" />
			</svg>
		</button>
		<span class="wordmark">rekordbox</span>
		<span class="grip" aria-hidden="true">
			<svg viewBox="0 0 12 12" width="10" height="10">
				<path d="M11 1L1 11M11 5L5 11M11 9L9 11" stroke="currentColor" stroke-width="1" />
			</svg>
		</span>
	</div>
</section>

<style>
	.rb-browser {
		grid-area: browser;
		display: grid;
		grid-template-areas:
			'rail tree list'
			'bottom bottom bottom';
		grid-template-columns: 30px 300px minmax(0, 1fr);
		grid-template-rows: minmax(0, 1fr) 18px;
		min-height: 0;
		background: var(--rb-bg);
		border-top: 1px solid var(--rb-border);
		font-size: var(--rb-fs-browser);
	}
	.tree-panel {
		grid-area: tree;
		min-height: 0;
		background: var(--rb-panel);
		border-right: 1px solid var(--rb-border);
	}
	.list-panel {
		grid-area: list;
		display: flex;
		flex-direction: column;
		min-height: 0;
		min-width: 0;
		background: var(--rb-panel);
	}
	.pane-header {
		flex: none;
		display: flex;
		align-items: stretch;
		justify-content: space-between;
		height: 24px;
		border-bottom: 1px solid var(--rb-border);
		background: var(--rb-panel);
		min-width: 0;
	}
	.header-right {
		display: flex;
		align-items: center;
		gap: 4px;
		padding: 0 6px;
		flex: none;
	}
	.master-dd {
		font-size: var(--rb-fs-label);
	}
	.hide-broken {
		display: inline-flex;
		align-items: center;
		gap: 3px;
		color: var(--rb-text-dim);
		font-size: var(--rb-fs-label);
		white-space: nowrap;
		cursor: pointer;
	}
	.hide-broken:hover {
		color: var(--rb-text);
	}
	.hide-broken input {
		width: 10px;
		height: 10px;
		margin: 0;
		accent-color: var(--rb-accent);
		cursor: pointer;
	}
	.caret {
		font-size: 7px;
	}
	.icon-btn {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 20px;
		height: 18px;
		padding: 0;
		background: transparent;
		border: none;
		color: var(--rb-text-dim);
	}
	.bottom-bar {
		grid-area: bottom;
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 0 6px;
		background: var(--rb-panel);
		border-top: 1px solid var(--rb-border);
	}
	.wordmark {
		color: var(--rb-text-dim);
		font-size: var(--rb-fs-label);
		font-weight: 600;
		letter-spacing: 0.5px;
	}
	.grip {
		margin-left: auto;
		color: var(--rb-text-dim);
	}
</style>
