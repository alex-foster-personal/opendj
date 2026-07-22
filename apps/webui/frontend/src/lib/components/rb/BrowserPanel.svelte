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
		getTrack,
		listPlaylistsHydrated,
		listTracksHydrated,
		patchTrack,
		searchCollection,
		vocalsOf
	} from '$lib/rb/api-rb';
	import type {
		PlaylistSummaryHydrated,
		PlaylistTrackRowWire,
		SearchHitWire,
		TrackListItemWire,
		Vocals
	} from '$lib/rb/api-rb';
	import type { DeckId, PlaylistNode } from '$lib/rb/types';
	// Deck state remains engine-owned; real load interactions route through
	// the same validated dispatcher exposed to browser agents.
	import { deckStates as decks } from '$lib/rb/audio-engine.svelte';
	import { runPerformanceCommandFromUi } from '$lib/rb/performance-ipc.svelte';
	import { setHideBrokenLinks, uiPrefs } from '$lib/rb/prefs.svelte';
	import { subscribeBrowserSearch } from '$lib/rb/browser-search';
	import {
		getPlaylistTracksEtag,
		PlaylistConflictError,
		replacePlaylistTracks
	} from '$lib/rb/playlist-write';
	import { pushToast } from '$lib/stores.svelte';
	import SuggestNextStrip from './SuggestNextStrip.svelte';
	import BulkEditModal from './BulkEditModal.svelte';
	import FindReplaceModal from './FindReplaceModal.svelte';
	import MyTagEditorModal from './MyTagEditorModal.svelte';
	import AddTrackSearch from './browser/AddTrackSearch.svelte';
	import IconRail from './browser/IconRail.svelte';
	import PaneTabs from './browser/PaneTabs.svelte';
	import type { PaneTabInfo } from './browser/PaneTabs.svelte';
	import {
		createPaneStore,
		makeClientRowProvider,
		sortRows,
		visibleRowsOf
	} from './browser/pane-contract.svelte';
	import type { BrowserRow, PaneStore, SortKey } from './browser/pane-contract.svelte';
	import PlaylistTree from './browser/PlaylistTree.svelte';
	import SearchBox from './browser/SearchBox.svelte';
	import TrackTable from './browser/TrackTable.svelte';
	import { getAnlzEntry } from './wave/anlz-cache.svelte';
	import { getSpotifyPendingTracks, type SpotifyPendingTrack } from '$lib/rb/spotify-api';
	import SpotifySourcePanel from './browser/SpotifySourcePanel.svelte';

	// No virtualization at v1: the existing VirtualTable is library-page
	// specific (hardcoded columns + route navigation), so panes cap at 500
	// rows with an explicit truncation note (PARITY-TODO).
	const MAX_ROWS = 500;
	const MAX_SEARCH_ROWS = 200;
	const DECK_IDS: DeckId[] = [1, 2, 3, 4];

	// Pane state lives in the typed contract (pane-contract.svelte.ts):
	// 4 independent PaneStore instances - selection, search, sort, and
	// scroll cursor per pane survive tab switches. Only the active pane
	// is mounted (one TrackTable) - a deliberate perf choice, kept.
	const panes: PaneStore[] = [
		createPaneStore(),
		createPaneStore(),
		createPaneStore(),
		createPaneStore()
	];
	let activePane = $state(0);
	let openModal = $state<'bulk-edit' | 'find-replace' | 'mytag' | null>(null);
	let modalEtags = $state<Record<string, string>>({});
	let playlists = $state<PlaylistSummaryHydrated[]>([]);
	let allTracksCount = $state<number | null>(null);
	let playlistsLoading = $state(true);
	let playlistsError = $state<string | null>(null);
	let source = $state<'collection' | 'spotify'>('collection');
	let spotifySelectedId = $state<string | null>(null);
	let spotifyPendingTracks = $state<SpotifyPendingTrack[] | null>(null);
	let spotifyPendingLoading = $state(false);
	let spotifyPendingError = $state<string | null>(null);
	let spotifyPendingSequence = 0;
	const _inflight = new Set<string>();

	const pane = $derived(panes[activePane]);
	const spotifyPlaylists = $derived(playlists.filter((playlist) => playlist.vendor === 'spotify'));
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
	const wholeCollectionActive = $derived(pane.whole_collection && pane.search.trim() !== '');
	const visibleRows = $derived(
		wholeCollectionActive
			? sortRows(pane.search_results, pane.sort_key, pane.sort_dir)
			: visibleRowsOf(pane, uiPrefs.hide_broken_links)
	);
	// Read contract handed to TrackTable (getters stay reactive through
	// visibleRows/pane). The virtualization lane replaces THIS provider,
	// not TrackTable's props.
	const provider = makeClientRowProvider(
		() => visibleRows,
		() => pane.truncated || (wholeCollectionActive && pane.search_total > MAX_SEARCH_ROWS)
	);
	const emptyMessage = $derived.by((): string | null => {
		if (wholeCollectionActive) {
			if (pane.searching) return 'searching whole collection...';
			else if (visibleRows.length === 0) return 'no tracks match the search';
			else return null;
		} else if (pane.loading) return 'loading...';
		else if (pane.error !== null) return `load failed: ${pane.error}`;
		else if (pane.playlist_id === null) return 'blank list - choose a playlist in the tree';
		else if (visibleRows.length === 0 && pane.search.trim() !== '') return 'no tracks match the search';
		else if (visibleRows.length === 0 && pane.rows.length > 0 && uiPrefs.hide_broken_links)
			return 'all tracks in this list are broken links (hidden by Hide broken links)';
		else if (visibleRows.length === 0) return 'empty playlist';
		else return null;
	});

	onMount(() => {
		const url = new URL(window.location.href);
		if (url.searchParams.get('source') === 'spotify') {
			source = 'spotify';
			spotifySelectedId = url.searchParams.get('playlist');
		}
		const unsubscribeSearch = subscribeBrowserSearch((request) => {
			if (request.revision > 0) panes[activePane].search = request.query;
		});
		void _init();
		return unsubscribeSearch;
	});

	async function _init(): Promise<void> {
		playlistsLoading = true;
		playlistsError = null;
		try {
			const [healthRes, lists] = await Promise.all([getHealth(), listPlaylistsHydrated()]);
			allTracksCount = healthRes.health.state_db.tracks;
			playlists = lists;
			if (source === 'spotify' && spotifySelectedId !== null) {
				const selected = lists.find(
					(playlist) =>
						playlist.vendor === 'spotify' && playlist.playlist_id === spotifySelectedId
				);
				if (selected === undefined) {
					spotifyPendingError = `Spotify playlist ${spotifySelectedId} is not imported`;
				} else {
					_selectSpotifyPlaylist(selected, false);
				}
			}
		} catch (exc) {
			playlistsError = String(exc);
			pushToast(`browser init failed: ${String(exc)}`, 'error');
			throw exc;
		} finally {
			playlistsLoading = false;
		}
	}

	// -------------------------------------------------------- source modes

	function selectSpotifySource(): void {
		source = 'spotify';
		_writeSpotifyQuery(spotifySelectedId);
	}

	function selectCollectionSource(): void {
		source = 'collection';
		_writeCollectionQuery();
	}

	function selectSpotifyPlaylist(playlist: PlaylistSummaryHydrated): void {
		_selectSpotifyPlaylist(playlist, true);
	}

	function _selectSpotifyPlaylist(playlist: PlaylistSummaryHydrated, writeQuery: boolean): void {
		spotifySelectedId = playlist.playlist_id;
		if (writeQuery) _writeSpotifyQuery(playlist.playlist_id);
		const node: PlaylistNode = {
			playlist_id: playlist.playlist_id,
			name: playlist.name,
			track_count: playlist.track_count,
			kind: 'playlist',
			children: []
		};
		void _loadPane(panes[activePane], node);
		void _loadSpotifyPendingTracks(playlist.playlist_id);
	}

	async function _loadSpotifyPendingTracks(playlistId: string): Promise<void> {
		const sequence = ++spotifyPendingSequence;
		spotifyPendingTracks = null;
		spotifyPendingLoading = true;
		spotifyPendingError = null;
		try {
			const pending = await getSpotifyPendingTracks(playlistId);
			if (sequence !== spotifyPendingSequence) return;
			spotifyPendingTracks = pending;
		} catch (exc) {
			if (sequence !== spotifyPendingSequence) return;
			spotifyPendingError = String(exc);
		} finally {
			if (sequence === spotifyPendingSequence) spotifyPendingLoading = false;
		}
	}

	function _writeSpotifyQuery(playlistId: string | null): void {
		const url = new URL(window.location.href);
		url.searchParams.set('source', 'spotify');
		if (playlistId === null) url.searchParams.delete('playlist');
		else url.searchParams.set('playlist', playlistId);
		window.history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`);
	}

	function _writeCollectionQuery(): void {
		const url = new URL(window.location.href);
		url.searchParams.delete('source');
		url.searchParams.delete('playlist');
		window.history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`);
	}

	// ------------------------------------------------- pane playlist loading

	function selectPlaylist(node: PlaylistNode): void {
		void _loadPane(panes[activePane], node);
	}

	async function _loadPane(p: PaneStore, node: PlaylistNode): Promise<void> {
		// beginLoad returns the stale-response token for rapid re-selection;
		// completeLoad/failLoad no-op when a newer load superseded this one.
		const seq = p.beginLoad(node.playlist_id, node.name);
		try {
			const result =
				node.kind === 'all_tracks'
					? await _fetchAllRows()
					: await _fetchPlaylistRows(node.playlist_id);
			p.completeLoad(seq, result.rows, result.truncated, result.etag);
		} catch (exc) {
			p.failLoad(seq, String(exc));
			pushToast(`playlist load failed: ${String(exc)}`, 'error');
		}
	}

	/** Reconstructs the minimal PlaylistNode _loadPane needs to refresh the
	 * currently-selected pane after a mutation (add-remove-reorder-tracks). */
	function _currentNode(p: PaneStore): PlaylistNode | null {
		if (p.playlist_id === null || p.playlist_id === 'all') return null;
		return {
			playlist_id: p.playlist_id,
			name: p.title,
			track_count: p.rows.length,
			kind: 'playlist',
			children: []
		};
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
			revealed: false,
			match_context: null
		};
	}

	function _rowFromSearchHit(wire: SearchHitWire, order: number): BrowserRow {
		return { ..._rowFromPlaylistWire(wire, order), match_context: wire.match_context };
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
			revealed: false,
			match_context: null
		};
	}

	async function _fetchAllRows(): Promise<{ rows: BrowserRow[]; truncated: boolean; etag: string }> {
		// All Tracks: one page of MAX_ROWS with inline preview/file_exists
		// (contract 1). ?available stays server-default 'all': FR-1 hiding
		// is client-side so the toggle flips instantly on loaded panes; the
		// API filter exists for agent parity, not for this UI path.
		const page = await listTracksHydrated({ limit: MAX_ROWS });
		return {
			rows: page.items.map((t, i) => _rowFromListWire(t, i + 1)),
			truncated: page.next_cursor !== null,
			// All Tracks is not a single playlist row - no membership etag.
			etag: ''
		};
	}

	async function _fetchPlaylistRows(
		id: string
	): Promise<{ rows: BrowserRow[]; truncated: boolean; etag: string }> {
		// Hydrated detail (contract 4) + the playlist's membership ETag
		// (add-remove-reorder-tracks: required If-Match for the write side's
		// PUT .../tracks) in one request - the 29x per-row GET fan-out stays gone.
		const { detail, etag } = await getPlaylistTracksEtag(id);
		const rows = detail.tracks
			.slice(0, MAX_ROWS)
			.map((wire, i) => _rowFromPlaylistWire(wire, i + 1));
		return { rows, truncated: detail.tracks.length > MAX_ROWS, etag };
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
		// Backend refusals are persisted in the deck alert and surfaced as a
		// toast by the same dispatcher used by agent IPC.
		await runPerformanceCommandFromUi({ type: 'load', deck: target, stable_id: row.stable_id });
	}

	function _lowestFreeDeck(): DeckId | null {
		for (const d of DECK_IDS) {
			if (decks[d].stable_id === null) return d;
		}
		return null;
	}

	// ---------------------------------------------- client sort + search
	// The filter/sort pipeline itself lives in the pane contract
	// (filterRows/sortRows/visibleRowsOf) - lane members extend it there.

	function sortBy(key: SortKey): void {
		panes[activePane].toggleSort(key);
	}

	function selectRow(row: BrowserRow, event: MouseEvent): void {
		panes[activePane].select(row.stable_id, event.metaKey || event.ctrlKey);
	}

	function setSearch(next: string): void {
		panes[activePane].setSearch(next);
		if (!panes[activePane].whole_collection) return;
		const paneIndex = activePane;
		clearTimeout(_searchDebounce[paneIndex]);
		_searchDebounce[paneIndex] = setTimeout(
			() => void _searchWholeCollection(panes[paneIndex], next),
			SEARCH_DEBOUNCE_MS
		);
	}

	async function openEditModal(kind: 'bulk-edit' | 'find-replace' | 'mytag'): Promise<void> {
		if (kind !== 'mytag' && pane.selected_ids.length === 0) {
			pushToast('select at least one track first', 'error');
			return;
		}
		try {
			const etags: Record<string, string> = {};
			for (const stableId of pane.selected_ids) {
				const row = pane.rows.find((candidate) => candidate.stable_id === stableId);
				etags[stableId] = row?.etag ? row.etag : (await getTrack(stableId)).etag;
			}
			modalEtags = etags;
			openModal = kind;
		} catch (exc) {
			pushToast(`could not load selected track versions: ${String(exc)}`, 'error');
		}
	}

	function onEditApplied(): void {
		openModal = null;
		const node = pane.playlist_id === 'all'
			? { playlist_id: 'all', name: 'All Tracks', track_count: 0, kind: 'all_tracks' as const, children: [] }
			: treeNodes.find((candidate) => candidate.playlist_id === pane.playlist_id);
		if (node !== undefined) void _loadPane(pane, node);
	}

	const _searchDebounce: Record<number, ReturnType<typeof setTimeout>> = {};
	const SEARCH_DEBOUNCE_MS = 250;

	function setWholeCollection(next: boolean): void {
		const active = panes[activePane];
		active.setWholeCollection(next);
		if (next) void _searchWholeCollection(active, active.search);
	}

	async function _searchWholeCollection(active: PaneStore, query: string): Promise<void> {
		const trimmed = query.trim();
		if (trimmed === '') {
			active.search_results = [];
			active.search_total = 0;
			active.searching = false;
			return;
		}
		active.searching = true;
		try {
			const results = await searchCollection({ q: trimmed, limit: MAX_SEARCH_ROWS });
			if (!active.whole_collection || active.search.trim() !== trimmed) return;
			active.search_results = results.items.map((hit, index) => _rowFromSearchHit(hit, index + 1));
			active.search_total = results.total;
		} catch (exc) {
			if (active.whole_collection && active.search.trim() === trimmed) {
				active.search_results = [];
				active.search_total = 0;
				pushToast(`search failed: ${String(exc)}`, 'error');
			}
		} finally {
			if (active.search.trim() === trimmed) active.searching = false;
		}
	}

	// ------------------------------------- add / remove / reorder (write path)
	// editable: a real playlist is selected (not All Tracks / blank).
	// reorderable narrows further to the pane's natural membership order -
	// drag-and-drop moves row.order positions, which only lines up with the
	// visible row order when there is no client sort/search in effect.
	const editablePane = $derived(pane.playlist_id !== null && pane.playlist_id !== 'all');
	const reorderablePane = $derived(
		editablePane && pane.sort_key === null && pane.search.trim() === ''
	);

	async function _mutateActivePane(computeNext: (items: string[]) => string[]): Promise<void> {
		const p = pane;
		const id = p.playlist_id;
		if (id === null || id === 'all') return;
		if (p.etag === '') {
			pushToast('playlist still loading - try again in a moment', 'error');
			return;
		}
		const nextItems = computeNext(p.rows.map((r) => r.stable_id));
		try {
			await replacePlaylistTracks(id, p.etag, nextItems);
		} catch (exc) {
			if (exc instanceof PlaylistConflictError) {
				pushToast('playlist changed elsewhere - reloaded with the latest version', 'error');
			} else {
				pushToast(`playlist update failed: ${String(exc)}`, 'error');
				return;
			}
		}
		// Reload from the server rather than trust the optimistic local
		// splice: keeps hydrated fields (title/artist/etag/etc) and the
		// fresh membership etag authoritative in one place.
		const node = _currentNode(p);
		if (node !== null) await _loadPane(p, node);
	}

	function addTrack(stableId: string): void {
		void _mutateActivePane((items) => [...items, stableId]);
	}

	function removeRow(row: BrowserRow): void {
		// Positional removal: duplicate stable_ids are allowed in a
		// playlist, so this must drop the SLOT the row represents, not
		// every occurrence of that stable_id.
		void _mutateActivePane((items) => items.filter((_, i) => i !== row.order - 1));
	}

	function reorderRows(fromOrder: number, toOrder: number): void {
		void _mutateActivePane((items) => {
			const next = items.slice();
			const [moved] = next.splice(fromOrder - 1, 1);
			next.splice(toOrder - 1, 0, moved);
			return next;
		});
	}
</script>

<section class="rb-browser">
	<IconRail {source} onspotify={selectSpotifySource} />
	<div class="tree-panel">
		{#if source === 'spotify'}
			<SpotifySourcePanel
				playlists={spotifyPlaylists}
				playlistsLoading={playlistsLoading}
				playlistsError={playlistsError}
				selectedId={spotifySelectedId}
				pendingTracks={spotifyPendingTracks}
				loading={spotifyPendingLoading}
				error={spotifyPendingError}
				oncollection={selectCollectionSource}
				onselect={selectSpotifyPlaylist}
			/>
		{:else}
			<PlaylistTree
				nodes={treeNodes}
				{allTracksCount}
				selectedId={pane.playlist_id}
				onselect={selectPlaylist}
			/>
		{/if}
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
				<label
					class="whole-collection"
					title="Search the whole collection with server-side FTS5 instead of only this pane"
				>
					<input
						type="checkbox"
						checked={pane.whole_collection}
						onchange={(event) => setWholeCollection(event.currentTarget.checked)}
					/>
					<span>Whole collection</span>
				</label>
				{#if editablePane}
					<AddTrackSearch onadd={addTrack} />
				{/if}
				<SearchBox value={pane.search} oninput={setSearch} />
				<button class="rb-lit-button" disabled={pane.selected_ids.length === 0} onclick={() => void openEditModal('find-replace')}>Find &amp; Replace</button>
				<button class="rb-lit-button" disabled={pane.selected_ids.length === 0} onclick={() => void openEditModal('bulk-edit')}>Bulk Edit</button>
				<button class="rb-lit-button" onclick={() => void openEditModal('mytag')}>MyTags</button>
			</div>
		</div>
		<TrackTable
			{provider}
			selectedIds={pane.selected_ids}
			{loadedIds}
			{vocalsById}
			sortKey={pane.sort_key}
			sortDir={pane.sort_dir}
			{emptyMessage}
			restoreKey={activePane}
			scrollTop={pane.scroll_top}
			removable={editablePane}
			reorderable={reorderablePane}
			onscrollcursor={(top) => panes[activePane].rememberScroll(top)}
			onsort={sortBy}
			onselectrow={selectRow}
			onloadrow={loadRow}
			onrate={rateRow}
			onrowvisible={rowVisible}
			onremoverow={removeRow}
			onreorder={reorderRows}
		/>
		<!-- dj_copilot suggest-next strip: keyed to the deck-1-loaded track
		     (recommended mount point, see SuggestNextStrip.svelte). -->
		<SuggestNextStrip stableId={decks[1].stable_id} />
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

{#if openModal === 'find-replace'}
	<FindReplaceModal stableIds={pane.selected_ids} etags={modalEtags} onclose={() => (openModal = null)} onapplied={onEditApplied} />
{:else if openModal === 'bulk-edit'}
	<BulkEditModal stableIds={pane.selected_ids} etags={modalEtags} onclose={() => (openModal = null)} onapplied={onEditApplied} />
{:else if openModal === 'mytag'}
	<MyTagEditorModal stableIds={pane.selected_ids} etags={modalEtags} onclose={() => (openModal = null)} onapplied={onEditApplied} />
{/if}

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
	.whole-collection {
		display: inline-flex;
		align-items: center;
		gap: 3px;
		color: var(--rb-text-dim);
		font-size: var(--rb-fs-label);
		white-space: nowrap;
		cursor: pointer;
	}
	.whole-collection:hover {
		color: var(--rb-text);
	}
	.whole-collection input {
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
