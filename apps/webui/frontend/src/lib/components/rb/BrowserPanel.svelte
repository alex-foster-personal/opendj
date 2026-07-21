<script lang="ts">
	// Build unit: browser (COMPONENT-MAP 1.5, SCREENSHOT-SPEC 5).
	// Layout: icon rail (inert) | playlist tree (real: /playlists + /health
	// counts - OUR numbers) | 4-pane track list (active pane hydrated via
	// GET /playlists/{id} + per-track GET /tracks/{sid}; lazy rb-meta/preview
	// per visible row; editable ratings via PATCH + If-Match; client-side
	// search + sort; dblclick/hover-buttons -> engine.load).
	import { onMount } from 'svelte';
	import {
		ConflictError,
		RbApiError,
		fetchAnlz,
		fetchRbMeta,
		getHealth,
		getPlaylist,
		getTrack,
		listPlaylists,
		listTracks,
		patchTrack
	} from '$lib/rb/api-rb';
	import type { PlaylistSummary, Track } from '$lib/rb/api-rb';
	import type { DeckId, PlaylistNode, TrackRow } from '$lib/rb/types';
	// Engine contract (build unit audio-engine): $lib/rb/audio-engine.svelte.ts
	// exports `engine` (AudioEngine impl) and `deckStates` (Record<DeckId,
	// DeckState> rune state, aliased to `decks` here). If the engine unit ever
	// moves, this import is the single line to fix.
	import { deckStates as decks, engine } from '$lib/rb/audio-engine.svelte';
	import { pushToast } from '$lib/stores.svelte';
	import IconRail from './browser/IconRail.svelte';
	import PaneTabs from './browser/PaneTabs.svelte';
	import type { PaneTabInfo } from './browser/PaneTabs.svelte';
	import PlaylistTree from './browser/PlaylistTree.svelte';
	import SearchBox from './browser/SearchBox.svelte';
	import TrackTable from './browser/TrackTable.svelte';
	import type { SortKey } from './browser/TrackTable.svelte';

	// No virtualization at v1: the existing VirtualTable is library-page
	// specific (hardcoded columns + route navigation), so panes cap at 500
	// rows with an explicit truncation note (PARITY-TODO).
	const MAX_ROWS = 500;
	// Note A hydration gap (COMPONENT-MAP 3): PlaylistDetail.items is
	// stable_ids only; hydrate via parallel GET /tracks/{sid} in waves.
	const HYDRATE_BATCH = 24;
	const DECK_IDS: DeckId[] = [1, 2, 3, 4];

	/** Backend TrackOut carries duration_ms; the legacy client Track type
	 * omits it (stale). Local widening only - types.ts is the contract. */
	type TrackWire = Track & { duration_ms?: number | null };

	interface PaneState {
		playlist_id: string | null;
		title: string;
		rows: TrackRow[];
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
	let playlists = $state<PlaylistSummary[]>([]);
	let allTracksCount = $state<number | null>(null);
	/** waveform kind per stable_id ('tri' | 'mono') - sits beside TrackRow
	 * because the contract's TrackRow.preview carries bands only. */
	let kinds = $state<Record<string, 'tri' | 'mono'>>({});
	const _inflight = new Set<string>();

	const pane = $derived(panes[activePane]);
	const loadedIds = $derived(
		new Set(DECK_IDS.map((d) => decks[d].stable_id).filter((v): v is string => v !== null))
	);
	const treeNodes = $derived(
		playlists
			.slice()
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
	const visibleRows = $derived(_sort(_filter(pane.rows, pane.search), pane.sort_key, pane.sort_dir));
	const emptyMessage = $derived.by((): string | null => {
		if (pane.loading) return 'loading...';
		else if (pane.error !== null) return `load failed: ${pane.error}`;
		else if (pane.playlist_id === null) return 'blank list - choose a playlist in the tree';
		else if (visibleRows.length === 0 && pane.search.trim() !== '') return 'no tracks match the search';
		else if (visibleRows.length === 0) return 'empty playlist';
		else return null;
	});

	onMount(() => {
		void _init();
	});

	async function _init(): Promise<void> {
		try {
			const [healthRes, lists] = await Promise.all([getHealth(), listPlaylists()]);
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

	function _toRow(track: TrackWire, order: number, etag: string): TrackRow {
		if (typeof track.stable_id !== 'string') {
			// The legacy client does not check r.ok; guard loudly here instead
			// of rendering an error payload as a track row.
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
			etag,
			comments: track.notes,
			duration_ms: track.duration_ms ?? null,
			rb_meta: null,
			preview: null
		};
	}

	async function _fetchAllRows(): Promise<{ rows: TrackRow[]; truncated: boolean }> {
		// All Tracks: one page of MAX_ROWS. List items carry no ETag; rating
		// edits lazily fetch one via GET /tracks/{sid} (see _patchRating).
		const page = await listTracks({ limit: MAX_ROWS });
		return {
			rows: page.items.map((t, i) => _toRow(t as TrackWire, i + 1, '')),
			truncated: page.next_cursor !== null
		};
	}

	async function _fetchPlaylistRows(id: string): Promise<{ rows: TrackRow[]; truncated: boolean }> {
		const detail = await getPlaylist(id);
		const ids = detail.items.slice(0, MAX_ROWS);
		const rows: TrackRow[] = new Array<TrackRow>(ids.length);
		for (let i = 0; i < ids.length; i += HYDRATE_BATCH) {
			const chunk = ids.slice(i, i + HYDRATE_BATCH);
			const fetched = await Promise.all(chunk.map((sid) => getTrack(sid)));
			fetched.forEach((got, j) => {
				rows[i + j] = _toRow(got.track as TrackWire, i + j + 1, got.etag);
			});
		}
		return { rows, truncated: detail.items.length > MAX_ROWS };
	}

	// -------------------------------------------- lazy per-row hydration

	function rowVisible(row: TrackRow): void {
		void _hydrateRow(row);
	}

	async function _hydrateRow(row: TrackRow): Promise<void> {
		if (row.rb_meta !== null || _inflight.has(row.stable_id)) return;
		_inflight.add(row.stable_id);
		try {
			const meta = await fetchRbMeta(row.stable_id);
			row.rb_meta = meta;
			if (!meta.analysis_available) {
				row.preview = 'unavailable'; // skip the doomed /anlz fetch
				return;
			}
			try {
				const anlz = await fetchAnlz(row.stable_id, 400);
				kinds[row.stable_id] = anlz.waveform.kind;
				row.preview = anlz.waveform.preview;
			} catch (exc) {
				if (exc instanceof RbApiError && exc.status === 404) {
					row.preview = 'unavailable'; // explicit no-analysis state
				} else {
					throw exc;
				}
			}
		} catch (exc) {
			if (exc instanceof RbApiError && exc.status === 404) {
				// No rekordbox vendor mapping for this track - a real library
				// state: no meta, no preview possible.
				row.preview = 'unavailable';
				return;
			}
			// Loud but non-modal: a toast per row would spam during scrolling.
			console.error(`rb-meta/anlz hydration failed for ${row.stable_id}:`, exc);
		} finally {
			_inflight.delete(row.stable_id);
		}
	}

	// ------------------------------------------------------- rating edits

	function rateRow(row: TrackRow, next: number): void {
		void _patchRating(row, next);
	}

	async function _patchRating(row: TrackRow, next: number): Promise<void> {
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

	function loadRow(row: TrackRow, deck: DeckId | null): void {
		void _loadOntoDeck(row, deck);
	}

	async function _loadOntoDeck(row: TrackRow, deck: DeckId | null): Promise<void> {
		if (row.rb_meta !== null && row.rb_meta.is_streaming) {
			pushToast('streaming track - deck load not implemented (see PARITY-TODO)', 'error');
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

	function _filter(rows: TrackRow[], query: string): TrackRow[] {
		const q = query.trim().toLowerCase();
		if (q === '') return rows;
		return rows.filter((r) =>
			[r.title, r.artist, r.comments, r.key, r.rb_meta?.genre ?? null].some(
				(field) => field !== null && field.toLowerCase().includes(q)
			)
		);
	}

	function _sortVal(row: TrackRow, key: SortKey): string | number | null {
		if (key === 'order') return row.order;
		else if (key === 'title') return row.title;
		else if (key === 'artist') return row.artist;
		else if (key === 'key') return row.key;
		else if (key === 'bpm') return row.bpm;
		else if (key === 'rating') return row.rating;
		else if (key === 'comments') return row.comments;
		else if (key === 'time') return row.duration_ms;
		else return row.rb_meta?.genre ?? null;
	}

	function _sort(rows: TrackRow[], key: SortKey | null, dir: 1 | -1): TrackRow[] {
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

	function selectRow(row: TrackRow): void {
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
				<SearchBox value={pane.search} oninput={setSearch} />
			</div>
		</div>
		<TrackTable
			rows={visibleRows}
			selectedId={pane.selected_id}
			{loadedIds}
			{kinds}
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
