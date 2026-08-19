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
	import { getConnectionState, subscribeKind, subscribeResync } from '$lib/api/events-bus';
	import { api, unwrap } from '$lib/api/client';
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
		parseStemSummary,
		parseVocals,
		vocalsOf
	} from '$lib/rb/api-rb';
	import type {
		PlaylistSummaryHydrated,
		PlaylistTrackRowWire,
		SearchHitWire,
		TrackListItemWire,
		Vocals
	} from '$lib/rb/api-rb';
	import type { DeckId, PlaylistNode, RbMeta } from '$lib/rb/types';
	// Deck state remains engine-owned; real load interactions route through
	// the same validated dispatcher exposed to browser agents.
	import { deckStates as decks, DECK_IDS } from '$lib/rb/audio-engine.svelte';
	import { resolveRowVocals } from '$lib/rb/row-vocals';
	import {
		ANALYSIS_COLORS,
		jobProgress
	} from '$lib/rb/job-progress.svelte';
	import {
		dispatchPerformanceCommand,
		runPerformanceCommandFromUi
	} from '$lib/rb/performance-ipc.svelte';
	import {
		BLANK_PLAYLIST_GRACE_MS,
		DEFAULT_PLAYLIST_NAME,
		clearPlaylistCreateGrace,
		collectBlankPlaylistDeletes,
		markPlaylistCreateGrace
	} from '$lib/rb/playlist-blank';
	import {
		createPlaylist,
		deletePlaylist,
		getPlaylistTracksEtag,
		PlaylistConflictError,
		renamePlaylist,
		replacePlaylistTracks
	} from '$lib/rb/playlist-write';
	import {
		hydrateConfirmPrefsFromDisk,
		setConfirmPref,
		setHideBrokenLinks,
		setLibraryDensity,
		setNextOnlyFilter,
		uiPrefs
	} from '$lib/rb/prefs.svelte';
	import { isAppropriateNext, type NextOnlyRef } from '$lib/rb/next-only-filter';
	import { pushToast } from '$lib/stores.svelte';
	import { subscribeBrowserSearch } from '$lib/rb/browser-search';
	import RecommendedSection from './RecommendedSection.svelte';
	import SuggestNextStrip from './SuggestNextStrip.svelte';
	import { setAutoPlayTrackFeed } from '$lib/rb/auto-play';
	import BulkEditModal from './BulkEditModal.svelte';
	import FindReplaceModal from './FindReplaceModal.svelte';
	import MyTagEditorModal from './MyTagEditorModal.svelte';
	import AddTrackSearch from './browser/AddTrackSearch.svelte';
	import IconRail from './browser/IconRail.svelte';
	import PaneTabs from './browser/PaneTabs.svelte';
	import type { PaneTabInfo } from './browser/PaneTabs.svelte';
	import {
		canMutatePlaylist,
		createPaneStore,
		filterRows,
		makeClientRowProvider,
		reorderPanesInPlace,
		resolveNewTabIndex,
		sortRows,
		visibleRowsOf
	} from './browser/pane-contract.svelte';
	import type {
		BrowserRow,
		PaneStore,
		PlaylistDragPayload,
		SortKey
	} from './browser/pane-contract.svelte';
	import PlaylistTree from './browser/PlaylistTree.svelte';
	import SearchBox from './browser/SearchBox.svelte';
	import TrackTable from './browser/TrackTable.svelte';
	import { fetchAllPages } from './browser/virtual-window';
	import { ensureAudioPrefetch } from '$lib/rb/audio-prefetch-cache.svelte';
	import { ensureAnlz, getAnlzEntry } from './wave/anlz-cache.svelte';
	import { getSpotifyPendingTracks, type SpotifyPendingTrack } from '$lib/rb/spotify-api';
	import SpotifySourcePanel from './browser/SpotifySourcePanel.svelte';

	// track-list-virtualization: TrackTable now DOM-virtualizes its render,
	// so panes no longer cap fetches at 500 rows - All Tracks walks every
	// cursor page (PAGE_SIZE is a per-request page size, not a result cap);
	// playlists were never fetch-capped (getPlaylistHydrated already
	// returns the full membership in one call), only client-sliced - that
	// slice is gone too (see _fetchPlaylistRows).
	const PAGE_SIZE = 500;
	// Whole-collection FTS5 search stays hard-capped (unrelated to the
	// fetch-cap removal above): a global text query over the whole library
	// is a separate, ranked result set, not a browsable pane listing.
	const MAX_SEARCH_ROWS = 200;
	/** Spike: hide tree playlists when fewer than 30% of tracks are on disk.
	 * Move to BE/config later. */
	const HIDE_BROKEN_PLAYLIST_MIN_AVAILABLE_RATIO = 0.3;

	function playlistMostlyBroken(p: PlaylistSummaryHydrated): boolean {
		if (p.track_count === 0) return false;
		return p.available_count / p.track_count < HIDE_BROKEN_PLAYLIST_MIN_AVAILABLE_RATIO;
	}

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
	/** Brief unload affordance after a load blocked by an active deck. */
	let unloadOffer = $state<{ deck: DeckId; until: number } | null>(null);
	let unloadOfferTimer: ReturnType<typeof setTimeout> | null = null;
	/** Bottom-left connectivity: lib = state.db tracks, BE = API, FE = Vite. */
	let libUp = $state(false);
	let beUp = $state(false);
	let feUp = $state(false);
	/** Suggest-next hover → temporary table scroll/highlight. */
	let suggestHoverId = $state<string | null>(null);
	/** Candidates for Recommended (outside-playlist) grouping. */
	let suggestCandidates = $state<
		Array<{
			stable_id: string;
			title: string | null;
			artist: string | null;
			bpm: number | null;
			key_camelot: string | null;
			energy: number | null;
			rating: number | null;
			rationale_tags: string[];
			explain_text: string | null;
		}>
	>([]);

	/** Library back stack: playlist + selection + scroll + search. */
	type NavSnap = {
		playlist_id: string | null;
		playlist_name: string;
		selected_id: string | null;
		scroll_top: number;
		search: string;
	};
	let navHistory = $state<NavSnap[]>([]);
	let navEpoch = $state(0);
	let _navRestoring = false;
	/** Genre filter undo + 20s library gesture window. */
	let genreFilterPrior = $state('');
	let genreFilterUntil = $state(0);
	const GENRE_WINDOW_MS = 20_000;

	/** Cmd+F filter / Cmd+FF find / Cmd+Shift+F collection. */
	type SearchMode = 'filter' | 'find' | 'collection';
	let searchMode = $state<SearchMode>('filter');
	let searchFocusToken = $state(0);
	let searchFocused = $state(false);
	/** Snapshot before search so Esc/X returns to prior place. */
	let searchReturnSnap = $state<NavSnap | null>(null);

	const pane = $derived(panes[activePane]);
	const spotifyPlaylists = $derived(playlists.filter((playlist) => playlist.vendor === 'spotify'));
	const loadedIds = $derived(
		new Set(DECK_IDS.map((d) => decks[d].stable_id).filter((v): v is string => v !== null))
	);
	// Vocals for PreviewStrip blue bars: listing hydrate (row.vocals) is
	// the base; loaded-deck / client anlz overwrite only when analyzed.
	// Strips never fan-out /anlz themselves.
	const vocalsById = $derived.by((): Record<string, Vocals> =>
		resolveRowVocals({
			rows: pane.rows,
			deckVocals: DECK_IDS.flatMap((d) => {
				const st = decks[d];
				if (st.stable_id === null || st.anlz === null) return [];
				return [{ stable_id: st.stable_id, vocals: vocalsOf(st.anlz) }];
			}),
			// getAnlzEntry is a PURE read - swapping it for ensureAnlz here is the
			// per-row fan-out this contract exists to prevent.
			cachedVocals: (stable_id: string): Vocals | undefined => {
				const entry = getAnlzEntry(stable_id);
				return entry !== undefined && entry.status === 'ready' ? vocalsOf(entry.data) : undefined;
			}
		})
	);
	const treeNodes = $derived(
		playlists
			.slice()
			// FR-1: with 'Hide broken links' ON, playlists with fewer than
			// 30% available (on-disk) tracks vanish from the tree.
			.filter((p) => !uiPrefs.hide_broken_links || !playlistMostlyBroken(p))
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
					mostly_broken: playlistMostlyBroken(p),
					children: []
				})
			)
	);
	const tabs = $derived(
		panes.map(
			(p): PaneTabInfo => ({
				title: p.playlist_id === null ? 'blank list' : p.title,
				count: p.playlist_id === null || p.loading ? null : p.rows.length,
				loading: p.loading,
				sticky: p.sticky,
				ephemeral: p.playlist_id === null
			})
		)
	);
	const wholeCollectionActive = $derived(
		searchMode === 'collection' &&
			pane.whole_collection &&
			pane.search.trim() !== '' &&
			!/^genre:/i.test(pane.search.trim())
	);
	const findHighlightQuery = $derived(
		searchMode === 'find' && pane.search.trim().length >= 3 ? pane.search.trim() : ''
	);

	/** Reference for next-only: master, else playing loaded, else any loaded with key+BPM. */
	const nextOnlyRef = $derived.by((): NextOnlyRef | null => {
		const states = DECK_IDS.map((d) => decks[d]);
		const ordered = [
			...states.filter((s) => s.is_master && s.stable_id !== null),
			...states.filter((s) => s.playing && s.stable_id !== null),
			...states.filter((s) => s.stable_id !== null)
		];
		for (const s of ordered) {
			if (s.key !== null && s.bpm !== null && s.bpm > 0) {
				return { key: s.key, bpm: s.bpm };
			}
		}
		return null;
	});

	function _applyNextOnly(rows: BrowserRow[]): BrowserRow[] {
		if (!uiPrefs.next_only_filter) return rows;
		const ref = nextOnlyRef;
		if (ref === null) return rows;
		return rows.filter((r) => isAppropriateNext(r, ref));
	}

	const visibleRows = $derived.by(() => {
		// Find mode: keep full list (no filter); TrackTable highlights matches.
		if (searchMode === 'find') {
			return _applyNextOnly(
				sortRows(
					filterRows(pane.rows, '', uiPrefs.hide_broken_links),
					pane.sort_key,
					pane.sort_dir
				)
			);
		}
		if (wholeCollectionActive) {
			return _applyNextOnly(sortRows(pane.search_results, pane.sort_key, pane.sort_dir));
		}
		return _applyNextOnly(visibleRowsOf(pane, uiPrefs.hide_broken_links));
	});
	// Read contract handed to TrackTable (getters stay reactive through
	// visibleRows/pane). The virtualization lane replaces THIS provider,
	// not TrackTable's props.
	const provider = makeClientRowProvider(
		() => visibleRows,
		() => pane.truncated || (wholeCollectionActive && pane.search_total > MAX_SEARCH_ROWS)
	);
	const searchPlaceholder = $derived(
		searchMode === 'find'
			? 'Find in list (highlight, 3+ chars)'
			: searchMode === 'collection'
				? 'Search whole collection'
				: 'Search within this track list'
	);
	const emptyMessage = $derived.by((): string | null => {
		if (searchMode === 'find') {
			if (pane.loading) return 'loading...';
			else if (pane.error !== null) return `load failed: ${pane.error}`;
			else if (pane.playlist_id === null) return 'blank list - choose a playlist in the tree';
			else if (visibleRows.length === 0) return 'empty playlist';
			else return null;
		}
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
		else if (
			visibleRows.length === 0 &&
			pane.rows.length > 0 &&
			uiPrefs.next_only_filter
		) {
			return nextOnlyRef === null
				? 'next-only: load a track with key+BPM (master preferred) to filter'
				: 'no appropriate next tracks in this list (Camelot + BPM ±6% or half/double ≤15)';
		} else if (visibleRows.length === 0) return 'empty playlist';
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
		const onKey = (e: KeyboardEvent): void => {
			if (!(e.metaKey || e.ctrlKey) || e.altKey) return;
			if (e.key !== 'f' && e.key !== 'F') return;
			// Don't steal from text fields outside the browser search box.
			const t = e.target;
			if (t instanceof HTMLElement) {
				const tag = t.tagName;
				const inSearch = t.closest('.rb-search') !== null;
				if (
					!inSearch &&
					(tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || t.isContentEditable)
				) {
					return;
				}
			}
			e.preventDefault();
			if (e.shiftKey) openSearchMode('collection');
			else if (searchFocused && searchMode === 'filter') openSearchMode('find');
			else openSearchMode('filter');
		};
		window.addEventListener('keydown', onKey);
		void _init();
		void hydrateConfirmPrefsFromDisk();
		let connAlive = true;
		const pingConn = async (): Promise<void> => {
			const [health, fe] = await Promise.all([_pingHealth(), _pingFe()]);
			if (!connAlive) return;
			beUp = health.be;
			libUp = health.lib;
			feUp = fe;
		};
		void pingConn();
		const connTimer = setInterval(() => void pingConn(), 2500);
		const blankSweepTimer = setInterval(
			() => void _sweepBlankPlaylists(),
			BLANK_PLAYLIST_GRACE_MS
		);

		// ---- library listing freshness -------------------------------------
		// FAST PATH: the engine tells us the moment a track changes, from any
		// writer (this tab, another tab, the CLI, an agent). One targeted
		// refetch, no idle polling.
		const unsubscribeTracks = subscribeKind('tracks', () => void _refreshLibraryRows());
		// A resync means the bus knows it missed events but not which, so the
		// only sound response is to refetch as if everything changed.
		const unsubscribeResync = subscribeResync(() => void _refreshLibraryRows());
		// DEGRADED PATH: the poll is deliberately kept, not deleted. When the
		// WS is down (daemon restarting, engine built without the hub) it is
		// the only thing keeping this pane honest. It is 60s rather than
		// aggressive because it is a fallback, and it stands down entirely
		// while the bus is open, where it would only duplicate work the
		// subscriptions above already do on demand.
		const LIBRARY_FALLBACK_POLL_MS = 60_000;
		const libraryFallbackTimer = setInterval(() => {
			if (getConnectionState() === 'open') return;
			void _refreshLibraryRows();
		}, LIBRARY_FALLBACK_POLL_MS);

		return () => {
			connAlive = false;
			clearInterval(connTimer);
			clearInterval(blankSweepTimer);
			clearInterval(libraryFallbackTimer);
			unsubscribeTracks();
			unsubscribeResync();
			unsubscribeSearch();
			window.removeEventListener('keydown', onKey);
		};
	});

	async function _pingHealth(): Promise<{ be: boolean; lib: boolean }> {
		try {
			const body = await unwrap(
				api.GET('/api/v1/health', {
					cache: 'no-store',
					signal: AbortSignal.timeout(2000)
				})
			);
			return { be: true, lib: (body.state_db?.tracks ?? 0) > 0 };
		} catch {
			return { be: false, lib: false };
		}
	}

	async function _pingFe(): Promise<boolean> {
		try {
			const r = await fetch(`${window.location.origin}/`, {
				method: 'GET',
				cache: 'no-store',
				signal: AbortSignal.timeout(2000)
			});
			return r.ok;
		} catch {
			return false;
		}
	}

	async function _init(): Promise<void> {
		playlistsLoading = true;
		playlistsError = null;
		try {
			const [healthRes, lists] = await Promise.all([getHealth(), listPlaylistsHydrated()]);
			allTracksCount = healthRes.health.state_db.tracks;
			playlists = lists;
			await _sweepBlankPlaylists(lists);
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

	function _navSnap(): NavSnap {
		const p = panes[activePane];
		return {
			playlist_id: p.playlist_id,
			playlist_name: p.title,
			selected_id: p.selected_id,
			scroll_top: p.scroll_top,
			search: p.search
		};
	}

	function _pushNav(): void {
		if (_navRestoring) return;
		const snap = _navSnap();
		const last = navHistory.length > 0 ? navHistory[navHistory.length - 1] : null;
		if (
			last !== null &&
			last.playlist_id === snap.playlist_id &&
			last.selected_id === snap.selected_id &&
			last.search === snap.search &&
			Math.abs(last.scroll_top - snap.scroll_top) < 8
		) {
			return;
		}
		navHistory = [...navHistory.slice(-39), snap];
	}

	function _nodeForNav(snap: NavSnap): PlaylistNode | null {
		if (snap.playlist_id === null) return null;
		if (snap.playlist_id === 'all') {
			return {
				playlist_id: 'all',
				name: 'All Tracks',
				track_count: allTracksCount ?? 0,
				kind: 'all_tracks',
				children: []
			};
		}
		const found = treeNodes.find((n) => n.playlist_id === snap.playlist_id);
		if (found !== undefined) return found;
		return {
			playlist_id: snap.playlist_id,
			name: snap.playlist_name,
			track_count: 0,
			kind: 'playlist',
			children: []
		};
	}

	async function goBack(): Promise<void> {
		if (navHistory.length === 0) return;
		const snap = navHistory[navHistory.length - 1];
		navHistory = navHistory.slice(0, -1);
		_navRestoring = true;
		try {
			const p = panes[activePane];
			if (snap.playlist_id !== p.playlist_id) {
				const node = _nodeForNav(snap);
				if (node !== null) await _loadPane(p, node);
			}
			p.setSearch(snap.search);
			if (snap.selected_id !== null) p.select(snap.selected_id, false);
			else {
				p.selected_id = null;
				p.selected_ids = [];
			}
			p.rememberScroll(snap.scroll_top);
			navEpoch += 1;
			genreFilterUntil = /^genre:/i.test(snap.search) ? Date.now() + GENRE_WINDOW_MS : 0;
		} finally {
			_navRestoring = false;
		}
	}

	function selectPlaylist(node: PlaylistNode, opts?: { newTab?: boolean }): void {
		const forceNew = opts?.newTab === true || panes[activePane].sticky;
		if (!forceNew) {
			if (panes[activePane].playlist_id === node.playlist_id) return;
			_pushNav();
			void _loadPane(panes[activePane], node);
			return;
		}
		_openPlaylistInNewTab(node);
	}

	function _openPlaylistInNewTab(node: PlaylistNode): void {
		const existing = panes.findIndex((p) => p.playlist_id === node.playlist_id);
		if (existing >= 0) {
			activePane = existing;
			return;
		}
		const target = resolveNewTabIndex(panes);
		if (target === null) {
			pushToast(
				'ALL 4 LIBRARY TABS ARE LOCKED - unlock one (or free a non-sticky tab) before opening another playlist',
				'error'
			);
			return;
		}
		if (panes[target].playlist_id === node.playlist_id) {
			activePane = target;
			return;
		}
		activePane = target;
		void _loadPane(panes[target], node);
	}

	function dropPlaylistOnTabBar(payload: PlaylistDragPayload): void {
		const node: PlaylistNode = {
			playlist_id: payload.playlist_id,
			name: payload.name,
			track_count: payload.track_count,
			kind: payload.kind,
			children: []
		};
		_openPlaylistInNewTab(node);
	}

	function togglePaneSticky(index: number): void {
		if (index < 0 || index >= panes.length) return;
		panes[index].sticky = !panes[index].sticky;
	}

	function reorderPaneTabs(from: number, to: number): void {
		activePane = reorderPanesInPlace(panes, from, to, activePane);
	}

	async function saveAsPlaylistUi(index: number): Promise<void> {
		const p = panes[index];
		if (p.playlist_id !== null) return;
		const name = window.prompt('Save blank list as playlist');
		if (name === null || name.trim() === '') return;
		try {
			const created = await createPlaylist(name.trim());
			await _refreshPlaylists();
			const stableIds = p.rows.map((r) => r.stable_id);
			if (stableIds.length > 0) {
				const { etag } = await getPlaylistTracksEtag(created.playlist_id);
				await replacePlaylistTracks(created.playlist_id, etag, stableIds);
			}
			activePane = index;
			await _loadPane(p, {
				playlist_id: created.playlist_id,
				name: created.name,
				track_count: stableIds.length,
				kind: 'playlist',
				children: []
			});
			pushToast(`Saved as playlist "${created.name}"`, 'info');
		} catch (exc) {
			pushToast(`save as playlist failed: ${String(exc)}`, 'error');
		}
	}

	async function _refreshPlaylists(): Promise<void> {
		playlists = await listPlaylistsHydrated();
		await _sweepBlankPlaylists(playlists);
	}

	/**
	 * Re-read the library listing WITHOUT disturbing the user: the all-tracks
	 * count plus every loaded pane's rows, replaced in place so selection and
	 * scroll survive.
	 *
	 * Deliberately NOT routed through _loadPane: beginLoad() clears
	 * selected_id, selected_ids and scroll_top, which is correct for a
	 * user-initiated playlist switch and destructive for a background
	 * invalidation. Editing one track's rating must not scroll the pane back
	 * to the top and drop a 40-row multi-selection.
	 *
	 * Failures log rather than toast: this runs unattended (WS events and a
	 * 60s timer), so a toast per failure during a daemon restart would bury
	 * the UI in noise. The stale rows stay on screen and the next event or
	 * tick retries.
	 */
	async function _refreshLibraryRows(): Promise<void> {
		try {
			const healthRes = await getHealth();
			allTracksCount = healthRes.health.state_db.tracks;
		} catch (exc) {
			console.error(`[library-refresh] track count refresh failed: ${String(exc)}`);
		}
		for (const p of panes) {
			// A blank pane has nothing to refresh, and a pane mid-load already
			// has a newer load token that owns its rows.
			if (p.playlist_id === null || p.loading) continue;
			try {
				const result =
					p.playlist_id === 'all'
						? await _fetchAllRows()
						: await _fetchPlaylistRows(p.playlist_id);
				p.rows = result.rows;
				p.truncated = result.truncated;
				p.etag = result.etag;
				// A selection pointing at a row the change deleted cannot
				// survive; everything still present stays selected.
				const present = new Set(result.rows.map((row) => row.stable_id));
				p.selected_ids = p.selected_ids.filter((id) => present.has(id));
				if (p.selected_id !== null && !present.has(p.selected_id)) p.selected_id = null;
			} catch (exc) {
				console.error(`[library-refresh] pane ${p.playlist_id} refresh failed: ${String(exc)}`);
			}
		}
	}

	/** Auto-delete blank untitled empties (never renamed / non-empty). */
	async function _sweepBlankPlaylists(
		lists: typeof playlists = playlists
	): Promise<void> {
		const toDelete = collectBlankPlaylistDeletes(lists);
		if (toDelete.length === 0) return;
		let deletedAny = false;
		for (const playlistId of toDelete) {
			const row = lists.find((p) => p.playlist_id === playlistId);
			if (row === undefined) continue;
			try {
				const { etag } = await getPlaylistTracksEtag(playlistId);
				await deletePlaylist(playlistId, etag);
				clearPlaylistCreateGrace(playlistId);
				deletedAny = true;
			} catch (exc) {
				console.info(
					`[playlist-blank] delete failed ${playlistId}: ${String(exc)}`
				);
			}
		}
		if (deletedAny) {
			playlists = await listPlaylistsHydrated();
		}
	}

	/** '+' create: default name + grace, return id for in-place rename focus. */
	async function createPlaylistUi(): Promise<string | null> {
		try {
			const created = await createPlaylist(DEFAULT_PLAYLIST_NAME);
			markPlaylistCreateGrace(created.playlist_id);
			// Refresh without sweeping away the brand-new blank (still in grace).
			playlists = await listPlaylistsHydrated();
			return created.playlist_id;
		} catch (exc) {
			pushToast(`create playlist failed: ${String(exc)}`, 'error');
			return null;
		}
	}

	async function renamePlaylistUi(node: PlaylistNode, name: string): Promise<void> {
		if (node.kind === 'all_tracks' || node.playlist_id === 'all') return;
		const next = name.trim();
		if (next === '' || next === node.name) return;
		try {
			const { etag } = await getPlaylistTracksEtag(node.playlist_id);
			await renamePlaylist(node.playlist_id, etag, next);
			clearPlaylistCreateGrace(node.playlist_id);
			await _refreshPlaylists();
			pushToast(`Renamed to "${next}"`, 'info');
		} catch (exc) {
			pushToast(`rename failed: ${String(exc)}`, 'error');
		}
	}

	async function deletePlaylistUi(node: PlaylistNode): Promise<void> {
		if (node.kind === 'all_tracks' || node.playlist_id === 'all') return;
		const skip = uiPrefs.confirm.delete_playlist === false;
		if (!skip) {
			const every = window.confirm(`Delete playlist "${node.name}"?`);
			if (!every) return;
			const remember = window.confirm('Do this every time (skip delete confirm)?');
			if (remember) setConfirmPref('delete_playlist', false);
		}
		try {
			const { etag } = await getPlaylistTracksEtag(node.playlist_id);
			await deletePlaylist(node.playlist_id, etag);
			await _refreshPlaylists();
			pushToast(`Deleted playlist "${node.name}"`, 'info');
		} catch (exc) {
			pushToast(`delete failed: ${String(exc)}`, 'error');
		}
	}

	async function dropTracksOnPlaylist(playlistId: string, stableIds: string[]): Promise<void> {
		const remembered = uiPrefs.confirm.playlist_drop_mode;
		let mode: 'add' | 'move' | null = remembered ?? null;
		if (mode === null) {
			const add = window.confirm(
				`Drop ${stableIds.length} track(s) onto playlist.\n\nOK = Add\nCancel = choose Move`
			);
			mode = add ? 'add' : 'move';
			const remember = window.confirm(`Remember "${mode}" every time for playlist drops?`);
			if (remember) setConfirmPref('playlist_drop_mode', mode);
		}
		try {
			const dest = await getPlaylistTracksEtag(playlistId);
			const destIds = dest.detail.tracks.map((t) => t.stable_id);
			const merged = [...destIds];
			for (const id of stableIds) {
				if (!merged.includes(id)) merged.push(id);
			}
			await replacePlaylistTracks(playlistId, dest.etag, merged);
			if (mode === 'move') {
				const srcId = panes[activePane].playlist_id;
				if (srcId !== null && srcId !== playlistId && canMutatePlaylist(panes[activePane])) {
					const src = await getPlaylistTracksEtag(srcId);
					const next = src.detail.tracks
						.map((t) => t.stable_id)
						.filter((id) => !stableIds.includes(id));
					await replacePlaylistTracks(srcId, src.etag, next);
					const node = _currentNode(panes[activePane]);
					if (node !== null) await _loadPane(panes[activePane], node);
				}
			}
			await _refreshPlaylists();
			pushToast(`${mode === 'add' ? 'Added' : 'Moved'} ${stableIds.length} track(s)`, 'info');
		} catch (exc) {
			pushToast(`playlist drop failed: ${String(exc)}`, 'error');
		}
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
			quality: wire.quality ?? null,
			play_count: typeof wire.play_count === 'number' ? wire.play_count : 0,
			strip: decodePreviewStrip(wire.preview_b64, wire.preview_max),
			vocals: parseVocals(wire.vocals),
			stems: parseStemSummary(wire.stems),
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
			quality: track.quality ?? null,
			play_count: typeof track.play_count === 'number' ? track.play_count : 0,
			strip: decodePreviewStrip(track.preview_b64, track.preview_max),
			vocals: parseVocals(track.vocals),
			stems: parseStemSummary(track.stems),
			rb_meta: null,
			revealed: false,
			match_context: null
		};
	}

	async function _fetchAllRows(): Promise<{ rows: BrowserRow[]; truncated: boolean; etag: string }> {
		// All Tracks: walk every cursor page with inline preview/file_exists
		// (contract 1). PAGE_SIZE is per request, while TrackTable's DOM
		// virtualization keeps the rendered pane bounded. ?available stays
		// server-default 'all': FR-1 hiding is client-side so the toggle
		// flips instantly on loaded panes; the API filter exists for agent
		// parity, not for this UI path.
		const items = await fetchAllPages((cursor) => listTracksHydrated({ limit: PAGE_SIZE, cursor }));
		return {
			rows: items.map((t, i) => _rowFromListWire(t, i + 1)),
			truncated: false,
			// All Tracks is not a single playlist row - no membership etag.
			etag: ''
		};
	}

	async function _fetchPlaylistRows(
		id: string
	): Promise<{ rows: BrowserRow[]; truncated: boolean; etag: string }> {
		// Hydrated detail (contract 4) + the playlist's membership ETag
		// (add-remove-reorder-tracks: required If-Match for the write side's
		// PUT .../tracks) in one request. Never client-slice membership:
		// TrackTable's DOM virtualization keeps rendering cheap instead.
		const { detail, etag } = await getPlaylistTracksEtag(id);
		const rows = detail.tracks.map((wire, i) => _rowFromPlaylistWire(wire, i + 1));
		return { rows, truncated: false, etag };
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
			row.rb_meta = await _fetchRbMetaWithRetry(row.stable_id);
		} catch (exc) {
			if (exc instanceof RbApiError && exc.status === 404) {
				// No rekordbox vendor mapping for this track - a real library
				// state: no meta, no artwork.
				return;
			}
			// Loud but non-modal: a toast per row would spam during scrolling.
			// Transient fails leave rb_meta null; rowVisible can retry later
			// when the row re-enters the observer (inflight cleared).
			console.error(`rb-meta hydration failed for ${row.stable_id}:`, exc);
		} finally {
			_inflight.delete(row.stable_id);
		}
	}

	/** One retry on non-404 failure so a blip does not leave the row art-dead. */
	async function _fetchRbMetaWithRetry(stable_id: string): Promise<RbMeta> {
		try {
			return await fetchRbMeta(stable_id);
		} catch (exc) {
			if (exc instanceof RbApiError && exc.status === 404) throw exc;
			await new Promise((r) => setTimeout(r, 250));
			return await fetchRbMeta(stable_id);
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
	// Narrowed to exactly the fields deck-load needs (not the full
	// BrowserRow) so the column-view lane's lighter ColumnTrackRow can
	// reuse this same dispatcher without depending on table-only fields
	// (strip/rb_meta/revealed) it never has.

	type LoadableRow = Pick<BrowserRow, 'stable_id' | 'file_exists' | 'is_streaming'> & {
		rb_meta?: BrowserRow['rb_meta'];
	};

	function loadRow(
		row: LoadableRow,
		deck: DeckId | null,
		opts: { play?: boolean } = {}
	): void {
		void _loadOntoDeck(row, deck, opts);
	}

	function loadSuggest(sid: string, opts: { play?: boolean } = {}): void {
		const row = { stable_id: sid, file_exists: true, is_streaming: false };
		if (opts.play) {
			const deck = pickDoubleDeck(row);
			if (deck === null) return;
			loadRow(row, deck, { play: true });
			return;
		}
		loadRow(row, null);
	}

	const playlistMemberIds = $derived(new Set(pane.rows.map((r) => r.stable_id)));
	const masterRef = $derived(
		DECK_IDS.map((d) => decks[d]).find((d) => d.is_master) ?? null
	);

	// Feed AutoPlay: open playlist membership with key/BPM + disk truth.
	// Include broken rows so pick can skip them; never invent file_exists.
	$effect(() => {
		setAutoPlayTrackFeed(
			pane.rows.map((r) => ({
				stable_id: r.stable_id,
				key: r.key,
				bpm: r.bpm,
				file_exists: r.file_exists
			}))
		);
	});

	/** Monotonic load counter - double-click prefers least-recent in the pair. */
	let deckLoadSeq = $state({ 1: 0, 2: 0, 3: 0, 4: 0 });
	let deckLoadTick = 0;

	/**
	 * Smart-load target: CH1/CH2 by default (least-recent).
	 * Shift: CH3/CH4 only if empty or stopped (empty preferred); null if both playing.
	 */
	function pickDoubleDeck(_row: LoadableRow, opts: { shift?: boolean } = {}): DeckId | null {
		if (opts.shift !== true) {
			return deckLoadSeq[1] <= deckLoadSeq[2] ? 1 : 2;
		}
		const pair: Array<3 | 4> = [3, 4];
		const empty = pair.filter((d) => decks[d].stable_id === null);
		const stopped = pair.filter((d) => decks[d].stable_id !== null && !decks[d].playing);
		const candidates = empty.length > 0 ? empty : stopped;
		if (candidates.length === 0) {
			pushToast('shift+dblclick: CH3 and CH4 are both playing - pause or unload one first', 'error');
			return null;
		}
		if (candidates.length === 1) return candidates[0];
		return deckLoadSeq[candidates[0]] <= deckLoadSeq[candidates[1]]
			? candidates[0]
			: candidates[1];
	}

	function previewSeek(row: LoadableRow, ratio: number): void {
		const r = Math.max(0, Math.min(1, ratio));
		const targets = DECK_IDS.filter((d) => decks[d].stable_id === row.stable_id);
		if (targets.length === 0) {
			pushToast(
				'preview seek: track not on a deck (headphone cue not implemented - see PARITY-TODO)',
				'error'
			);
			return;
		}
		for (const deck of targets) {
			const dur = decks[deck].duration_ms;
			if (dur === null || dur <= 0) continue;
			void runPerformanceCommandFromUi({
				type: 'seek',
				deck,
				position_ms: Math.round(r * dur)
			});
		}
	}

	async function _loadOntoDeck(
		row: LoadableRow,
		deck: DeckId | null,
		opts: { play?: boolean } = {}
	): Promise<void> {
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
			// Explicit CH load (incl. confirmed double-click): replace if occupied.
			if (deck !== null && decks[target].stable_id !== null) {
				await dispatchPerformanceCommand({ type: 'unload', deck: target });
			}
			await dispatchPerformanceCommand({ type: 'load', deck: target, stable_id: row.stable_id });
			deckLoadTick += 1;
			deckLoadSeq = { ...deckLoadSeq, [target]: deckLoadTick };
			if (opts.play === true) {
				await dispatchPerformanceCommand({ type: 'play', deck: target, playing: true });
			}
		} catch (error: unknown) {
			const message = error instanceof Error ? error.message : String(error);
			if (message.includes('must be fully stopped before replacement')) {
				_offerUnload(target);
			}
			// Dispatcher already toasted + recorded the deck alert.
		}
	}

	function _offerUnload(deck: DeckId): void {
		if (unloadOfferTimer !== null) clearTimeout(unloadOfferTimer);
		unloadOffer = { deck, until: performance.now() + 2000 };
		unloadOfferTimer = setTimeout(() => {
			unloadOffer = null;
			unloadOfferTimer = null;
		}, 2000);
	}

	async function _acceptUnloadOffer(): Promise<void> {
		const offer = unloadOffer;
		if (offer === null) return;
		unloadOffer = null;
		if (unloadOfferTimer !== null) {
			clearTimeout(unloadOfferTimer);
			unloadOfferTimer = null;
		}
		await runPerformanceCommandFromUi({ type: 'unload', deck: offer.deck });
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

	// event is absent for the column-view lane's plain click (ColumnBrowser
	// has no multi-select concept) - treated as a non-extending single
	// select, same as a modifier-less TrackTable click.
	function selectRow(row: Pick<BrowserRow, 'stable_id'>, event?: MouseEvent): void {
		const p = panes[activePane];
		if (p.selected_id !== row.stable_id) _pushNav();
		const extend = event !== undefined && (event.metaKey || event.ctrlKey);
		const range = event !== undefined && event.shiftKey;
		const orderedIds = range
			? visibleRows.map((r) => r.stable_id)
			: [];
		p.select(row.stable_id, extend, range, orderedIds);
		// Warm /anlz so a subsequent deck load shares the in-flight fetch.
		ensureAnlz(row.stable_id);
		// Warm audio ArrayBuffer in background (never awaited - see
		// audio-prefetch-cache.svelte.ts). Saves ~1s fetchAudio on warm load.
		ensureAudioPrefetch(row.stable_id);
	}

	function setSearch(next: string): void {
		panes[activePane].setSearch(next);
		if (searchMode === 'find') return;
		if (!panes[activePane].whole_collection) return;
		// Genre chip filters stay client-side (filterRows), never FTS.
		if (/^genre:/i.test(next.trim())) return;
		const paneIndex = activePane;
		clearTimeout(_searchDebounce[paneIndex]);
		_searchDebounce[paneIndex] = setTimeout(
			() => void _searchWholeCollection(panes[paneIndex], next),
			SEARCH_DEBOUNCE_MS
		);
	}

	function _captureSearchReturn(): void {
		if (searchReturnSnap !== null) return;
		if (panes[activePane].search.trim() !== '') return;
		searchReturnSnap = _navSnap();
	}

	function openSearchMode(mode: SearchMode): void {
		_captureSearchReturn();
		searchMode = mode;
		const p = panes[activePane];
		if (mode === 'collection') {
			p.setWholeCollection(true);
			if (p.search.trim() !== '') void _searchWholeCollection(p, p.search);
		} else {
			p.setWholeCollection(false);
		}
		searchFocused = true;
		searchFocusToken += 1;
	}

	async function clearSearchAndReturn(): Promise<void> {
		const snap = searchReturnSnap;
		searchReturnSnap = null;
		searchMode = 'filter';
		searchFocused = false;
		const p = panes[activePane];
		p.setWholeCollection(false);
		p.setSearch('');
		genreFilterUntil = 0;
		if (snap === null) return;
		_navRestoring = true;
		try {
			if (snap.playlist_id !== p.playlist_id) {
				const node = _nodeForNav(snap);
				if (node !== null) await _loadPane(p, node);
			}
			p.setSearch(snap.search);
			if (snap.selected_id !== null) p.select(snap.selected_id, false);
			else {
				p.selected_id = null;
				p.selected_ids = [];
			}
			p.rememberScroll(snap.scroll_top);
			navEpoch += 1;
		} finally {
			_navRestoring = false;
		}
	}

	/** Genre chip → search box (`genre:` / `genre:~` / clear / undo). */
	function genreFilter(mode: 'strict' | 'loose' | 'clear' | 'undo', tag?: string): void {
		const p = panes[activePane];
		if (mode === 'clear') {
			setSearch('');
			genreFilterUntil = 0;
			return;
		}
		if (mode === 'undo') {
			setSearch(genreFilterPrior);
			genreFilterUntil = 0;
			return;
		}
		const clean = (tag ?? '').trim();
		if (clean === '') return;
		const next = mode === 'loose' ? `genre:~${clean}` : `genre:${clean}`;
		const cur = p.search.trim();
		// Same tag again clears (way back without needing triple / 20s window).
		if (cur.toLowerCase() === next.toLowerCase()) {
			setSearch('');
			genreFilterUntil = 0;
			return;
		}
		genreFilterPrior = cur;
		setSearch(next);
		genreFilterUntil = Date.now() + GENRE_WINDOW_MS;
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
		searchMode = next ? 'collection' : searchMode === 'find' ? 'find' : 'filter';
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
	// editable: a fully loaded real playlist is selected. Mutating a
	// truncated pane would replace the server membership with only its first
	// MAX_ROWS entries, silently dropping the rest.
	// reorderable narrows further to the pane's natural membership order -
	// drag-and-drop moves row.order positions, which only lines up with the
	// visible row order when there is no client sort/search in effect.
	const editablePane = $derived(source === 'collection' && canMutatePlaylist(pane));
	const reorderablePane = $derived(
		editablePane && pane.sort_key === null && pane.search.trim() === ''
	);

	async function _mutateActivePane(computeNext: (items: string[]) => string[]): Promise<void> {
		const p = pane;
		const id = p.playlist_id;
		if (id === null || id === 'all') return;
		if (source !== 'collection' || p.whole_collection) {
			pushToast('membership editing is disabled outside the complete playlist view', 'error');
			return;
		}
		if (p.truncated) {
			pushToast('playlist is truncated - membership editing is disabled to preserve unrendered tracks', 'error');
			return;
		}
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
				trackSelectedId={pane.selected_id}
				onselect={selectPlaylist}
				onselecttrack={selectRow}
				onloadtrack={loadRow}
				oncreateplaylist={() => createPlaylistUi()}
				onrenameplaylist={(n, name) => void renamePlaylistUi(n, name)}
				ondeleteplaylist={(n) => void deletePlaylistUi(n)}
				ondroptracks={(id, ids) => void dropTracksOnPlaylist(id, ids)}
			/>
		{/if}
	</div>
	<div class="list-panel">
		{#if unloadOffer !== null}
			<button class="unload-offer" onclick={() => void _acceptUnloadOffer()}>
				Unload CH {unloadOffer.deck}
			</button>
		{/if}
		<div class="pane-header">
			<PaneTabs
				{tabs}
				active={activePane}
				onactivate={(i) => (activePane = i)}
				ontogglesticky={togglePaneSticky}
				onreorder={reorderPaneTabs}
				ondropplaylist={dropPlaylistOnTabBar}
				onsaveas={(i) => void saveAsPlaylistUi(i)}
			/>
			<div class="header-right">
				<button
					class="rb-lit-button rb-inert master-dd"
					disabled
					title="not implemented - see PARITY-TODO"
				>
					MASTER <span class="caret">▾</span>
				</button>
				<button
					class="icon-btn"
					class:active={uiPrefs.library_density === 'compact'}
					title="compact row density"
					aria-label="compact row density"
					aria-pressed={uiPrefs.library_density === 'compact'}
					onclick={() => setLibraryDensity('compact')}
				>
					<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
						<path d="M2 3h12v1.5H2zM2 6h12v1.5H2zM2 9h12v1.5H2zM2 12h12v1.5H2z" fill="currentColor" />
					</svg>
				</button>
				<button
					class="icon-btn"
					class:active={uiPrefs.library_density === 'cosy'}
					title="cosy row density"
					aria-label="cosy row density"
					aria-pressed={uiPrefs.library_density === 'cosy'}
					onclick={() => setLibraryDensity('cosy')}
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
					title="FR-1: hide tracks whose audio file is missing on disk; also hides playlists with fewer than 30% available tracks from the tree"
				>
					<input
						type="checkbox"
						checked={uiPrefs.hide_broken_links}
						onchange={(e) => setHideBrokenLinks(e.currentTarget.checked)}
					/>
					<span>Hide broken links</span>
				</label>
				<label
					class="next-only"
					title="Filter to appropriate next tracks (Camelot compatible + BPM within ±6% of master, or half/double within 15 BPM). Shortcut: Tab"
				>
					<input
						type="checkbox"
						checked={uiPrefs.next_only_filter}
						onchange={(e) => setNextOnlyFilter(e.currentTarget.checked)}
					/>
					<span>Next-only</span>
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
				<button
					type="button"
					class="rb-lit-button nav-back"
					disabled={navHistory.length === 0}
					title="Back - previous playlist + selection"
					onclick={() => void goBack()}
				>
					← Back
				</button>
				<SearchBox
					value={pane.search}
					mode={searchMode}
					focusToken={searchFocusToken}
					placeholder={searchPlaceholder}
					oninput={setSearch}
					onclear={() => void clearSearchAndReturn()}
					onescapeclear={() => void clearSearchAndReturn()}
					onfocuschange={(f) => (searchFocused = f)}
				/>
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
			restoreKey={`${activePane}:${navEpoch}`}
			scrollTop={pane.scroll_top}
			removable={editablePane}
			reorderable={reorderablePane}
			onscrollcursor={(top) => panes[activePane].rememberScroll(top)}
			onsort={sortBy}
			onselectrow={selectRow}
			onloadrow={loadRow}
			onpickdoubledeck={pickDoubleDeck}
			onpreviewseek={previewSeek}
			onrate={rateRow}
			onrowvisible={rowVisible}
			onremoverow={removeRow}
			onreorder={reorderRows}
			ongenrefilter={genreFilter}
			{genreFilterUntil}
			searchQuery={pane.search}
			findQuery={findHighlightQuery}
			{suggestHoverId}
		/>
		<!-- dj_copilot suggest-next strip: keyed to the deck-1-loaded track. -->
		<SuggestNextStrip
			stableId={decks[1].stable_id}
			onload={(sid) => loadSuggest(sid)}
			onplay={(sid) => loadSuggest(sid, { play: true })}
			onhover={(sid) => (suggestHoverId = sid)}
			oncandidates={(cands) => (suggestCandidates = cands)}
		/>
		<RecommendedSection
			candidates={suggestCandidates}
			currentPlaylistId={pane.playlist_id}
			currentPlaylistMemberIds={playlistMemberIds}
			referenceBpm={masterRef?.bpm ?? null}
			referenceKey={masterRef?.key ?? null}
			onload={(sid) => loadSuggest(sid)}
			onplay={(sid) => loadSuggest(sid, { play: true })}
			onhover={(sid) => (suggestHoverId = sid)}
		/>
	</div>
	<div
		class="conn-dots"
		aria-label="server connectivity"
		title={`LIB ${libUp ? 'loaded' : 'empty'} · BE ${beUp ? 'up' : 'down'} · FE ${feUp ? 'up' : 'down'}`}
	>
		<span class="conn-dot" class:up={libUp} data-server="lib" aria-label={libUp ? 'library loaded' : 'library empty'}></span>
		<span class="conn-dot" class:up={beUp} data-server="be" aria-label={beUp ? 'backend up' : 'backend down'}></span>
		<span class="conn-dot" class:up={feUp} data-server="fe" aria-label={feUp ? 'frontend up' : 'frontend down'}></span>
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
		{#if jobProgress.ribbon()}
			{@const ribbon = jobProgress.ribbon()!}
			<span
				class="job-ribbon"
				style={`--job:${ANALYSIS_COLORS[ribbon.kind]}; --pct:${Math.max(0.02, ribbon.progress)}`}
				title={`${ribbon.label} offload`}
				aria-label={ribbon.label}
			>
				<span class="job-ribbon-fill"></span>
				<span class="job-ribbon-label">{ribbon.label}</span>
			</span>
		{/if}
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
		position: relative;
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
		position: relative;
	}
	.unload-offer {
		position: absolute;
		left: 50%;
		bottom: 28px;
		transform: translateX(-50%);
		z-index: 5;
		padding: 8px 18px;
		border: 1px solid var(--rb-orange);
		border-radius: 4px;
		background: rgba(20, 24, 32, 0.94);
		color: var(--rb-orange);
		font-family: var(--rb-font);
		font-size: 13px;
		font-weight: 600;
		cursor: pointer;
		box-shadow: 0 4px 16px rgba(0, 0, 0, 0.45);
	}
	.unload-offer:hover {
		background: rgba(232, 161, 58, 0.18);
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
	.hide-broken,
	.next-only {
		display: inline-flex;
		align-items: center;
		gap: 3px;
		color: var(--rb-text-dim);
		font-size: var(--rb-fs-label);
		white-space: nowrap;
		cursor: pointer;
	}
	.hide-broken:hover,
	.next-only:hover {
		color: var(--rb-text);
	}
	.hide-broken input,
	.next-only input {
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
		cursor: pointer;
	}
	.icon-btn:disabled {
		cursor: default;
	}
	.icon-btn.active {
		color: var(--rb-accent);
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
	.job-ribbon {
		position: relative;
		flex: 1;
		height: 12px;
		border: 1px solid color-mix(in srgb, var(--job) 55%, transparent);
		background: color-mix(in srgb, var(--job) 12%, transparent);
		overflow: hidden;
		min-width: 80px;
	}
	.job-ribbon-fill {
		position: absolute;
		inset: 0 auto 0 0;
		width: calc(var(--pct) * 100%);
		background: color-mix(in srgb, var(--job) 55%, transparent);
	}
	.job-ribbon-label {
		position: relative;
		z-index: 1;
		display: block;
		padding: 0 6px;
		font-size: 9px;
		line-height: 12px;
		color: var(--rb-text);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.conn-dots {
		position: absolute;
		left: 10px;
		bottom: 22px;
		display: flex;
		flex-direction: column;
		gap: 3px;
		z-index: 6;
		pointer-events: none;
	}
	.conn-dot {
		width: 7px;
		height: 7px;
		border-radius: 50%;
		background: #3a4048;
		box-shadow: inset 0 0 0 1px #23282f;
	}
	.conn-dot.up {
		background: var(--rb-green, #35c04f);
		box-shadow: 0 0 4px color-mix(in srgb, var(--rb-green, #35c04f) 70%, transparent);
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
