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
	import { replaceState } from '$app/navigation';
	import { onMount, tick, untrack } from 'svelte';
	import { viewportFloatingPopover } from '$lib/ui/clamp-to-viewport';
	import { getConnectionState, subscribeKind, subscribeResync } from '$lib/api/events-bus';
	import { shouldRunLibraryFallbackPoll } from '$lib/rb/app-posture';
	import {
		ConflictError,
		RbApiError,
		decodePreviewStrip,
		fetchRbMeta,
		getHealth,
		pingHealth,
		timeoutSignal,
		getReconcileSummary,
		getTrack,
		listPlaylistsHydrated,
		listPlaylistTracksPage,
		listTracksHydrated,
		patchTrack,
		searchCollection,
		vocalsOf
	} from '$lib/rb/api-rb';
	import { getSmartlistTracks, type SmartlistSummary } from '$lib/rb/api-smartlists';
	import { getIngestCoverage, type IngestCoverage } from '$lib/rb/api-ingest';
	import {
		libraryHealthDot as _computeLibraryHealthDot,
		type LibraryHealthDot
	} from '$lib/rb/library-health-dots';
	import {
		plannedTitle,
		anyDeckPlaying,
		createPlayingGate,
		resolveRowMarkerAnlz,
		resolveRowVocals,
		isAppropriateNext,
		resolveSearchFilterFallback,
		selectSearchFilterFallback,
		enqueueLibraryJobsBatched,
		libraryJobsStore,
		LibraryJobsChrome,
		type NextOnlyRef,
		completeLibraryUsable,
		recordOpenToLibraryRows,
		formatReplaceStateUrl,
		addToPlaylistToastMessage,
		appendTracksToPlaylist,
		removeFromLibrary,
		isCurrentBrowserSearch,
		reportBrowserSearchResult,
		subscribeBrowserSearch,
		type BrowserSearchRequest,
		isLibraryPanelsCollapsed,
		noteVisibleLibraryRowCount,
		setLibraryPanelsCollapsed,
		toggleLibraryPanels,
		createAutoPlayFeedSnapshot,
		getAutoPlayRankOf,
		setAutoPlayTrackFeed,
		getSpotifyPendingTracks,
		type SpotifyPendingTrack,
		fillAllTracksPane,
		fillPlaylistPane,
		PLAYLIST_FIRST_PAGE,
		fillAutolistPane,
		autolistNode,
		isAutolistId,
		AUTOLIST_ID,
		queryAutolists,
		emptyAutolistSelection,
		hasAutolistSelection,
		type AutolistSelection,
		ensureAudioPrefetch,
		clearSelection,
		pruneSelection,
		fetchAllPages,
		rowFromListWire as _rowFromListWire,
		rowFromPlaylistWire as _rowFromPlaylistWire,
		PlaylistSetTabs
	} from './browser/browser-panel-support';
	import type {
		PlaylistSummaryHydrated,
		PlaylistTrackRowWire,
		SearchHitWire,
		Vocals
	} from '$lib/rb/api-rb';
	import type { DeckId } from '$lib/rb/deck-slots';
	import type { PlaylistNode, RbMeta } from '$lib/rb/library-types';
	// Deck state remains engine-owned; real load interactions route through
	// the same validated dispatcher exposed to browser agents.
	import { deckStates as decks, DECK_IDS, mixerState } from '$lib/rb/audio-engine.svelte';
	import {
		createFilterDebounce,
		recordCollectionSearchTiming,
		recordFilterTiming,
		recordLibraryLoadTiming,
		recordPlaylistSwitchFirstRowsMs,
		recordPlaylistTreeReadyMs
	} from '$lib/rb/library-perf';
	import type { FilterDebounce, FilterSettle } from '$lib/rb/library-perf';
	import {
		dispatchPerformanceCommand,
		registerPerformanceBrowserAdapter,
		runPerformanceCommandFromUi
	} from '$lib/rb/performance-ipc.svelte';
	import {
		BLANK_PLAYLIST_GRACE_MS,
		DEFAULT_PLAYLIST_NAME,
		clearPlaylistCreateGrace,
		collectBlankPlaylistDeletes,
		createPlaylist,
		deletePlaylist,
		deletePlaylistItem,
		duplicatePlaylist,
		getPlaylistTracksEtag,
		transferPlaylistTracks,
		isWithinCreateGrace,
		markPlaylistCreateGrace,
		movePlaylistItems,
		PlaylistConflictError,
		patchPlaylist,
		renamePlaylist,
		replacePlaylistTracks
	} from '$lib/rb/playlist-write';
	import {
		bootPlaylistsPrefetch,
		bootTracksPrefetch,
		canBootAllTracksEarly,
		fetchBootTracksFirstPage,
		LIBRARY_BOOT_PAGE_SIZE,
	} from '$lib/rb/library-boot-hydration';
	import { bootScheduler } from '$lib/rb/boot-scheduler';
	import {
		rememberSpotifyRecent,
		setConfirmPref,
		setHideBrokenLinks,
		setLastPlaylist,
		setLibraryDensity,
		setPlaylistTreeWidth,
		setNextOnlyFilter,
		setAvailableOfflineFilter,
		setRemixesFilter,
		setVocalsFilter,
		uiPrefs,
		PLAYLIST_TREE_WIDTH_MAX,
		PLAYLIST_TREE_WIDTH_MIN
	} from '$lib/rb/prefs.svelte';
	import {
		formatHideBrokenCheckboxTooltip,
		hydrateRuntimePolicy,
		playlistMostlyBroken
	} from '$lib/rb/runtime-policy.svelte';
	import { PREVIEW_SUPERSEDED, previewCueSeek } from '$lib/player/preview-cue.svelte';
	import { pushToast } from '$lib/stores.svelte';
	import type { UploadFileResult } from '$lib/rb/api-ingest';
	import {
		collectDroppedAudioFiles,
		collectDroppedFolderName
	} from '$lib/rb/ingest-drop-files';
	import {
		ingestFolderToNewPlaylist,
		type PossibleDupDecision
	} from '$lib/rb/playlist-folder-drop';
	import PlaylistFolderDupModal from './PlaylistFolderDupModal.svelte';
	import AddToPlaylistPicker from './browser/AddToPlaylistPicker.svelte';
	import {
		removeFromLibraryConfirmMessage,
		removeFromLibraryToastMessage
	} from '$lib/components/rb/browser/track-library-menu';
	import BuildIdentity from './BuildIdentity.svelte';
	import RecommendedSection from './RecommendedSection.svelte';
	import SuggestNextStrip from './SuggestNextStrip.svelte';
	import {
		beginPendingLoadPlay,
		clearPendingLoadPlay,
		consumePendingLoadPlay,
		markLoadSpanningPress,
		pickDoubleClickDeck,
		type DeckSlotState
	} from '$lib/rb/deck-slots';
	import TrackEditModals from './TrackEditModals.svelte';
	import AddTrackSearch from './browser/AddTrackSearch.svelte';
	import PerformanceRecorderRail from './browser/PerformanceRecorderRail.svelte';
	import PaneTabs from './browser/PaneTabs.svelte';
	import type { PaneTabInfo } from './browser/PaneTabs.svelte';
	import {
		applyDecodedStripAcrossPanes,
		canMutatePlaylist,
		createPaneStore,
		derivePlaylistDeckMembership,
		derivePlaylistPaneOpenCounts,
		filterRows,
		getHealthAtBoot,
		getHealthFreshWithRetry,
		installBrowserSortIpc,
		makeClientRowProvider,
		multiPanePlaylistIds,
		reconcileBootSnapshot,
		canAddPaneSlot,
		MAX_PANE_SLOTS,
		reorderPanesInPlace,
		parseLv1,
		resolveBootPlaylist,
		resolveNewTabIndex,
		shouldRetryBootPane,
		writeLv1,
		rowHasVocalLyrics,
		rowIsLocallyAvailable,
		rowIsRemix,
		sortRows,
		visibleRowsOf,
		collectionSearchEmptyMessage
	} from './browser/pane-contract.svelte';
	import type {
		BrowserRow,
		PaneStore,
		PlaylistDragPayload,
		SortKey
	} from './browser/pane-contract.svelte';
	import LibraryNav from './browser/LibraryNav.svelte';
	import {
		fetchMissingTrackRows,
		isMissingTracksId,
		MISSING_TRACKS_ID,
		missingTracksNode
	} from './browser/missing-tracks';
	import LibraryLoadIndicator from './browser/LibraryLoadIndicator.svelte';
	import LyricSearchResults from './browser/LyricSearchResults.svelte';
	import SearchBox from './browser/SearchBox.svelte';
	import TrackTable from './browser/TrackTable.svelte';
	import {
		ensureAnlzPrefetch,
		getAnlzEntry,
		isAnlzEntryUsable,
		registerAnlzConsumer,
		resolveDisplayedAnlz,
		unregisterAnlzConsumer
	} from './wave/anlz-cache.svelte';
	import SpotifySourcePanel from './browser/SpotifySourcePanel.svelte';
	// track-list-virtualization: TrackTable now DOM-virtualizes its render,
	// so panes no longer cap fetches at 500 rows - All Tracks walks every
	// cursor page (PAGE_SIZE is a per-request page size, not a result cap);
	// playlists were never fetch-capped (getPlaylistHydrated already
	// returns the full membership in one call), only client-sliced - that
	// slice is gone too (see _fetchPlaylistRows).
	const PAGE_SIZE = LIBRARY_BOOT_PAGE_SIZE;
	// Whole-collection FTS5 search stays hard-capped (unrelated to the
	// fetch-cap removal above): a global text query over the whole library
	// is a separate, ranked result set, not a browsable pane listing.
	const MAX_SEARCH_ROWS = 200;
	function playlistBrokenCount(p: PlaylistSummaryHydrated): number {
		if (p.available_count < 0) return 0;
		return p.track_count - p.available_count;
	}

	// Pane state lives in the typed contract (pane-contract.svelte.ts):
	// 4 independent PaneStore instances - selection, search, sort, and
	// scroll cursor per pane survive tab switches. Only the active pane
	// is mounted (one TrackTable) - a deliberate perf choice, kept.
	const panes: PaneStore[] = [createPaneStore()];
	let activePane = $state(0);
	let openModal = $state<'bulk-edit' | 'find-replace' | 'mytag' | null>(null);
	let modalEtags = $state<Record<string, string>>({});
	let playlists = $state<PlaylistSummaryHydrated[]>([]);
	let allTracksCount = $state<number | null>(null);
	// Bumped every time something OTHER than _init() writes allTracksCount or
	// playlists respectively, so _init()'s boot Promise.all can tell whether
	// its own snapshot of EACH field is still the freshest once it resolves.
	// Two separate counters, not one shared epoch: _refreshLibraryRowsOnce
	// writes these two fields at different times within the same call (
	// _refreshPlaylists() first, the health re-read after), so a single
	// shared epoch bumped by either write made a playlists-only write during
	// _init()'s boot read discard _init()'s own, still-uncontested health
	// snapshot - the boot pane then read a stale/null allTracksCount and
	// _restoreBootPane() treated a non-empty library as empty (PR #1656
	// review round 11, P2 BLOCKING). See reconcileBootSnapshot's doc comment
	// for the race this guards.
	let _healthWriteEpoch = 0;
	let _playlistsWriteEpoch = 0;
	let allTracksNonBrokenCount = $state<number | null>(null);
	let allTracksBrokenCount = $state<number | null>(null);
	let allTracksReconcileError = $state<string | null>(null);
	let playlistsLoading = $state(true);
	let playlistsError = $state<string | null>(null);
	let source = $state<'collection' | 'spotify'>('collection');
	let spotifySelectedId = $state<string | null>(null);
	let urlPlaylistId: string | null = null;
	let spotifyPendingTracks = $state<SpotifyPendingTrack[] | null>(null);
	let spotifyPendingLoading = $state(false);
	let spotifyPendingError = $state<string | null>(null);
	let spotifyPendingSequence = 0;
	let autolistSelection = $state<AutolistSelection>(emptyAutolistSelection());
	let autolistTitle = $state('Autolists');
	const _inflight = new Set<string>();
	/** Brief unload affordance after a load blocked by an active deck. */
	let unloadOffer = $state<{ deck: DeckId; until: number } | null>(null);
	let unloadOfferTimer: ReturnType<typeof setTimeout> | null = null;
	// Restored by pin a66ee132a14e. #1339/#1352 replaced the old three-dot
	// `conn-dots` strip (fe / be / lib) with this coverage row and carried
	// only the `lib` signal across as `libraryHealth`, so the two liveness
	// dots vanished with no replacement anywhere in the UI. They are the
	// only thing that distinguishes "the library really is empty" from "the
	// engine is not answering", which is exactly the confusion that made
	// this pin worth filing.
	let frontendOnline = $state<LibraryHealthDot>({
		label: 'Frontend',
		state: 'loading',
		detail: 'checking frontend'
	});
	let backendOnline = $state<LibraryHealthDot>({
		label: 'Backend',
		state: 'loading',
		detail: 'checking backend'
	});
	let libraryHealthError = $state<string | null>(null);
	/**
	 * Derived, not assigned. pin a66ee132a14e: this dot used to quote
	 * `state_db.tracks`, the RAW row count, so it advertised ">5k tracks
	 * available" on a library where most of those rows are broken links to
	 * files that are permanently gone. The playable total is
	 * `allTracksNonBrokenCount`, which the reconcile summary settles a moment
	 * AFTER init - assigning the dot at init time is precisely how it came to
	 * quote the wrong number, so the dot is computed from whatever has landed
	 * instead of frozen at the first thing that did.
	 */
	const libraryHealth = $derived<LibraryHealthDot>(
		_computeLibraryHealthDot(
			libraryHealthError,
			allTracksCount,
			playlists.length,
			allTracksNonBrokenCount,
			allTracksBrokenCount,
			allTracksReconcileError
		)
	);
	let vocalsCompletion = $state<LibraryHealthDot>({
		label: 'Vocals completion',
		state: 'loading',
		detail: 'checking vocals coverage'
	});
	let stemsCompletion = $state<LibraryHealthDot>({
		label: 'Stems completion',
		state: 'loading',
		detail: 'checking stems coverage'
	});
	let lyricsCompletion = $state<LibraryHealthDot>({
		label: 'Lyrics completion',
		state: 'loading',
		detail: 'checking lyrics coverage'
	});
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
	// Pin 2ac3a0: playlist deck-membership + multi-pane tints for PlaylistTree,
	// derived from panes' ALREADY HYDRATED rows only - never a fetch of an
	// unopened playlist just to colour it in.
	const deckLoadedPlaylistIds = $derived(derivePlaylistDeckMembership(panes, loadedIds));
	const multiPanePlaylistIds_ = $derived(multiPanePlaylistIds(derivePlaylistPaneOpenCounts(panes)));
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
	// Strip cue/phrase markers (LIBUX-12), resolved here for the same reason as
	// vocalsById: TrackTable renders per row and must render from props alone.
	const markerAnlzById = $derived.by(() =>
		resolveRowMarkerAnlz({
			rows: pane.rows,
			decks: DECK_IDS.flatMap((d) => {
				const st = decks[d];
				if (st.stable_id === null) return [];
				return [{ stable_id: st.stable_id, playing: st.playing, anlz: resolveDisplayedAnlz(st.anlz, st.stable_id) }];
			}),
			// Pure read, same contract as cachedVocals above.
			cachedAnlz: (stable_id: string) => {
				const entry = getAnlzEntry(stable_id);
				return entry !== undefined && entry.status === 'ready' ? entry.data : undefined;
			}
		})
	);
	/** Reactively copies a decoded local waveform strip into the selected
	 * row(s), across every pane and every row loaded there. Reads the shared
	 * anlz cache (same pattern as `vocalsById` above) instead of fetching
	 * directly, so a decode that only resolves after `ensureAnlz`'s ambient
	 * retry (issue #735 follow-up) still reaches the row. The old one-shot
	 * fetch-and-adopt stopped watching the moment its OWN fetch settled
	 * retryable, so a track needing a second or third retry never got its
	 * strip until the row was reselected or the page reloaded (Codex
	 * finding, issue #735 follow-up, discussion_r3908286630). Applies to
	 * EVERY row matching a selected stable_id in EVERY pane, not just the
	 * pane whose own selection triggered the decode: the same track can be
	 * loaded as a row in more than one pane, and a copy that isn't the
	 * active selection still shares the one cache entry
	 * (discussion_r3908503574, discussion_r3909752654). Watches each pane's
	 * full `selected_ids` multi-selection, not just its singular
	 * `selected_id` (the last-clicked anchor): a Cmd/Ctrl-click adds to
	 * `selected_ids` without moving `selected_id` off the new anchor, so an
	 * earlier multi-selected row's still-pending decode needs its own id
	 * watched too (discussion_r3910053530). */
	$effect(() => {
		const selectedIds = new Set<string>();
		for (const p of panes) {
			for (const sid of p.selected_ids) selectedIds.add(sid);
			if (p.selected_id !== null) selectedIds.add(p.selected_id);
		}
		for (const sid of selectedIds) {
			const entry = getAnlzEntry(sid);
			if (!isAnlzEntryUsable(entry) || entry.data.local_waveform?.status !== 'decoded') continue;
			applyDecodedStripAcrossPanes(
				panes,
				sid,
				decodePreviewStrip(
					entry.data.local_waveform.preview_b64,
					entry.data.local_waveform.preview_max
				)
			);
		}
	});
	/** Keeps anlz-cache's ambient retry alive only while a track is actually
	 * being looked at here - selected in some pane, or loaded on some deck -
	 * and releases it the moment neither is true. Without this, arrow-key
	 * navigation across many uncached rows leaves every one of them retrying
	 * every cooldown forever, each still holding a decoder slot the user's
	 * ACTUAL selection needs (Codex finding, issue #735 follow-up,
	 * discussion_r3908644098). Diffed against the previous run rather than
	 * unregister-all-then-reregister-all, so a selection that merely moves
	 * within the same set of ids (e.g. two panes both still pointing at
	 * loaded decks) does not thrash tokens on every unrelated rerun. */
	let _heldAnlzConsumers = new Map<string, symbol>();
	$effect(() => {
		const active = new Set<string>(loadedIds);
		for (const p of panes) {
			if (p.selected_id !== null) active.add(p.selected_id);
		}
		for (const [sid, token] of _heldAnlzConsumers) {
			if (!active.has(sid)) {
				unregisterAnlzConsumer(sid, token);
				_heldAnlzConsumers.delete(sid);
			}
		}
		for (const sid of active) {
			if (!_heldAnlzConsumers.has(sid)) {
				_heldAnlzConsumers.set(sid, registerAnlzConsumer(sid));
			}
		}
		return () => {
			for (const [sid, token] of _heldAnlzConsumers) unregisterAnlzConsumer(sid, token);
			_heldAnlzConsumers.clear();
		};
	});
	const treeNodes = $derived(
		playlists
			.slice()
			// With Broken unchecked, playlists below the existing 30% playable
			// threshold, including zero-track empty entries, vanish from the tree.
			// A playlist still inside its create grace stays: the '+' flow needs
			// the brand-new blank reachable so PlaylistTree can focus its rename.
			.filter(
				(p) =>
					!uiPrefs.hide_broken_links ||
					isWithinCreateGrace(p.playlist_id) ||
					!playlistMostlyBroken(p)
			)
			// Rekordbox custom tree order (djmdPlaylist Seq walk, SCREENSHOT-SPEC
			// 5b) - NOT alphabetical. Playlists without a rekordbox order (seq
			// null) sink below the ordered ones, name-sorted among themselves.
			.sort((a, b) => {
				// `seq` is optional as well as nullable in PlaylistSummary, so an
				// absent key means the same "no rekordbox order" as a null one.
				const aSeq = a.seq ?? null;
				const bSeq = b.seq ?? null;
				if (aSeq !== null && bSeq !== null) return aSeq - bSeq;
				else if (aSeq !== null) return -1;
				else if (bSeq !== null) return 1;
				else return a.name.localeCompare(b.name);
			})
			.map(
				(p): PlaylistNode => ({
					playlist_id: p.playlist_id,
					name: p.name,
					track_count: p.track_count,
					broken_count: playlistBrokenCount(p),
					kind: 'playlist',
					mostly_broken: playlistMostlyBroken(p),
					forbid_duplicates: p.forbid_duplicates === true,
					children: []
				})
			)
	);
	const hiddenBrokenPlaylistCount = $derived(
		uiPrefs.hide_broken_links
			? playlists.filter(
					(p) => !isWithinCreateGrace(p.playlist_id) && playlistMostlyBroken(p)
				).length
			: 0
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

	function _applyLibraryFilters(rows: BrowserRow[]): BrowserRow[] {
		let out = rows;
		if (uiPrefs.remixes_filter) out = out.filter(rowIsRemix);
		if (uiPrefs.vocals_filter) out = out.filter(rowHasVocalLyrics);
		if (uiPrefs.available_offline_filter) out = out.filter(rowIsLocallyAvailable);
		return out;
	}

	function _applyNextOnly(rows: BrowserRow[]): BrowserRow[] {
		if (!uiPrefs.next_only_filter) return rows;
		const ref = nextOnlyRef;
		if (ref === null) return rows;
		return rows.filter((r) => isAppropriateNext(r, ref));
	}

	function _applyPaneFilters(rows: BrowserRow[]): BrowserRow[] {
		return _applyNextOnly(_applyLibraryFilters(rows));
	}

	interface VisibleSearchResult {
		rows: BrowserRow[];
		ignoredFilters: string[];
	}

	const hideBrokenForActivePane = $derived(
		uiPrefs.hide_broken_links && !isMissingTracksId(pane.playlist_id)
	);

	function _searchFilterNames(includeBrokenFilter: boolean): string[] {
		const names: string[] = [];
		if (includeBrokenFilter && hideBrokenForActivePane) names.push('Hide broken links');
		if (uiPrefs.next_only_filter && nextOnlyRef !== null) names.push('next-only');
		return names;
	}

	function _computeVisibleSearchResult(): VisibleSearchResult {
		const autoPlayRankOf = pane.sort_key === 'autoplay' ? getAutoPlayRankOf() : undefined;
		// Find mode: keep full list (no filter); TrackTable highlights matches.
		if (searchMode === 'find') {
			return {
				rows: _applyPaneFilters(
					sortRows(
						filterRows(pane.rows, '', hideBrokenForActivePane),
						pane.sort_key,
						pane.sort_dir,
						autoPlayRankOf
					)
				),
				ignoredFilters: []
			};
		}
		if (wholeCollectionActive) {
			const unfilteredRows = sortRows(
				pane.search_results,
				pane.sort_key,
				pane.sort_dir,
				autoPlayRankOf
			);
			const fallback = resolveSearchFilterFallback(
				_applyPaneFilters(unfilteredRows),
				unfilteredRows,
				_searchFilterNames(false)
			);
			return fallback;
		}
		const unfilteredRows = visibleRowsOf(pane, false, autoPlayRankOf);
		const fallback = resolveSearchFilterFallback(
			_applyPaneFilters(visibleRowsOf(pane, hideBrokenForActivePane, autoPlayRankOf)),
			unfilteredRows,
			pane.search.trim() === '' ? [] : _searchFilterNames(true)
		);
		return fallback;
	}

	/** Wall time of the LAST filter+sort pass (PERF-R5 Q9).
	 * Deliberately a plain `let`, not $state: it is written from inside the
	 * $derived below, and a rune write there would re-dirty the very derived
	 * that produced it. */
	let _lastVisibleComputeMs = 0;

	const visibleSearchResult = $derived.by(() => {
		// This pass is filterRows + sortRows over the WHOLE pane array - up to
		// ~8k rows on All Tracks, plus an O(n log n) sort when a column sort is
		// active. Timing it is the only way the filter debounce below can be
		// shown to be worth having.
		const startedAt = performance.now();
		const result = _computeVisibleSearchResult();
		_lastVisibleComputeMs = performance.now() - startedAt;
		return result;
	});
	function noteRenderedLibraryRowCapacity(count: number): void {
		// PaneStore starts with an intentional empty array before onMount begins
		// its first fetch. That is not a settled library result and must not
		// collapse the panels for a normal relaunch.
		if (!pane.has_settled_result || pane.loading) return;
		noteVisibleLibraryRowCount(count);
	}

	$effect(() => {
		if (isLibraryPanelsCollapsed()) suggestHoverId = null;
	});
	const visibleRows = $derived(visibleSearchResult.rows);
	const ignoredSearchFilters = $derived(visibleSearchResult.ignoredFilters);
	const filterFallbackNote = $derived.by(() => {
		if (ignoredSearchFilters.length === 0) return null;
		const filterLabel = ignoredSearchFilters.join(' and ');
		const suffix = ignoredSearchFilters.length === 1 ? '' : 's';
		const trackLabel = visibleRows.length === 1 ? 'track' : 'tracks';
		return `${visibleRows.length} ${trackLabel} fetched by ignoring the active ${filterLabel} filter${suffix}.`;
	});
	const searchFilterFallback = $derived.by(() => {
		// Find highlights rather than filters. Recovery needs complete, settled
		// results for the current query, never a capped page or prior FTS response.
		if (
			pane.search.trim() === '' ||
			visibleRows.length !== 0 ||
			pane.loading ||
			pane.searching ||
			searchMode === 'find' ||
			!uiPrefs.next_only_filter
		)
			return null;
		const complete = wholeCollectionActive
			? pane.search_result_query === pane.search.trim() &&
				pane.search_total === pane.search_results.length
			: !pane.truncated;
		// Only the compatible filter is bypassed. The user-selected Broken filter stays intact.
		return selectSearchFilterFallback(
			pane.search,
			visibleRows,
			wholeCollectionActive
				? sortRows(
						filterRows(pane.search_results, '', hideBrokenForActivePane),
						pane.sort_key,
						pane.sort_dir
					)
				: visibleRowsOf(pane, hideBrokenForActivePane),
			complete
		);
	});
	const renderedRows = $derived(searchFilterFallback ?? visibleRows);
	const filterBypassNote = $derived(
		searchFilterFallback === null
			? null
			: `Showing ${searchFilterFallback.length} search match${searchFilterFallback.length === 1 ? '' : 'es'} with the compatible filter bypassed.`
	);
	// Read contract handed to TrackTable (getters stay reactive through
	// renderedRows/pane). The virtualization lane replaces THIS provider,
	// not TrackTable's props.
	const provider = makeClientRowProvider(
		() => renderedRows,
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
		if (searchFilterFallback !== null) return null;
		// Overlay (LibraryLoadIndicator) is the only in-flight surface: load
		// and whole-collection search both paint there (pin 02717d4ea496,
		// follow-up #1688). Empty-state is settled-only, so a concurrent
		// search+load cannot stack two status strings on the same pixels.
		if (pane.loading || pane.searching) return null;
		if (searchMode === 'find') {
			if (pane.error !== null) return `load failed: ${pane.error}`;
			else if (pane.playlist_id === null) return 'blank list - choose a playlist in the tree';
			else if (isMissingTracksId(pane.playlist_id) && visibleRows.length === 0) return 'no missing tracks';
			else if (isAutolistId(pane.playlist_id) && !hasAutolistSelection(autolistSelection))
				return 'select an autolist';
			else if (isAutolistId(pane.playlist_id) && visibleRows.length === 0)
				return 'no tracks in stacked autolists';
			else if (visibleRows.length === 0) return 'empty playlist';
			else return null;
		}
		if (wholeCollectionActive) {
			return collectionSearchEmptyMessage(pane.search_error, visibleRows.length);
		} else if (pane.error !== null) return `load failed: ${pane.error}`;
		else if (pane.playlist_id === null) return 'blank list - choose a playlist in the tree';
		else if (visibleRows.length === 0 && pane.search.trim() !== '') return 'no tracks match the search';
		else if (isMissingTracksId(pane.playlist_id) && visibleRows.length === 0) return 'no missing tracks';
		else if (isAutolistId(pane.playlist_id) && !hasAutolistSelection(autolistSelection))
			return 'select an autolist';
		else if (isAutolistId(pane.playlist_id) && visibleRows.length === 0)
			return 'no tracks in stacked autolists';
		else if (visibleRows.length === 0 && pane.rows.length > 0 && hideBrokenForActivePane)
			return 'all tracks in this list are broken links (hidden by Broken filter)';
		else if (
			visibleRows.length === 0 &&
			pane.rows.length > 0 &&
			uiPrefs.next_only_filter
		) {
			return nextOnlyRef === null
				? 'next-only: load a track with key+BPM (master preferred) to filter'
				: 'no appropriate next tracks in this list (Camelot + BPM ±6% or half/double ≤15)';
		} else if (visibleRows.length === 0 && pane.rows.length > 0 && uiPrefs.remixes_filter) {
			return 'no remixes in this list (title markers: remix/bootleg/rework/VIP/edit)';
		} else if (visibleRows.length === 0 && pane.rows.length > 0 && uiPrefs.vocals_filter) {
			return 'no tracks here with >5 lines of lyrics (Vocals filter)';
		} else if (visibleRows.length === 0) return 'empty playlist';
		else return null;
	});

	onMount(() => {
		const uninstallBrowserSortIpc = installBrowserSortIpc({
			sort: sortBy,
			query: () => ({
				sort_key: pane.sort_key,
				sort_dir: pane.sort_dir,
				visible_ids: visibleRows.map((row) => row.stable_id)
			})
		});
		const unregisterPerformanceBrowser = registerPerformanceBrowserAdapter({
			selectPlaylist: _selectPlaylistFromCommand,
			readSnapshot: () => {
				const p = panes[activePane];
				const trimmedSearch = p.search.trim();
				return {
					search: trimmedSearch === '' ? null : p.search,
					sort:
						p.sort_key === null
							? null
							: { key: p.sort_key, direction: p.sort_dir === 1 ? 'asc' : 'desc' },
					selected_row: p.selected_id
				};
			}
		});
		const url = new URL(window.location.href);
		const lv1 = parseLv1(url.searchParams);
		if (lv1.source === 'spotify') {
			source = 'spotify';
			spotifySelectedId = lv1.playlist_id;
		} else {
			urlPlaylistId = lv1.playlist_id;
		}
		const unsubscribeSearch = subscribeBrowserSearch((request) => {
			// Programmatic, so it must beat (and cancel) any keystroke burst
			// still waiting to settle.
			if (request.revision > 0) _setSearchNow(request.query, request);
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
		// PERF-UI-01: first crossing into a short viewport collapses
		// Next/Recommended so thead + one track row fit in the leftover
		// library row. Edge-triggered so a manual chevron re-expand is
		// not fought closed on every resize tick.
		const shortViewportMq = window.matchMedia('(max-height: 799px)');
		let wasShortViewport = false;
		const applyShortViewport = (): void => {
			const matches = shortViewportMq.matches;
			if (matches && !wasShortViewport) setLibraryPanelsCollapsed(true);
			wasShortViewport = matches;
		};
		applyShortViewport();
		shortViewportMq.addEventListener('change', applyShortViewport);
		void _init();
		const blankSweepTimer = setInterval(
			() => void _sweepBlankPlaylists(),
			BLANK_PLAYLIST_GRACE_MS
		);
		// Liveness ping restored with pin a66ee132a14e's dots. Kept at the
		// original 2.5s cadence and 2s timeout: this is the only signal that
		// goes red while the rest of the pane simply shows stale data, so a
		// slower poll would make it lie for longer than it is useful.
		let connAlive = true;
		const pingConn = async (): Promise<void> => {
			const [be, fe] = await Promise.all([_pingBackend(), _pingFrontend()]);
			if (!connAlive) return;
			backendOnline = be;
			frontendOnline = fe;
		};
		void pingConn();
		const connTimer = setInterval(() => void pingConn(), CONN_PING_MS);

		// ---- library listing freshness -------------------------------------
		// FAST PATH: the engine tells us the moment a track changes, from any
		// writer (this tab, another tab, the CLI, an agent). One targeted
		// refetch, no idle polling.
		// Pane-row refetches go through `_libraryRefreshGate`, never through
		// `_refreshLibraryRowsOnce` directly: the gate decides WHEN a background
		// refetch is allowed to run, and its coalescer guarantees it is only
		// ever one refetch. Playlist TREE names refresh immediately on
		// `playlists` / resync (see the handlers below); that is cheap and is
		// user-visible undo/redo state.
		const unsubscribeTracks = subscribeKind('tracks', () => _libraryRefreshGate.request());
		// Tree names are user-visible undo/redo state (v1). The playing-gated
		// full library refetch can be in flight, deferred, or throw after its
		// GET /playlists snapshot, which left the history panel enabled while
		// the renamed row never appeared (#1888). Refresh names immediately;
		// the gate still refreshes pane membership and the health-dot total.
		const unsubscribePlaylists = subscribeKind('playlists', () => {
			void _refreshPlaylists();
			_libraryRefreshGate.request();
		});
		const unsubscribeSmartlists = subscribeKind('smartlists', () => _libraryRefreshGate.request());
		// A resync means the bus knows it missed events but not which, so the
		// only sound response is to refetch as if everything changed.
		const unsubscribeResync = subscribeResync(() => {
			void _refreshPlaylists();
			_libraryRefreshGate.request();
		});
		// DEGRADED PATH: the poll is deliberately kept, not deleted. When the
		// WS is down it is the only thing keeping this pane honest. Poll follows
		// Gig/Prep posture and stands down while the bus is open.
		let lastLibraryFallbackAt = 0;
		const libraryFallbackTimer = setInterval(() => {
			const now = Date.now();
			if (!shouldRunLibraryFallbackPoll(now, lastLibraryFallbackAt, getConnectionState() === 'open')) return;
			lastLibraryFallbackAt = now;
			_libraryRefreshGate.request();
		}, 60_000);

		return () => {
			uninstallBrowserSortIpc();
			unregisterPerformanceBrowser();
			connAlive = false;
			clearInterval(connTimer);
			clearInterval(blankSweepTimer);
			clearInterval(libraryFallbackTimer);
			unsubscribeTracks();
			unsubscribePlaylists();
			unsubscribeSmartlists();
			unsubscribeResync();
			unsubscribeSearch();
			window.removeEventListener('keydown', onKey);
			shortViewportMq.removeEventListener('change', applyShortViewport);
		};
	});
	/**
	 * The Library health dot policy now lives in `$lib/rb/library-health-dots`
	 * (pure, unit tested with no component or network mock), the same split
	 * as `meter-math.ts`. `libraryHealth` above calls it directly.
	 */

	/** Liveness poll cadence and per-probe timeout, restored with the dots. */
	const CONN_PING_MS = 2500;
	const CONN_PING_TIMEOUT_MS = 2000;

	async function _pingBackend(): Promise<LibraryHealthDot> {
		try {
			await pingHealth(CONN_PING_TIMEOUT_MS);
			return { label: 'Backend', state: 'complete', detail: 'engine online' };
		} catch (error: unknown) {
			// The reason is kept rather than flattened to "offline": a timeout
			// and a 500 want different things from the reader.
			const why = error instanceof Error ? error.message : String(error);
			return { label: 'Backend', state: 'error', detail: `engine not answering - ${why}` };
		}
	}

	async function _pingFrontend(): Promise<LibraryHealthDot> {
		const { signal, clear } = timeoutSignal(CONN_PING_TIMEOUT_MS);
		try {
			const response = await fetch(`${window.location.origin}/`, {
				method: 'GET',
				cache: 'no-store',
				signal
			});
			if (!response.ok) {
				return {
					label: 'Frontend',
					state: 'error',
					detail: `dev server returned HTTP ${response.status}`
				};
			}
			return { label: 'Frontend', state: 'complete', detail: 'dev server online' };
		} catch (error: unknown) {
			const why = error instanceof Error ? error.message : String(error);
			return { label: 'Frontend', state: 'error', detail: `dev server not answering - ${why}` };
		} finally {
			clear();
		}
	}

	function _coverageDot(
		label: LibraryHealthDot['label'],
		coverage: IngestCoverage,
		step: 'vocals' | 'stems' | 'lyrics'
	): LibraryHealthDot {
		const missing = coverage.missing[step];
		if (typeof missing !== 'number' || !Number.isInteger(missing) || missing < 0) {
			return { label, state: 'unavailable', detail: `${step} coverage could not be measured` };
		}
		if (coverage.on_disk <= 0) {
			return {
				label,
				state: 'unavailable',
				detail: `no playable tracks to measure, ${coverage.unreachable} broken ${coverage.unreachable === 1 ? 'link' : 'links'}`
			};
		}
		const completed = coverage.on_disk - missing;
		if (completed < 0) {
			throw new Error(`${step} coverage missing count exceeds on-disk tracks`);
		}
		// Corruption is a DISTINCT, always-surfaced state - never folded into a
		// quiet 'incomplete'. It is a subset of `missing` (a malformed entry is
		// not done, whatever else it is), so it is checked after validating
		// `missing` but before the ordinary complete/incomplete split. lyrics
		// has no refresh runner (see routes/ingest.py), so a corrupt lyrics
		// entry has NO repair path except this dot saying so.
		const corrupt = coverage.corrupt[step];
		if (typeof corrupt !== 'number' || !Number.isInteger(corrupt) || corrupt < 0) {
			throw new Error(`${step} coverage corrupt count must be a nonnegative integer`);
		}
		if (corrupt > 0) {
			return {
				label,
				state: 'error',
				detail: `${corrupt} corrupt ${corrupt === 1 ? 'entry' : 'entries'} - ${completed}/${coverage.on_disk} playable complete, ${missing} missing, ${coverage.unreachable} broken ${coverage.unreachable === 1 ? 'link' : 'links'}`
			};
		}
		return {
			label,
			state: missing === 0 ? 'complete' : 'incomplete',
			detail: `${completed}/${coverage.on_disk} playable complete, ${missing} missing, ${coverage.unreachable} broken ${coverage.unreachable === 1 ? 'link' : 'links'}`
		};
	}

	async function _loadIngestCoverage(): Promise<void> {
		try {
			const coverage = await getIngestCoverage();
			vocalsCompletion = _coverageDot('Vocals completion', coverage, 'vocals');
			stemsCompletion = _coverageDot('Stems completion', coverage, 'stems');
			lyricsCompletion = _coverageDot('Lyrics completion', coverage, 'lyrics');
		} catch (error: unknown) {
			const detail = error instanceof Error ? error.message : String(error);
			vocalsCompletion = { label: 'Vocals completion', state: 'error', detail };
			stemsCompletion = { label: 'Stems completion', state: 'error', detail };
			lyricsCompletion = { label: 'Lyrics completion', state: 'error', detail };
		}
	}

	async function _loadReconcileSummary(): Promise<void> {
		try {
			const summary = await getReconcileSummary();
			allTracksNonBrokenCount = summary.total_tracks - summary.total_broken;
			allTracksBrokenCount = summary.total_broken;
			allTracksReconcileError = null;
		} catch (error: unknown) {
			allTracksReconcileError = error instanceof Error ? error.message : String(error);
		}
	}

	async function _init(): Promise<void> {
		playlistsLoading = true;
		playlistsError = null;
		const bootHealthEpoch = _healthWriteEpoch;
		const bootPlaylistsEpoch = _playlistsWriteEpoch;
		const bootAllTracksEarly = canBootAllTracksEarly({
			remembered: uiPrefs.last_playlist,
			url_playlist_id: urlPlaylistId,
			source,
			spotify_selected_id: spotifySelectedId
		});
		let bootPaneRestored = false;
		const healthPromise = getHealthAtBoot(getHealth);
		const playlistsPromise = bootPlaylistsPrefetch();
		try {
			await hydrateRuntimePolicy();
			if (bootAllTracksEarly) {
				await bootTracksPrefetch().prefsPromise.catch(() => {});
				const healthRes = await healthPromise;
				libraryHealthError = null;
				allTracksCount = reconcileBootSnapshot({
					bootEpoch: bootHealthEpoch,
					currentEpoch: _healthWriteEpoch,
					bootValue: healthRes.health.state_db.tracks,
					currentValue: allTracksCount
				});
				if (!(source === 'spotify' && spotifySelectedId !== null)) {
					await _restoreBootPane();
					bootPaneRestored = true;
				}
			}
			const lists = await playlistsPromise;
			if (!bootAllTracksEarly) {
				const healthRes = await healthPromise;
				libraryHealthError = null;
				allTracksCount = reconcileBootSnapshot({
					bootEpoch: bootHealthEpoch,
					currentEpoch: _healthWriteEpoch,
					bootValue: healthRes.health.state_db.tracks,
					currentValue: allTracksCount
				});
			}
			playlists = reconcileBootSnapshot({
				bootEpoch: bootPlaylistsEpoch,
				currentEpoch: _playlistsWriteEpoch,
				bootValue: lists,
				currentValue: playlists
			});
			// Playlist navigation is ready even while the initial track pane loads.
			playlistsLoading = false;
			recordPlaylistTreeReadyMs(
				Math.max(0, Math.round(performance.now() - bootTracksPrefetch().startedAt))
			);
			bootScheduler.defer('browser-panel:refresh-playlist-availability', () => {
				void _refreshPlaylists();
			});
			if (_playlistsWriteEpoch === bootPlaylistsEpoch) {
				await _sweepBlankPlaylists(lists);
			}
			if (source === 'spotify' && spotifySelectedId !== null) {
				const selected = playlists.find(
					(playlist) =>
						playlist.vendor === 'spotify' && playlist.playlist_id === spotifySelectedId
				);
				if (selected === undefined) {
					spotifyPendingError = `Spotify playlist ${spotifySelectedId} is not imported`;
				} else {
					_selectSpotifyPlaylist(selected, false);
				}
			} else if (!bootPaneRestored) {
				await _restoreBootPane();
			}
		} catch (exc) {
			libraryHealthError = exc instanceof Error ? exc.message : String(exc);
			playlistsError = String(exc);
			pushToast(`browser init failed: ${String(exc)}`, 'error');
			throw exc;
		} finally {
			playlistsLoading = false;
			// Reconcile runs in finally so it never races the boot tree/pane but still
			// resolves when playlist boot throws (#3750).
			void _loadReconcileSummary();
		}
		// This coverage request is deliberately after primary browser initialization:
		// tree and first track pane must never wait on ingestion accounting.
		void _loadIngestCoverage();
	}

	/**
	 * Open the first pane on boot instead of leaving it blank.
	 *
	 * /performance used to launch with playlist_id=null on every pane, so the
	 * track table was empty until a human clicked a playlist - indistinguishable
	 * from a load that failed. This restores the pane the user last had, falling
	 * back to All Tracks, and deliberately does NOTHING when the library is empty:
	 * an empty table there is the honest state, not a default worth faking.
	 *
	 * _navRestoring suppresses the back-stack entry, matching goBack(): booting
	 * into a pane is not a navigation the user can go "back" from.
	 */
	async function _restoreBootPane(): Promise<void> {
		const target = panes[0];
		if (target.playlist_id !== null) return; // a deep link already claimed it
		const choice = resolveBootPlaylist({
			remembered: uiPrefs.last_playlist,
			known_playlist_ids: treeNodes.map((n) => n.playlist_id),
			all_tracks_count: allTracksCount ?? 0,
			url_playlist_id: urlPlaylistId
		});
		if (choice === null) return; // empty library - keep the explicit empty state
		const node = _nodeForNav({
			playlist_id: choice.playlist_id,
			playlist_name: choice.name,
			selected_id: null,
			scroll_top: 0,
			search: ''
		});
		if (node === null) return;
		_navRestoring = true;
		try {
			await _loadPane(target, {
				playlist_id: node.playlist_id,
				name: node.name,
				track_count: choice.kind === 'all_tracks' ? (allTracksCount ?? 0) : node.track_count,
				broken_count: choice.kind === 'all_tracks' ? (allTracksBrokenCount ?? 0) : node.broken_count,
				kind: node.kind,
				children: node.children
			});
		} finally {
			_navRestoring = false;
		}
	}

	// -------------------------------------------------------- source modes

	function selectSpotifySource(): void {
		source = 'spotify';
		_writeSpotifyQuery(spotifySelectedId);
	}

	function selectCollectionSource(): void {
		source = 'collection';
		_syncCollectionPlaylistQuery();
	}

	function selectSpotifyPlaylist(playlist: PlaylistSummaryHydrated): void {
		_selectSpotifyPlaylist(playlist, true);
	}

	function _selectSpotifyPlaylist(playlist: PlaylistSummaryHydrated, writeQuery: boolean): void {
		spotifySelectedId = playlist.playlist_id;
		rememberSpotifyRecent(playlist.playlist_id);
		if (writeQuery) _writeSpotifyQuery(playlist.playlist_id);
		const node: PlaylistNode = {
			playlist_id: playlist.playlist_id,
			name: playlist.name,
			track_count: playlist.track_count,
			broken_count: playlistBrokenCount(playlist),
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

	function _replaceQueryParams(params: URLSearchParams): void {
		const url = new URL(window.location.href);
		url.search = params.toString();
		replaceState(formatReplaceStateUrl(url), {});
	}

	function _writeSpotifyQuery(playlistId: string | null): void {
		const url = new URL(window.location.href);
		_replaceQueryParams(
			writeLv1(url.searchParams, { source: 'spotify', playlist_id: playlistId })
		);
	}

	function _writeCollectionQuery(playlistId: string | null): void {
		const url = new URL(window.location.href);
		_replaceQueryParams(
			writeLv1(url.searchParams, { source: 'collection', playlist_id: playlistId })
		);
	}

	function _syncCollectionPlaylistQuery(): void {
		const pane = panes[0];
		if (pane.playlist_id === null || isMissingTracksId(pane.playlist_id)) {
			_writeCollectionQuery(null);
			return;
		}
		_writeCollectionQuery(pane.playlist_id);
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
				broken_count: allTracksBrokenCount ?? 0,
				kind: 'all_tracks',
				children: []
			};
		}
		if (snap.playlist_id === MISSING_TRACKS_ID) {
			return missingTracksNode(allTracksBrokenCount ?? 0);
		}
		if (isAutolistId(snap.playlist_id)) {
			return autolistNode(autolistTitle, 0);
		}
		if (snap.playlist_id.startsWith('taglist:')) {
			return {
				playlist_id: snap.playlist_id,
				name: snap.playlist_name,
				track_count: 0,
				broken_count: 0,
				kind: 'taglist',
				children: []
			};
		}
		const found = treeNodes.find((n) => n.playlist_id === snap.playlist_id);
		if (found !== undefined) return found;
		return {
			playlist_id: snap.playlist_id,
			name: snap.playlist_name,
			track_count: 0,
			broken_count: 0,
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
			_setSearchNow(snap.search);
			if (snap.selected_id !== null) p.select(snap.selected_id, false);
			else clearSelection(p);
			p.rememberScroll(snap.scroll_top);
			navEpoch += 1;
			genreFilterUntil = /^genre:/i.test(snap.search) ? Date.now() + GENRE_WINDOW_MS : 0;
		} finally {
			_navRestoring = false;
		}
	}

	function selectPlaylist(node: PlaylistNode, opts?: { newTab?: boolean }): void {
		if (opts?.newTab === true) {
			_openPlaylistInNewTab(node);
			return;
		}
		void runPerformanceCommandFromUi({ type: 'browser_select_playlist', playlist_id: node.playlist_id });
	}

	function selectSmartlist(smartlist: SmartlistSummary): void {
		const node: PlaylistNode = {
			playlist_id: smartlist.id,
			name: smartlist.name,
			track_count: smartlist.count ?? 0,
			broken_count: 0,
			kind: 'smartlist',
			children: []
		};
		void _loadPane(panes[activePane], node);
	}

	async function _selectPlaylistFromCommand(playlistId: string): Promise<void> {
		const node = _nodeForNav({
			playlist_id: playlistId,
			playlist_name: playlistId,
			search: '',
			selected_id: null,
			scroll_top: 0
		});
		if (node === null) throw new Error(`browser_select_playlist: unknown playlist ${playlistId}`);
		if (panes[activePane].playlist_id === node.playlist_id) return;
		_pushNav();
		await _loadPane(panes[activePane], node);
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
				`ALL ${MAX_PANE_SLOTS} LIBRARY TABS ARE LOCKED - unlock one (or free a non-sticky tab) before opening another playlist`,
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
			broken_count: 0,
			kind: payload.kind,
			children: []
		};
		_openPlaylistInNewTab(node);
	}

	function togglePaneSticky(index: number): void {
		if (index < 0 || index >= panes.length) return;
		panes[index].sticky = !panes[index].sticky;
	}

	function addBlankPaneSlot(): void {
		if (!canAddPaneSlot(panes.length)) return;
		panes.push(createPaneStore());
		activePane = panes.length - 1;
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
				broken_count: 0,
				kind: 'playlist',
				children: []
			});
			pushToast(`Saved as playlist "${created.name}"`, 'info');
		} catch (exc) {
			pushToast(`save as playlist failed: ${String(exc)}`, 'error');
		}
	}

	async function _refreshPlaylists(): Promise<void> {
		try {
			playlists = await listPlaylistsHydrated();
			_playlistsWriteEpoch += 1;
			await _sweepBlankPlaylists(playlists);
		} catch (exc) {
			console.error(`[playlists] refresh failed: ${String(exc)}`);
		}
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
	 *
	 * Never call this directly: go through `_libraryRefreshGate.request()`,
	 * which decides when a background refetch may run and coalesces overlapping
	 * triggers. See the comment on that binding.
	 */
	async function _refreshLibraryRowsOnce(): Promise<void> {
		await Promise.all([_loadIngestCoverage(), _loadReconcileSummary(), _refreshPlaylists()]);
		try {
			// `fresh` because this runs OFF a library-change event: a body
			// shared from before that change would paint a stale count and
			// leave it there until the next event. The mount-time read in
			// `_init` above has no such constraint and shares one.
			const healthRes = await getHealthFreshWithRetry(getHealth);
			allTracksCount = healthRes.health.state_db.tracks;
			_healthWriteEpoch += 1;
		} catch (exc) {
			console.error(`[library-refresh] track count refresh failed: ${String(exc)}`);
		}
		for (const p of panes) {
			// A blank pane has nothing to refresh. Skip blocking loads
			// (`loading`) and All Tracks background fills (`load_progress`).
			if (p.playlist_id === null || p.loading || p.load_progress !== null) continue;
			// Snapshotted BEFORE the await, then rechecked after it: the user
			// can switch this pane to another playlist while the fetch is in
			// flight, and writing the response then would paint pane B with
			// pane A's rows. The ETag design means the next WRITE 412s rather
			// than corrupting anything, so this is a display-level fix, but a
			// pane showing another playlist's tracks is still wrong on screen.
			const requestedPlaylistId = p.playlist_id;
			try {
				const result =
					requestedPlaylistId === 'all'
						? await _fetchAllRows()
						: isMissingTracksId(requestedPlaylistId)
							? await fetchMissingTrackRows()
							: isAutolistId(requestedPlaylistId)
								? await _fetchAutolistRows(autolistSelection)
								: p.kind === 'smartlist'
								? await _fetchSmartlistRows(requestedPlaylistId)
								: await _fetchPlaylistRows(requestedPlaylistId);
				if (p.playlist_id !== requestedPlaylistId) continue;
				p.rows = result.rows;
				p.truncated = result.truncated;
				p.etag = result.etag;
				pruneSelection(p, result.rows);
			} catch (exc) {
				console.error(
					`[library-refresh] pane ${requestedPlaylistId} refresh failed: ${String(exc)}`
				);
			}
		}
		// A boot health read that joined an in-flight coalesced entry can settle
		// with a snapshot from BEFORE a change this same trigger exists to react
		// to (request-coalescer.ts's `forceInFlight: false` on the
		// 'initial-connect' resync deliberately leaves such an entry untouched -
		// see its docstring). _restoreBootPane() then saw a falsely-empty
		// library and left panes[0] unclaimed on purpose, and nothing above this
		// point ever retries it (blank panes are explicitly skipped). The fresh
		// count just read above may have corrected that, so retry now rather
		// than stranding the pane blank until a manual reload - see
		// `shouldRetryBootPane`'s own doc comment for the decision and why it
		// is a separate, pure, real-module-tested function.
		if (
			shouldRetryBootPane({
				boot_pane_playlist_id: panes[0].playlist_id,
				source,
				spotify_selected_id: spotifySelectedId
			})
		) {
			await _restoreBootPane();
		}
	}

	/**
	 * The only entry point for a background library refresh: WHEN it may run,
	 * and how many times.
	 *
	 * Five triggers feed it (`subscribeKind('tracks')`, `subscribeKind('playlists')`,
	 * `subscribeKind('smartlists')`, `subscribeResync` and the 60s degraded-path poll)
	 * and a single gap-revealing
	 * `library.changed` frame fires two of them for ONE event. Unguarded that is
	 * concurrent full library reads racing to write the same panes; the gate's
	 * coalescer makes it one run plus one trailing run (`$lib/rb/coalesce`).
	 *
	 * PERFMODE-04 on top of that: no background refetch AT ALL while a deck is
	 * playing. A refresh is a full library read per open pane followed by a
	 * `p.rows` reassignment that re-renders the whole list, and every trigger
	 * above fires from something the DJ did NOT ask for right now - someone
	 * else's rating PATCH, a bulk edit, a find/replace, a WS reconnect. Mid-mix
	 * that is main-thread time competing with the audio graph for no benefit the
	 * DJ can see, because they are looking at the decks.
	 *
	 * Deferred, never dropped: the gate owes the run and pays it the moment
	 * playback stops, or sooner if the user touches the pane (see
	 * `_noteLibraryInteraction`).
	 */
	const _libraryRefreshGate = createPlayingGate({
		kind: 'library-refresh-deferred',
		isPlaying: anyDeckPlaying,
		run: _refreshLibraryRowsOnce
	});

	// The reactive read IS the drain trigger: `anyDeckPlaying` touches every
	// deck's transport state, so this effect re-runs the instant the last deck
	// stops and the owed refresh lands with no polling anywhere.
	//
	// untrack around the drain for the same reason PerfMeters untracks its
	// sampler: the refresh it starts reads and writes pane state, and anything
	// it touches synchronously would otherwise become a dependency of the effect
	// that started it. The transport read is the only dependency this may have.
	$effect(() => {
		if (!anyDeckPlaying()) untrack(() => _libraryRefreshGate.drain());
	});

	/**
	 * Scrolling, selecting or searching the library is an explicit request for
	 * fresh rows, so a deferred refresh is released even though audio is live -
	 * yielded to an idle slot rather than run on the gesture path. A no-op (one
	 * boolean read) when nothing is owed, which is the common case.
	 */
	function _noteLibraryInteraction(): void {
		_libraryRefreshGate.flushOnInteraction();
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

	async function toggleForbidDuplicates(node: PlaylistNode): Promise<void> {
		if (
			node.kind !== 'playlist' ||
			node.playlist_id === 'all' ||
			isMissingTracksId(node.playlist_id) ||
			isAutolistId(node.playlist_id)
		)
			return;
		try {
			const { etag } = await getPlaylistTracksEtag(node.playlist_id);
			await patchPlaylist(node.playlist_id, etag, {
				forbid_duplicates: !node.forbid_duplicates
			});
			node.forbid_duplicates = !node.forbid_duplicates;
			await _refreshPlaylists();
			pushToast(
				node.forbid_duplicates
					? 'Forbid duplicates enabled'
					: 'Forbid duplicates disabled',
				'info'
			);
		} catch (exc) {
			pushToast(`forbid duplicates failed: ${String(exc)}`, 'error');
		}
	}

	async function renamePlaylistUi(node: PlaylistNode, name: string): Promise<void> {
		if (
			node.kind === 'all_tracks' ||
			node.playlist_id === 'all' ||
			isMissingTracksId(node.playlist_id) ||
			isAutolistId(node.playlist_id)
		)
			return;
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
		if (
			node.kind === 'all_tracks' ||
			node.playlist_id === 'all' ||
			isMissingTracksId(node.playlist_id) ||
			isAutolistId(node.playlist_id)
		)
			return;
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

	async function duplicatePlaylistUi(node: PlaylistNode): Promise<void> {
		if (node.kind === 'all_tracks' || node.playlist_id === 'all') return;
		try {
			const { etag } = await getPlaylistTracksEtag(node.playlist_id);
			const copy = await duplicatePlaylist(node.playlist_id, etag);
			await _refreshPlaylists();
			pushToast(`Duplicated as "${copy.name}"`, 'info');
		} catch (exc) {
			pushToast(`duplicate failed: ${String(exc)}`, 'error');
		}
	}

	let folderDupPrompt = $state<{
		rows: UploadFileResult[];
		resolve: (decisions: Map<string, PossibleDupDecision> | null) => void;
	} | null>(null);

	function askPossibleDups(
		rows: UploadFileResult[]
	): Promise<Map<string, PossibleDupDecision> | null> {
		return new Promise((resolve) => {
			folderDupPrompt = { rows, resolve };
		});
	}

	async function folderDropOnPlaylistTree(event: DragEvent): Promise<void> {
		const dt = event.dataTransfer;
		if (dt === null) return;
		// Read the folder name BEFORE the first await: the drag data store is
		// only readable while the drop event is being dispatched.
		const droppedFolder = collectDroppedFolderName(dt);
		const files = await collectDroppedAudioFiles(dt);
		if (files.length === 0) {
			pushToast('No audio files in that drop', 'error');
			return;
		}
		const folderName =
			droppedFolder ?? files[0]?.name.split('/')[0]?.trim() ?? 'New playlist';
		try {
			const result = await ingestFolderToNewPlaylist({
				files,
				folderName,
				onPossibleDups: (rows) => askPossibleDups(rows),
				onQueuedRefreshError: (err) =>
					pushToast(`analysis for "${folderName.trim()}" not started: ${String(err)}`, 'error')
			});
			await _refreshPlaylists();
			await _selectPlaylistFromCommand(result.playlistId);
			pushToast(
				`Created "${folderName.trim()}" with ${result.added} track(s) (${result.staged} new, ${result.skippedDup} linked duplicate(s))`,
				'info'
			);
		} catch (exc) {
			pushToast(`folder drop failed: ${String(exc)}`, 'error');
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
			let effectiveMode: 'add' | 'move' = 'add';
			if (mode === 'move') {
				const srcId = panes[activePane].playlist_id;
				if (srcId !== null && srcId !== 'all' && srcId !== playlistId) {
					const dest = await getPlaylistTracksEtag(playlistId);
					const src = await getPlaylistTracksEtag(srcId);
					await transferPlaylistTracks(playlistId, dest.etag, {
						stable_ids: stableIds,
						mode: 'move',
						source_playlist_id: srcId,
						source_etag: src.etag
					});
					effectiveMode = 'move';
				} else {
					await appendTracksToPlaylist(playlistId, stableIds);
				}
			} else {
				await appendTracksToPlaylist(playlistId, stableIds);
			}
			if (effectiveMode === 'move') {
				const node = _currentNode(panes[activePane]);
				if (node !== null) await _loadPane(panes[activePane], node);
			}
			await _refreshPlaylists();
			pushToast(
				`${effectiveMode === 'add' ? 'Added' : 'Moved'} ${stableIds.length} track(s)`,
				'info'
			);
		} catch (exc) {
			pushToast(`playlist drop failed: ${String(exc)}`, 'error');
		}
	}

	function loadAutolistUi(selection: AutolistSelection, title: string): void {
		autolistSelection = selection;
		autolistTitle = title;
		const node = autolistNode(title, 0);
		if (pane.playlist_id !== AUTOLIST_ID) _pushNav();
		void _loadPane(panes[activePane], node);
	}

	async function _fetchAutolistRows(
		selection: AutolistSelection
	): Promise<{ rows: BrowserRow[]; truncated: boolean; etag: string }> {
		if (!hasAutolistSelection(selection)) {
			return { rows: [], truncated: false, etag: '' };
		}
		const page = await queryAutolists(selection, 0, PAGE_SIZE);
		const rows = page.tracks.map((wire, i) =>
			_rowFromPlaylistWire(wire as PlaylistTrackRowWire, i + 1)
		);
		return { rows, truncated: page.total > rows.length, etag: '' };
	}

	async function _loadPane(p: PaneStore, node: PlaylistNode): Promise<void> {
		// Every route into a pane funnels through here (tree click, new tab,
		// back-stack, post-mutation refresh), so this is the one place that
		// needs to remember the selection for the next boot. Folders are not
		// loadable panes, so only the two real kinds are recorded.
		if (
			p === panes[0] &&
			node.kind !== 'folder' &&
			node.kind !== 'missing_tracks' &&
			node.kind !== 'taglist' &&
			node.kind !== 'smartlist' &&
			node.kind !== 'autolist'
		) {
			setLastPlaylist({
				playlist_id: node.playlist_id,
				name: node.name,
				kind: node.kind
			});
			if (source === 'collection' && (node.kind === 'playlist' || node.kind === 'all_tracks')) {
				_writeCollectionQuery(node.playlist_id);
			}
		}
		// beginLoad returns the stale-response token for rapid re-selection;
		// completeLoad/failLoad no-op when a newer load superseded this one.
		const seq = p.beginLoad(node.playlist_id, node.name, node.kind);
		try {
			if (node.kind === 'autolist') {
				await fillAutolistPane({
					pane: p,
					seq,
					selection: autolistSelection,
					pageSize: PAGE_SIZE,
					fetchPage: (offset, limit) => queryAutolists(autolistSelection, offset, limit),
					mapRow: (wire, order) =>
						_rowFromPlaylistWire(wire as PlaylistTrackRowWire, order),
					onFillError: (error) => pushToast(`autolist load failed: ${error}`, 'error')
				});
				return;
			}
			if (node.kind === 'all_tracks') {
				const switchStartedAt = performance.now();
				await fillAllTracksPane({
					pane: p,
					seq,
					fetchPage: (cursor) => fetchBootTracksFirstPage(cursor),
					mapRow: (t, order) => _rowFromListWire(t, order),
					progressTotal: allTracksNonBrokenCount,
					onFirstPaint: () => {
						recordPlaylistSwitchFirstRowsMs(
							'all-tracks',
							performance.now() - switchStartedAt
						);
						recordOpenToLibraryRows({ source: 'all-tracks' });
						completeLibraryUsable({ source: 'all-tracks' });
					},
					onComplete: (info) => recordLibraryLoadTiming('all-tracks', info),
					onFillError: (error) => pushToast(`playlist load failed: ${error}`, 'error')
				});
				return;
			}
			if (node.kind === 'taglist') {
				await fillAllTracksPane({
					pane: p,
					seq,
					fetchPage: (cursor) =>
						listTracksHydrated({ limit: PAGE_SIZE, cursor, tag: node.name }),
					mapRow: (t, order) => _rowFromListWire(t, order),
					progressTotal: node.track_count,
					onFirstPaint: () => {
						recordOpenToLibraryRows({ source: 'all-tracks' });
						completeLibraryUsable({ source: 'all-tracks' });
					},
					onComplete: (info) => recordLibraryLoadTiming('all-tracks', info),
					onFillError: (error) => pushToast(`taglist load failed: ${error}`, 'error')
				});
				return;
			}
			if (node.kind === 'playlist') {
				const switchStartedAt = performance.now();
				await fillPlaylistPane({
					pane: p,
					seq,
					pageSize: PLAYLIST_FIRST_PAGE,
					fetchPage: (offset) =>
						listPlaylistTracksPage(node.playlist_id, {
							limit: PLAYLIST_FIRST_PAGE,
							offset
						}),
					mapRow: (wire, order) => _rowFromPlaylistWire(wire, order),
					progressTotal: node.track_count,
					onFirstPaint: () => {
						recordPlaylistSwitchFirstRowsMs(
							'playlist',
							performance.now() - switchStartedAt
						);
						recordOpenToLibraryRows({ source: 'playlist' });
						completeLibraryUsable({ source: 'playlist' });
					},
					onComplete: (info) => recordLibraryLoadTiming('playlist', info),
					onFillError: (error) => pushToast(`playlist load failed: ${error}`, 'error')
				});
				return;
			}
			const result =
				node.kind === 'missing_tracks'
					? await fetchMissingTrackRows()
					: await _fetchSmartlistRows(node.playlist_id);
			p.completeLoad(seq, result.rows, result.truncated, result.etag);
			if (node.kind === 'smartlist') {
				recordOpenToLibraryRows({ source: 'playlist' });
				completeLibraryUsable({ source: 'playlist' });
			}
		} catch (exc) {
			if (p.failLoad(seq, String(exc))) {
				pushToast(`playlist load failed: ${String(exc)}`, 'error');
			}
		}
	}

	/** Reconstructs the minimal PlaylistNode _loadPane needs to refresh the
	 * currently-selected pane after a mutation (add-remove-reorder-tracks). */
	function _currentNode(p: PaneStore): PlaylistNode | null {
		if (p.playlist_id === null || p.playlist_id === 'all' || isMissingTracksId(p.playlist_id)) return null;
		if (isAutolistId(p.playlist_id) || p.kind === 'autolist') {
			return autolistNode(p.title, p.rows.length);
		}
		if (p.kind === 'smartlist') {
			return {
				playlist_id: p.playlist_id,
				name: p.title,
				track_count: p.rows.length,
				broken_count: 0,
				kind: 'smartlist',
				children: []
			};
		}
		if (p.playlist_id.startsWith('taglist:')) {
			return {
				playlist_id: p.playlist_id,
				name: p.title,
				track_count: p.rows.length,
				broken_count: 0,
				kind: 'taglist',
				children: []
			};
		}
		return {
			playlist_id: p.playlist_id,
			name: p.title,
			track_count: p.rows.length,
			broken_count: 0,
			kind: 'playlist',
			children: []
		};
	}

	function _rowFromSearchHit(wire: SearchHitWire, order: number): BrowserRow {
		return { ..._rowFromPlaylistWire(wire, order), match_context: wire.match_context };
	}

	async function _fetchAllRows(
		onPage?: (info: { loaded: number; pageCount: number }) => void
	): Promise<{ rows: BrowserRow[]; truncated: boolean; etag: string }> {
		// All Tracks: walk every cursor page with inline preview/file_exists
		// (contract 1). PAGE_SIZE is per request, while TrackTable's DOM
		// virtualization keeps the rendered pane bounded. ?available stays
		// server-default 'all': FR-1 hiding is client-side so the toggle
		// flips instantly on loaded panes; the API filter exists for agent
		// parity, not for this UI path.
		// One ring row per completed walk (PERF-R5 Q9). Both the user-initiated
		// load and the background refresh land here, and both are the same
		// ~8k-row cost, so this is the one place that needs the clock.
		const startedAt = performance.now();
		const items = await fetchAllPages(
			(cursor) => listTracksHydrated({ limit: PAGE_SIZE, cursor }),
			onPage !== undefined ? { onPage } : {}
		);
		const rows = items.map((t, i) => _rowFromListWire(t, i + 1));
		recordLibraryLoadTiming('all-tracks', {
			fetchMs: performance.now() - startedAt,
			rows: rows.length
		});
		return {
			rows,
			truncated: false,
			// All Tracks is not a single playlist row - no membership etag.
			etag: ''
		};
	}

	async function _fetchSmartlistRows(
		id: string
	): Promise<{ rows: BrowserRow[]; truncated: boolean; etag: string }> {
		const startedAt = performance.now();
		const detail = await getSmartlistTracks(id);
		const rows = detail.tracks.map((wire, i) =>
			_rowFromPlaylistWire(
				{
					...wire,
					has_remote_copy: wire.has_remote_copy ?? false,
					cloud_transfer: wire.cloud_transfer ?? null
				},
				i + 1
			)
		);
		recordLibraryLoadTiming('playlist', {
			fetchMs: performance.now() - startedAt,
			rows: rows.length
		});
		return { rows, truncated: false, etag: '' };
	}

	async function _fetchPlaylistRows(
		id: string
	): Promise<{ rows: BrowserRow[]; truncated: boolean; etag: string }> {
		// Hydrated detail (contract 4) + the playlist's membership ETag
		// (add-remove-reorder-tracks: required If-Match for the write side's
		// PUT .../tracks) in one request. Never client-slice membership:
		// TrackTable's DOM virtualization keeps rendering cheap instead.
		const startedAt = performance.now();
		const { detail, etag } = await getPlaylistTracksEtag(id);
		const rows = detail.tracks.map((wire, i) => _rowFromPlaylistWire(wire, i + 1));
		recordLibraryLoadTiming('playlist', {
			fetchMs: performance.now() - startedAt,
			rows: rows.length
		});
		return { rows, truncated: false, etag };
	}

	// -------------------------------------------- lazy per-row hydration
	// rb-meta remains lazy for genre/streaming and analysis fallbacks. Artwork,
	// strips, and file_exists arrive inline; the
	// observer also flips row.revealed for the one-time canvas draw.

	function rowVisible(row: BrowserRow): void {
		row.revealed = true;
		void _hydrateRowMeta(row);
	}

	async function _hydrateRowMeta(row: BrowserRow): Promise<void> {
		// Pre-#737 a track with no rekordbox mapping had every rb-meta field
		// pinned to a known constant (artwork/analysis always false), so the
		// round-trip could only confirm what the row already knew and was
		// skipped outright. #737 made artwork_available a real embedded-tag
		// read for these rows (see _local_rb_meta), so skipping the fetch
		// left the main browser permanently blind to it -- the artwork cell
		// stayed empty even when /artwork could serve real bytes. Unmapped
		// rows now pay the same one-fetch-per-visible-row cost mapped rows
		// already pay via this same IntersectionObserver-gated path.
		if (row.rb_meta !== null || _inflight.has(row.stable_id)) return;
		_inflight.add(row.stable_id);
		try {
			row.rb_meta = await _fetchRbMetaWithRetry(row.stable_id);
		} catch (exc) {
			// A track with no rekordbox vendor mapping is NOT an error any more:
			// rb-meta answers 200 with the local-vendor payload. A 404 here now
			// means an unknown stable_id, which is a real fault worth logging.
			// Loud but non-modal: a toast per row would spam during scrolling.
			// Transient fails leave rb_meta null; rowVisible can retry later
			// when the row re-enters the observer (inflight cleared).
			console.error(`rb-meta hydration failed for ${row.stable_id}:`, exc);
		} finally {
			_inflight.delete(row.stable_id);
		}
	}

	/** One retry on non-404 failure so a blip does not leave the row art-dead.
	 * A 404 (unknown stable_id) is terminal - retrying it just doubles the noise. */
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
			row.rating = track.rating ?? null;
			row.etag = fresh;
		} catch (exc) {
			if (exc instanceof ConflictError) {
				row.rating = exc.current.rating ?? null;
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
		file_availability?: BrowserRow['file_availability'];
	};

	/** PERF-RB-01: disk truth not probed yet (file_exists null). Refused with
	 * its own reason, never reported as a missing file. */
	function _availabilityPending(row: LoadableRow): boolean {
		return row.file_availability === 'AVAILABILITY_PENDING' || row.file_exists === null;
	}

	function loadRow(
		row: LoadableRow,
		deck: DeckId | null,
		opts: { play?: boolean; reservation?: number; pressT0Ms?: number } = {}
	): void {
		void _loadOntoDeck(row, deck, opts);
	}

	function loadSuggest(sid: string, opts: { play?: boolean; pressT0Ms?: number } = {}): void {
		const row = { stable_id: sid, file_exists: true, is_streaming: false };
		if (opts.play) {
			const picked = pickDoubleDeck(row);
			if (picked === null) return;
			// pickDoubleDeck already reserved this deck - see _loadOntoDeck's
			// reservation gate.
			loadRow(row, picked.deck, {
				play: true,
				reservation: picked.reservation,
				...(opts.pressT0Ms === undefined ? {} : { pressT0Ms: opts.pressT0Ms })
			});
			return;
		}
		loadRow(row, null);
	}

	const playlistMemberIds = $derived(new Set(pane.rows.map((r) => r.stable_id)));
	const suggestTargetDeck = $derived(_lowestFreeDeck());
	const suggestPlayTargetDeck = $derived(suggestTargetDeck === null ? null : _pickDoubleDeckTarget().deck);
	const masterRef = $derived(
		DECK_IDS.map((d) => decks[d]).find((d) => d.is_master) ?? null
	);
	/** Filters change AutoPlay's candidate set. Sort state is intentionally absent. */
	const autoPlayFilterKey = $derived(
		[
			searchMode === 'find' ? '' : pane.search.trim(),
			searchMode,
			wholeCollectionActive ? 'collection' : 'playlist',
			uiPrefs.hide_broken_links ? 'hide-broken' : 'show-broken',
			uiPrefs.next_only_filter ? 'next-only' : 'all-next',
			nextOnlyRef?.key ?? '',
			nextOnlyRef?.bpm ?? ''
		].join('|')
	);

	/** Holds the order AutoPlay walks, frozen at activation except for active
	 * filter changes (see createAutoPlayFeedSnapshot in auto-play.ts). */
	const autoPlayFeed = createAutoPlayFeedSnapshot();
	let autoPlaySnapshotActive = $state(false);
	let autoPlaySnapshotMatchesView = $state(false);

	// The AutoPlay column is hidden while disabled. Clear its active sort through
	// each pane's own owner before that happens, so the visible and published feed
	// return to that pane's natural order rather than a stale rank map. Every pane
	// is checked, not just the active one: switching tabs never mounts the other
	// three, so a sort left on an inactive pane would otherwise resurface silently
	// the next time the user tabs back to it.
	$effect(() => {
		if (uiPrefs.auto_play_enabled) return;
		for (const p of panes) {
			if (p.sort_key === 'autoplay') p.toggleSort('autoplay');
		}
	});

	// Feed AutoPlay from the SORTED, filtered view the user is actually looking
	// at - not pane.rows, which is raw stored membership and ignored the sort
	// outright.
	//
	// This means the view's own filters govern what AutoPlay can reach, which
	// is the point: with 'Hide broken links' ON, broken rows are not on screen
	// and are not candidates either. With it OFF they are fed through carrying
	// file_exists: false, and pick skips them and still reports the
	// missing-audio case. file_exists is never invented in either direction.
	//
	// The snapshot decides WHEN this may change: on activation it takes the
	// current view, and while AutoPlay runs it publishes nothing, so re-sorting
	// mid-set cannot re-order a set in flight.
	$effect(() => {
		const decision = autoPlayFeed.step(
			uiPrefs.auto_play_enabled,
			pane.playlist_id,
			renderedRows.map((r) => ({
				stable_id: r.stable_id,
				key: r.key,
				bpm: r.bpm,
				// Pending (null) is not playable yet; AutoPlay skips it like a
				// missing row until the disk probe settles it.
				file_exists: r.file_exists === true,
				title: r.title,
				artist: r.artist
			})),
			autoPlayFilterKey
		);
		autoPlaySnapshotActive = autoPlayFeed.active;
		autoPlaySnapshotMatchesView = autoPlayFeed.matches(visibleRows);
		if (decision.publish !== null) setAutoPlayTrackFeed(pane.playlist_id, decision.publish);
	});

	/** Monotonic load counter - double-click prefers least-recent in the pair. */
	let deckLoadSeq = $state({ 1: 0, 2: 0, 3: 0, 4: 0 });
	let deckLoadTick = 0;
	/** Last deck a plain (non-replace) double-click targeted; cmd/ctrl+dblclick reuses it. */
	let lastDoubleClickDeck = $state<DeckId | null>(null);
	/**
	 * True from the instant a deck is reserved until its load/unload settles
	 * or the confirm-before-load dialog is dismissed without loading.
	 * loadSeq alone cannot tell a still-in-flight reservation apart from one
	 * that settled long ago and is now legitimately idle again - both just
	 * read as "touched more recently than 0". A picker call that lands
	 * mid-flight needs the sharper signal to exclude that deck outright
	 * rather than merely rank it lower within its tier.
	 */
	let deckReservationPending = $state<Record<DeckId, boolean>>({
		1: false,
		2: false,
		3: false,
		4: false
	});
	/**
	 * Reservation identity, deliberately separate from deckLoadSeq: a
	 * successful load bumps deckLoadSeq itself (recency for the picker), and
	 * gating release on THAT counter meant every successful load bumped it
	 * out from under its own reservation's finally, so the equality check in
	 * _releaseDeckReservation always failed and deckReservationPending never
	 * cleared after a successful load (r3920224748). This counter changes
	 * only at reservation time, never at load-settle time, so it stays
	 * stable across the reservation's own load.
	 */
	let deckReservationTick = 0;
	let deckReservationGen = $state<Record<DeckId, number>>({ 1: 0, 2: 0, 3: 0, 4: 0 });

	/**
	 * Reserve a deck slot the instant it is chosen, synchronously, before
	 * the (async) load command even starts. Regression Mon 17 Aug 2026: the
	 * old code bumped deckLoadSeq only after the load command resolved, so
	 * double-clicking two tracks in quick succession (before the first
	 * load's await settled) had both clicks read the same stale deckLoadSeq
	 * and pick the SAME deck - the second track silently replaced the first
	 * instead of landing on the other deck.
	 */
	// Returns the reservation's generation (deckReservationTick at the moment
	// of reservation) so the caller can release EXACTLY this reservation
	// later, not whichever one happens to be live on the deck by then - see
	// _releaseDeckReservation.
	function _reserveDeckSlot(deck: DeckId): number {
		deckLoadTick += 1;
		deckLoadSeq = { ...deckLoadSeq, [deck]: deckLoadTick };
		deckReservationPending = { ...deckReservationPending, [deck]: true };
		deckReservationTick += 1;
		deckReservationGen = { ...deckReservationGen, [deck]: deckReservationTick };
		return deckReservationTick;
	}

	/**
	 * Releases a reservation once its load/unload settles (_loadOntoDeck's
	 * finally) or its confirm-before-load dialog is dismissed without
	 * loading (onloadconfirmcancelled) - the two ways a reservation can end
	 * without ever changing stable_id/playing/fader.
	 *
	 * Gated on `generation` matching the deck's CURRENT deckReservationGen,
	 * not just "this deck has a pending flag" - deck-slots.ts's all-pending
	 * fallback deliberately lets a later double-click re-reserve an
	 * already-reserved deck (refusing every gesture once all 4 decks are
	 * mid-load would be worse), which creates two in-flight owners of one
	 * deck. Without this check, the EARLIER owner's finally clears the flag
	 * while the LATER owner is still loading, exposing the deck to a third
	 * gesture that collides with the still-in-flight load (r3919940011).
	 * Checked against deckReservationGen, NOT deckLoadSeq: deckLoadSeq is
	 * also bumped on load SUCCESS (recency for the picker), so gating on it
	 * meant a reservation's own successful load invalidated its own release
	 * (r3920224748).
	 */
	function _releaseDeckReservation(deck: DeckId, generation: number): void {
		if (deckReservationGen[deck] !== generation) return;
		deckReservationPending = { ...deckReservationPending, [deck]: false };
	}

	/** Shared read-only decision for the play label and the click reservation. */
	function _pickDoubleDeckTarget(
		opts: { shift?: boolean; replace?: boolean } = {}
	): ReturnType<typeof pickDoubleClickDeck> {
		// The picker needs master, playing and fader now, not just a load
		// counter: it used to be able to take the live master (pin
		// d2c156a503bb) precisely because those were invisible to it.
		const slots = {} as Record<DeckId, DeckSlotState>;
		for (const d of DECK_IDS) {
			slots[d] = {
				stable_id: decks[d].stable_id,
				playing: decks[d].playing,
				is_master: decks[d].is_master,
				fader: mixerState.channels[d].fader,
				loadSeq: deckLoadSeq[d],
				reservationPending: deckReservationPending[d]
			};
		}
		return pickDoubleClickDeck({
			shift: opts.shift === true,
			replace: opts.replace === true,
			decks: slots,
			lastDoubleClickDeck
		});
	}

	/** Reserve the published picker decision synchronously before loading. */
	function pickDoubleDeck(
		_row: LoadableRow,
		opts: { shift?: boolean; replace?: boolean } = {}
	): { deck: DeckId; reservation: number } | null {
		const result = _pickDoubleDeckTarget(opts);
		if (result.deck === null) {
			if (result.error !== null) pushToast(result.error, 'error');
			return null;
		}
		const reservation = _reserveDeckSlot(result.deck);
		if (opts.replace !== true) lastDoubleClickDeck = result.deck;
		return { deck: result.deck, reservation };
	}

	/**
	 * CUEOUT-15: a click on a library mini-waveform means "play it in my ears
	 * from here", always, whether or not the track is also on a deck.
	 *
	 * It used to seek EVERY deck holding that stable_id, with no check on
	 * `playing` and none on `is_master`, so a click while browsing could jump
	 * a deck that was live on air. `_loadOntoDeck` guards the master three
	 * separate ways for exactly that reason; this path guarded nothing. The
	 * browser is now a monitoring surface and never a transport control:
	 * moving a deck is what the deck's own waveform and CUE are for.
	 *
	 * `previewCueSeek` owns every refusal, because only it can tell a missing
	 * engine from a dead sink from a MIX knob at the master end.
	 */
	function previewSeek(row: LoadableRow & { bpm?: number | null }, ratio: number): void {
		// Same refusal the deck load gives (FR-1), and for the same reason: a
		// broken link has no audio to preview, and finding that out as an
		// opaque decoder error several hundred milliseconds later teaches the
		// operator nothing. `is_streaming` has no local file at all.
		if (row.is_streaming ?? row.rb_meta?.is_streaming ?? false) {
			pushToast('preview: streaming track has no local audio to preview', 'error');
			return;
		}
		if (_availabilityPending(row)) {
			pushToast('preview: availability still checking (wait for disk probe)', 'error');
			return;
		}
		if (!row.file_exists) {
			pushToast('preview: audio file missing on disk (broken link)', 'error');
			return;
		}
		// The row already carries the analyzed BPM, so the tempo match (CUEOUT-15
		// R6) costs no request on the click path.
		void previewCueSeek(row.stable_id, Math.max(0, Math.min(1, ratio)), {
			trackBpm: row.bpm ?? null
		}).then((outcome) => {
			if (!outcome.ok) {
				if (outcome.reason !== PREVIEW_SUPERSEDED) pushToast(outcome.reason, 'error');
			} else if (outcome.warning !== null) {
				pushToast(outcome.warning, 'warn');
			}
		});
	}

	async function _loadOntoDeck(
		row: LoadableRow,
		deck: DeckId | null,
		opts: { play?: boolean; reservation?: number; pressT0Ms?: number } = {}
	): Promise<void> {
		// A picker-chosen `deck` carries a reservation (_reserveDeckSlot) that
		// must be released on EVERY exit path here - refusal, error, or
		// success - or that slot stays wrongly excluded from every future
		// pick (see deck-slots.ts's reservationPending). Releasing is gated on
		// `opts.reservation`, not merely on `deck !== null`: the "Load onto
		// deck N" button also passes a non-null deck it never reserved, and an
		// unconditional release would clear a DIFFERENT, still-open
		// reservation on that same deck (e.g. a pending double-click confirm
		// dialog) out from under it. See _releaseDeckReservation for why a
		// generation number, not a boolean, is what makes that safe.
		let loadIntent: ReturnType<typeof beginPendingLoadPlay> | null = null;
		try {
			if (row.is_streaming ?? row.rb_meta?.is_streaming ?? false) {
				pushToast('streaming track - deck load not implemented (see PARITY-TODO)', 'error');
				return;
			}
			if (_availabilityPending(row)) {
				pushToast('cannot load: availability still checking (wait for disk probe)', 'error');
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
			// The master is never a victim (pin d2c156a503bb) - pickDoubleClickDeck
			// enforces this at PICK time, but an explicit target can point at a
			// deck that has since become master (a confirm dialog can sit open
			// for any length of time - r3919940009), or that was always master
			// (the "Load onto deck N" button never consults the picker at all).
			// Revalidate at the actual load boundary, the one place every path
			// converges, instead of trusting a pick made earlier.
			if (decks[target].is_master) {
				pushToast(`cannot load: CH${target} is the live master - unload or reassign it first`, 'error');
				return;
			}
			try {
				loadIntent = beginPendingLoadPlay(target, opts.play === true);
				await dispatchPerformanceCommand(
					{
						type: 'load_play_intent',
						deck: target,
						generation: loadIntent.generation,
						desired_play: loadIntent.desiredPlay
					},
					opts.pressT0Ms
				);
				// Explicit CH load (incl. confirmed double-click): replace if occupied.
				// refuseIfMaster: true on both - this is a destructive REPLACE, not
				// a standalone eject, so it must stay refused if `target` raced to
				// master between the check above and here (r3920224754). A
				// standalone unload (Deck.svelte's Unload button, Quick Draw) does
				// NOT set this: it must still be able to eject the live master with
				// no other deck to reassign to (r3920297846).
				if (deck !== null && decks[target].stable_id !== null) {
					await dispatchPerformanceCommand({ type: 'unload', deck: target, refuseIfMaster: true });
				}
				await dispatchPerformanceCommand({
					type: 'load',
					deck: target,
					stable_id: row.stable_id,
					refuseIfMaster: true
				});
				deckLoadTick += 1;
				deckLoadSeq = { ...deckLoadSeq, [target]: deckLoadTick };
				const pendingPlay = consumePendingLoadPlay(target, loadIntent.generation);
				if (pendingPlay?.desiredPlay === true) {
					// Q1: timed from the operator's ORIGINAL keydown, which is
					// what they felt, not from this dispatch downstream of the
					// load they were waiting on.
					// This dispatch only ever fires after `load` above has
					// resolved, so a stamp reaching it always spans this
					// deck's load - mark it before it is spent (r3974057968).
					if (pendingPlay.pressT0Ms !== undefined) {
						markLoadSpanningPress(pendingPlay.pressT0Ms);
					}
					await dispatchPerformanceCommand(
						{
							type: 'play',
							deck: target,
							playing: true,
							...(pendingPlay.quantize === true ? { quantize: true } : {})
						},
						pendingPlay.pressT0Ms
					);
				}
			} catch (error: unknown) {
				const message = error instanceof Error ? error.message : String(error);
				if (message.includes('must be fully stopped before replacement')) {
					_offerUnload(target);
				}
				// Dispatcher already toasted + recorded the deck alert.
			}
		} finally {
			if (loadIntent !== null) clearPendingLoadPlay(loadIntent.deck, loadIntent.generation);
			if (deck !== null && opts.reservation !== undefined) {
				_releaseDeckReservation(deck, opts.reservation);
			}
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

	let playlistTreeResizePointerId = $state<number | null>(null);

	function _resizePlaylistTree(event: PointerEvent): void {
		if (playlistTreeResizePointerId !== event.pointerId) return;
		const separator = event.currentTarget as HTMLElement;
		const browser = separator.closest<HTMLElement>('.rb-browser');
		if (browser === null) throw new Error('playlist tree resize separator is outside the browser panel');
		const requestedWidth = event.clientX - browser.getBoundingClientRect().left - 30;
		uiPrefs.playlist_tree_width = Math.round(
			Math.min(PLAYLIST_TREE_WIDTH_MAX, Math.max(PLAYLIST_TREE_WIDTH_MIN, requestedWidth))
		);
	}

	function _startPlaylistTreeResize(event: PointerEvent): void {
		playlistTreeResizePointerId = event.pointerId;
		(event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
		_resizePlaylistTree(event);
	}

	function _finishPlaylistTreeResize(event: PointerEvent): void {
		if (playlistTreeResizePointerId !== event.pointerId) return;
		playlistTreeResizePointerId = null;
		setPlaylistTreeWidth(uiPrefs.playlist_tree_width);
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
	function selectRow(
		row: Pick<BrowserRow, 'stable_id'> & { order?: number },
		event?: MouseEvent
	): void {
		_noteLibraryInteraction();
		const p = panes[activePane];
		if (p.selected_id !== row.stable_id) _pushNav();
		const extend = event !== undefined && (event.metaKey || event.ctrlKey);
		const range = event !== undefined && event.shiftKey;
		const orderedIds = range ? renderedRows.map((r) => r.stable_id) : [];
		const orderedRows = range
			? renderedRows.map((r) => ({ stable_id: r.stable_id, order: r.order }))
			: undefined;
		p.select(row.stable_id, extend, range, orderedIds, row.order, orderedRows);
		// Warm /anlz so a subsequent deck load shares the in-flight fetch.
		// Selecting an unmapped row is the only place OUR ffmpeg decode ever
		// runs (issue #735); the strip-adoption effect below picks up the
		// result (including a later ambient retry) once it lands in the cache.
		ensureAnlzPrefetch(row.stable_id);
		// Warm audio ArrayBuffer in background (never awaited - see
		// audio-prefetch-cache.svelte.ts). Saves ~1s fetchAudio on warm load.
		ensureAudioPrefetch(row.stable_id);
	}

	/**
	 * Trailing-edge debounce for the LOCAL filter, one per pane (PERF-R5 Q9).
	 *
	 * `pane.search` feeds the visibleRows $derived, so writing it per keystroke
	 * ran filterRows + sortRows over the whole pane array once per character.
	 * The keystroke itself still lands instantly - SearchBox echoes its own
	 * draft - only the recompute waits for the burst to settle.
	 */
	const _filterDebounces: Record<number, FilterDebounce> = {};

	function _filterDebounceFor(paneIndex: number): FilterDebounce {
		const existing = _filterDebounces[paneIndex];
		if (existing !== undefined) return existing;
		const created = createFilterDebounce((settle) => void _applyFilter(paneIndex, settle));
		_filterDebounces[paneIndex] = created;
		return created;
	}

	async function _applyFilter(paneIndex: number, settle: FilterSettle): Promise<void> {
		const p = panes[paneIndex];
		p.setSearch(settle.query);
		// Only the ACTIVE pane owns the visibleRows derived, so a burst that
		// settles after a pane switch still applies its query but has no
		// recompute of its own to report.
		if (paneIndex !== activePane) return;
		// Wait for the flush so the number below is the pass the user actually
		// waited on, rather than a second pass run purely to measure the first.
		await tick();
		recordFilterTiming({
			keystrokes: settle.keystrokes,
			coalescedMs: settle.coalescedMs,
			computeMs: _lastVisibleComputeMs,
			rowsIn: wholeCollectionActive ? p.search_results.length : p.rows.length,
			rowsOut: renderedRows.length
		});
	}

	function setSearch(next: string): void {
		const paneIndex = activePane;
		_filterDebounceFor(paneIndex).push(next);
		_noteLibraryInteraction();
		if (searchMode === 'find') return;
		if (!panes[paneIndex].whole_collection) return;
		// Genre chip filters stay client-side (filterRows), never FTS.
		if (/^genre:/i.test(next.trim())) return;
		// The FTS debounce is longer than the local one, so `pane.search` has
		// always settled by the time this fires and its staleness guard still
		// compares against the query the user actually typed.
		clearTimeout(_searchDebounce[paneIndex]);
		_searchDebounce[paneIndex] = setTimeout(
			() => void _searchWholeCollection(panes[paneIndex], next),
			SEARCH_DEBOUNCE_MS
		);
	}

	/** Programmatic search write (genre chip, clear, restore). Immediate, and
	 * cancels any pending keystroke settle so a superseded burst cannot land
	 * on top of what was just clicked. */
	function _setSearchNow(next: string, request?: BrowserSearchRequest): void {
		_filterDebounceFor(activePane).cancel();
		panes[activePane].setSearch(next);
		if (request === undefined) return;
		// A whole-collection command answers from server FTS, so its result is
		// only knowable once that lands; everything else is already rendered.
		// Genre chip filters stay client-side (filterRows), never FTS.
		const active = panes[activePane];
		if (active.whole_collection && !/^genre:/i.test(next.trim())) {
			void _searchWholeCollection(active, next, request);
		} else _reportBrowserSearchResult(request);
	}

	function _reportBrowserSearchResult(request: BrowserSearchRequest): void {
		// A newer command superseded this one while its FTS was in flight. These
		// rows are not its answer, and reporting them throws on the stale guard
		// inside an unawaited promise.
		if (!isCurrentBrowserSearch(request)) return;
		const result = _computeVisibleSearchResult();
		reportBrowserSearchResult(request, {
			fallback: result.ignoredFilters.length > 0,
			ignoredFilters: result.ignoredFilters,
			rowCount: result.rows.length
		});
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
		// Drop a burst still waiting to settle - it would otherwise repaint the
		// query the user just cleared, 80 ms after the pane returned.
		_filterDebounceFor(activePane).cancel();
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
			else clearSelection(p);
			p.rememberScroll(snap.scroll_top);
			navEpoch += 1;
		} finally {
			_navRestoring = false;
		}
	}

	/** Genre chip → search box (`genre:` / `genre:~` / clear / undo).
	 * A chip click is one discrete gesture, not a burst, so it writes through
	 * _setSearchNow rather than paying the keystroke debounce. */
	function genreFilter(mode: 'strict' | 'loose' | 'clear' | 'undo', tag?: string): void {
		const p = panes[activePane];
		if (mode === 'clear') {
			_setSearchNow('');
			genreFilterUntil = 0;
			return;
		}
		if (mode === 'undo') {
			_setSearchNow(genreFilterPrior);
			genreFilterUntil = 0;
			return;
		}
		const clean = (tag ?? '').trim();
		if (clean === '') return;
		const next = mode === 'loose' ? `genre:~${clean}` : `genre:${clean}`;
		const cur = p.search.trim();
		// Same tag again clears (way back without needing triple / 20s window).
		if (cur.toLowerCase() === next.toLowerCase()) {
			_setSearchNow('');
			genreFilterUntil = 0;
			return;
		}
		genreFilterPrior = cur;
		_setSearchNow(next);
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
			? { playlist_id: 'all', name: 'All Tracks', track_count: 0, broken_count: 0, kind: 'all_tracks' as const, children: [] }
			: isMissingTracksId(pane.playlist_id)
				? missingTracksNode(allTracksBrokenCount ?? 0)
				: isAutolistId(pane.playlist_id)
					? autolistNode(autolistTitle, 0)
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

	async function _searchWholeCollection(
		active: PaneStore,
		query: string,
		request?: BrowserSearchRequest
	): Promise<void> {
		const trimmed = query.trim();
		if (trimmed === '') {
			active.search_results = [];
			active.search_total = 0;
			active.searching = false;
			active.search_error = null;
			if (request !== undefined) _reportBrowserSearchResult(request);
			return;
		}
		active.searching = true;
		active.search_error = null;
		const startedAt = performance.now();
		try {
			const results = await searchCollection({ q: trimmed, limit: MAX_SEARCH_ROWS });
			if (!active.whole_collection || active.search.trim() !== trimmed) return;
			active.search_results = results.items.map((hit, index) => _rowFromSearchHit(hit, index + 1));
			active.search_total = results.total;
			active.search_result_query = trimmed;
			// One ring row per query that actually published results; a
			// superseded query returned above and is not a completed search.
			recordCollectionSearchTiming({
				queryMs: performance.now() - startedAt,
				hits: active.search_results.length,
				total: results.total
			});
		} catch (exc) {
			if (active.whole_collection && active.search.trim() === trimmed) {
				active.search_results = [];
				active.search_total = 0;
				active.search_error = String(exc);
				pushToast(`search failed: ${String(exc)}`, 'error');
			}
		} finally {
			if (active.search.trim() === trimmed) {
				active.searching = false;
				if (request !== undefined && active === pane) _reportBrowserSearchResult(request);
			}
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
		if (id === null || id === 'all' || isMissingTracksId(id) || isAutolistId(id)) return;
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
		void _removeMembership(row);
	}

	async function _removeMembership(row: BrowserRow): Promise<void> {
		const p = pane;
		const id = p.playlist_id;
		if (id === null || id === 'all' || isMissingTracksId(id) || isAutolistId(id)) return;
		if (source !== 'collection' || p.whole_collection) {
			pushToast('membership editing is disabled outside the complete playlist view', 'error');
			return;
		}
		if (!row.item_id) {
			pushToast('playlist row has no membership id - reload and try again', 'error');
			return;
		}
		try {
			await deletePlaylistItem(id, row.item_id);
		} catch (exc) {
			pushToast(`playlist update failed: ${String(exc)}`, 'error');
			return;
		}
		// FLOW-06: every other membership mutation in this file confirms with a
		// toast; this one silently succeeded, indistinguishable from a no-op.
		pushToast(`Removed "${row.title ?? row.stable_id}" from playlist`, 'info');
		const node = _currentNode(p);
		if (node !== null) await _loadPane(p, node);
	}

	// FLOW-07: relocate rewrites the track's own path, not a playlist
	// membership, so a plain pane reload (same call every membership
	// mutation above already makes) is enough to pick up the fresh
	// file_exists / mostly_broken state - no separate refetch needed.
	async function _reloadActivePane(): Promise<void> {
		const p = pane;
		const node = _currentNode(p);
		if (node !== null) await _loadPane(p, node);
	}

	let addToPlaylistIds = $state<string[] | null>(null);

	function openAddToPlaylistPicker(ids: string[]): void {
		addToPlaylistIds = ids;
	}

	async function addTracksToPlaylist(node: PlaylistNode): Promise<void> {
		const ids = addToPlaylistIds;
		if (ids === null || ids.length === 0) return;
		addToPlaylistIds = null;
		try {
			await appendTracksToPlaylist(node.playlist_id, ids);
			await _refreshPlaylists();
			pushToast(addToPlaylistToastMessage(ids.length, node.name), 'info');
		} catch (exc) {
			pushToast(`add to playlist failed: ${String(exc)}`, 'error');
		}
	}

	async function removeFromLibraryUi(stableIds: string[]): Promise<void> {
		const ids = [...new Set(stableIds)];
		if (ids.length === 0) return;
		if (!window.confirm(removeFromLibraryConfirmMessage(ids.length))) return;
		let okCount = 0;
		for (const stableId of ids) {
			try {
				await removeFromLibrary(stableId);
				okCount += 1;
			} catch (exc) {
				pushToast(`remove from library failed: ${String(exc)}`, 'error');
				return;
			}
		}
		pushToast(removeFromLibraryToastMessage(okCount), 'info');
	}

	function reorderRows(fromOrder: number, toOrder: number, count = 1): void {
		void _moveMembershipSlice(fromOrder, toOrder, count);
	}

	async function _moveMembershipSlice(
		fromOrder: number,
		toOrder: number,
		count: number
	): Promise<void> {
		const p = pane;
		const id = p.playlist_id;
		if (id === null || id === 'all' || isMissingTracksId(id) || isAutolistId(id)) return;
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
		const rowsByOrder = new Map(p.rows.map((row) => [row.order, row]));
		const slice: BrowserRow[] = [];
		for (let order = fromOrder; order < fromOrder + count; order += 1) {
			const row = rowsByOrder.get(order);
			if (row === undefined) {
				pushToast('playlist row has no membership id - reload and try again', 'error');
				return;
			}
			slice.push(row);
		}
		const target = rowsByOrder.get(toOrder);
		if (target === undefined) {
			pushToast('playlist row has no membership id - reload and try again', 'error');
			return;
		}
		for (const row of [...slice, target]) {
			if (!row.item_id) {
				pushToast('playlist row has no membership id - reload and try again', 'error');
				return;
			}
		}
		if (toOrder >= fromOrder && toOrder < fromOrder + count) {
			return;
		}
		const body: {
			range_start: string;
			range_length: number;
			range_end: string;
			before_item_id?: string;
			after_item_id?: string;
		} = {
			range_start: slice[0].item_id!,
			range_length: count,
			range_end: slice[count - 1].item_id!
		};
		if (toOrder > fromOrder + count - 1) {
			body.after_item_id = target.item_id!;
		} else if (toOrder < fromOrder) {
			body.before_item_id = target.item_id!;
		} else {
			return;
		}
		try {
			await movePlaylistItems(id, p.etag, body);
			// FLOW-06: silent on success today, so a drag-reorder looked identical
			// to a dropped/ignored gesture until the row visibly re-sorted.
			pushToast(count === 1 ? 'Moved track' : `Moved ${count} tracks`, 'info');
		} catch (exc) {
			if (exc instanceof PlaylistConflictError) {
				pushToast('playlist changed elsewhere - reloaded with the latest version', 'error');
			} else {
				pushToast(`playlist update failed: ${String(exc)}`, 'error');
				return;
			}
		}
		const node = _currentNode(p);
		if (node !== null) await _loadPane(p, node);
	}
</script>

<section
	class="rb-browser"
	data-library-root
	data-testid="browser-panel"
	style:--playlist-tree-width={`${uiPrefs.playlist_tree_width}px`}
>
	<PerformanceRecorderRail {source} onspotify={selectSpotifySource} />
	<div class="tree-panel" data-testid="playlist-tree">
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
			<LibraryNav
				nodes={treeNodes}
				playlistsLoading={playlistsLoading}
				playlistsError={playlistsError}
				hiddenBrokenPlaylistCount={hiddenBrokenPlaylistCount}
				allTracksCount={allTracksNonBrokenCount}
				allTracksBrokenCount={allTracksBrokenCount}
				allTracksError={allTracksReconcileError}
				selectedId={pane.playlist_id}
				trackSelectedId={pane.selected_id}
				{deckLoadedPlaylistIds}
				multiPanePlaylistIds={multiPanePlaylistIds_}
				onselect={selectPlaylist}
				onselectsmartlist={selectSmartlist}
				onautolistchange={(sel, title) => loadAutolistUi(sel, title)}
				onselecttrack={selectRow}
				onloadtrack={loadRow}
				oncreateplaylist={() => createPlaylistUi()}
				onrenameplaylist={(n, name) => void renamePlaylistUi(n, name)}
				onforbidduplicates={(n) => void toggleForbidDuplicates(n)}
				ondeleteplaylist={(n) => void deletePlaylistUi(n)}
				onduplicateplaylist={(n) => void duplicatePlaylistUi(n)}
				ondroptracks={(id, ids) => void dropTracksOnPlaylist(id, ids)}
				onfolderdrop={(e) => void folderDropOnPlaylistTree(e)}
			/>
		{/if}
	</div>
	<div
		class="playlist-tree-resize"
		role="separator"
		aria-orientation="vertical"
		aria-label="Resize playlist tree"
		onpointerdown={_startPlaylistTreeResize}
		onpointermove={_resizePlaylistTree}
		onpointerup={_finishPlaylistTreeResize}
		onpointercancel={_finishPlaylistTreeResize}
	></div>
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
				onaddpane={addBlankPaneSlot}
			/>
			<div class="header-right">
				<div class="header-controls-cluster">
				<button
					class="rb-lit-button rb-inert master-dd"
					disabled
					title={plannedTitle('master-dropdown')}
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
					title={plannedTitle('single-column-layout')}
					aria-label="single column layout"
				>
					<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
						<path d="M2 2h12v12H2z" fill="none" stroke="currentColor" stroke-width="1.4" />
					</svg>
				</button>
				<button
					class="icon-btn rb-inert"
					disabled
					title={plannedTitle('split-column-layout')}
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
				<label class="hide-broken" title={formatHideBrokenCheckboxTooltip()}>
					<input
						type="checkbox"
						aria-label="Show broken links"
						checked={!uiPrefs.hide_broken_links}
						onchange={(e) => setHideBrokenLinks(!e.currentTarget.checked)}
					/>
					<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
						<path
							d="M6 5 4.5 3.5a2.1 2.1 0 0 0-3 3L3 8m7-1 1.5 1.5a2.1 2.1 0 0 0 3-3L13 4m-8.5 7.5L11.5 4.5M6.5 9.5l3-3"
							fill="none"
							stroke="currentColor"
							stroke-width="1.4"
							stroke-linecap="round"
						/>
					</svg>
					<span>Broken</span>
				</label>
				<label
					class="remixes-filter"
					title="Keep only remixes: title version markers (remix / bootleg / rework / VIP / non-radio edit). The lyric repair signal joins this once the full-library alignment run lands"
				>
					<input
						type="checkbox"
						checked={uiPrefs.remixes_filter}
						onchange={(e) => setRemixesFilter(e.currentTarget.checked)}
					/>
					<span>Remixes</span>
				</label>
				<label
					class="vocals-filter"
					title="Keep only tracks with real word-level lyrics spanning more than 5 lines; tracks not yet run through the lyric pipeline are excluded"
				>
					<input
						type="checkbox"
						checked={uiPrefs.vocals_filter}
						onchange={(e) => setVocalsFilter(e.currentTarget.checked)}
					/>
					<span>Vocals</span>
				</label>
				<!--
					pin 5e3ed689ad3a: this control used to live in `.search-options`,
					which only renders while the search box is focused or non-empty, so
					it vanished the moment you clicked away and the feature looked
					deleted. It belongs in the header row next to Broken, where it is
					always reachable. The persisted pref id stays `next_only_filter`:
					renaming the storage key would silently drop every stored
					preference (prefs.svelte.ts validates that exact key). Only the
					user-facing label changes, to the one the maintainer asked for.
				-->
				<label class="next-only" title="Show only tracks compatible with the reference deck (master, else playing, else any loaded with key and BPM): Camelot key family (including half/double BPM folds) and inside the BPM window. Shortcut: Tab">
					<input
						type="checkbox"
						aria-label="Show only tracks compatible with the reference deck (master, else playing, else any loaded with key and BPM)"
						checked={uiPrefs.next_only_filter}
						onchange={(e) => setNextOnlyFilter(e.currentTarget.checked)}
					/>
					<span>compatible</span>
				</label>
				<label
					class="offline-filter"
					title="Keep only tracks with local audio present (excludes cloud-only and streaming rows)"
				>
					<input
						type="checkbox"
						aria-label="Available offline - keep only tracks with local audio present"
						checked={uiPrefs.available_offline_filter}
						onchange={(e) => setAvailableOfflineFilter(e.currentTarget.checked)}
					/>
					<span>available offline</span>
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
				<div class="search-stack">
					{#if searchFocused || pane.search.trim() !== ''}
						<div class="search-options" aria-label="Search options">
							<label
								class="whole-collection"
								title="Search all playlists uses server FTS across the collection. Unchecked filters only the current pane."
							>
								<input
									type="checkbox"
									checked={pane.whole_collection}
									onchange={(event) => setWholeCollection(event.currentTarget.checked)}
								/>
								<span>Search all playlists</span>
							</label>
						</div>
					{/if}
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
				</div>
				</div>
				<div class="edit-actions-stack">
					<button class="rb-lit-button" disabled={pane.selected_ids.length === 0} onclick={() => void openEditModal('find-replace')}>Find &amp; Replace</button>
					<div class="edit-actions-fold">
						<button class="rb-lit-button" disabled={pane.selected_ids.length === 0} onclick={() => void openEditModal('bulk-edit')}>Bulk Edit</button>
						<button class="rb-lit-button" onclick={() => void openEditModal('mytag')}>MyTags</button>
					</div>
				</div>
			</div>
		</div>
		{#if uiPrefs.auto_play_enabled && autoPlaySnapshotActive && !autoPlaySnapshotMatchesView}
			<div class="autoplay-snapshot-notice" role="status">
				AutoPlay is using its activation order. Toggle it off and on to use this order.
			</div>
		{/if}
		{#snippet libraryLoadOverlay()}
			<LibraryLoadIndicator
				loading={pane.loading}
				progress={pane.load_progress}
				searching={pane.searching}
			/>
		{/snippet}
		{#if pane.kind === 'playlist' && pane.playlist_id !== null}
			<PlaylistSetTabs playlistId={pane.playlist_id} />
		{/if}
		<TrackTable
			bodyOverlay={libraryLoadOverlay}
			{provider}
			selectedIds={pane.selected_ids}
			selectedOrders={pane.selected_orders}
			{loadedIds}
			{vocalsById}
			{markerAnlzById}
			sortKey={pane.sort_key}
			sortDir={pane.sort_dir}
			{emptyMessage}
			onemptyretry={
				wholeCollectionActive && pane.search_error !== null
					? () => void _searchWholeCollection(pane, pane.search)
					: undefined
			}
			{filterBypassNote}
			restoreKey={`${activePane}:${navEpoch}`}
			scrollTop={pane.scroll_top}
			removable={editablePane}
			reorderable={reorderablePane}
			onscrollcursor={(top) => {
				_noteLibraryInteraction();
				panes[activePane].rememberScroll(top);
			}}
			onrenderedrowcapacity={noteRenderedLibraryRowCapacity}
			onsort={sortBy}
			onselectrow={selectRow}
			onloadrow={loadRow}
			onpickdoubledeck={pickDoubleDeck}
			onloadconfirmcancelled={_releaseDeckReservation}
			onpreviewseek={previewSeek}
			onrefused={(reason) => pushToast(reason, 'error')}
			onrate={rateRow}
			onrowvisible={rowVisible}
			onremoverow={removeRow}
			onreorder={reorderRows}
			onstemsdonext={(ids) => void enqueueLibraryJobsBatched({ lane: 'stems', stable_ids: ids }).then(() => libraryJobsStore.refresh())}
			onlyricsdonext={(ids) => void enqueueLibraryJobsBatched({ lane: 'lyrics', stable_ids: ids }).then(() => libraryJobsStore.refresh())}
			onopeneditmodal={(kind) => void openEditModal(kind)}
			onremovefromlibrary={(ids) => void removeFromLibraryUi(ids)}
			onrelocated={() => void _reloadActivePane()}
			onaddtoplaylist={(ids) => openAddToPlaylistPicker(ids)}
			ongenrefilter={genreFilter}
			{genreFilterUntil}
			searchQuery={pane.search}
			findQuery={findHighlightQuery}
			{suggestHoverId}
		/>
		{#if filterFallbackNote !== null}
			<div class="filter-fallback-note" role="status">{filterFallbackNote}</div>
		{/if}
		<LyricSearchResults
			query={pane.search}
			active={wholeCollectionActive}
			primarySettled={!pane.searching}
			onerror={(message) => pushToast(message, 'error')}
		/>
		<!-- LIBUX-02: one chevron collapses/restores both panels together, so
		     TrackTable (flex: 1 in this column) reclaims their vertical space
		     the instant they stop rendering. -->
		<div
			class="library-panels-collapse-bar"
			class:collapsed={isLibraryPanelsCollapsed()}
			data-testid="library-panels-collapse-bar"
		>
			<button
				type="button"
				class="panels-chevron"
				title={isLibraryPanelsCollapsed()
					? 'Show Next / Recommended panels'
					: 'Hide Next / Recommended panels'}
				aria-label={isLibraryPanelsCollapsed()
					? 'Show Next / Recommended panels'
					: 'Hide Next / Recommended panels'}
				aria-pressed={isLibraryPanelsCollapsed()}
				onclick={() => toggleLibraryPanels()}
			>
				{isLibraryPanelsCollapsed() ? '‹' : '›'}
			</button>
		</div>
		{#if !isLibraryPanelsCollapsed()}
			<!-- dj_copilot suggest-next strip: keyed to the deck-1-loaded track. -->
			<SuggestNextStrip
				stableId={decks[1].stable_id}
				targetLabel={suggestTargetDeck === null ? null : `CH ${suggestTargetDeck}`}
				playTargetLabel={suggestPlayTargetDeck === null ? null : `CH ${suggestPlayTargetDeck}`}
				onload={(sid) => loadSuggest(sid)}
				onplay={(sid, pressT0Ms) => loadSuggest(sid, { play: true, pressT0Ms })}
				onhover={(sid) => (suggestHoverId = sid)}
				oncandidates={(cands) => (suggestCandidates = cands)}
			/>
			<RecommendedSection
				candidates={suggestCandidates}
				currentPlaylistId={pane.playlist_id}
				currentPlaylistMemberIds={playlistMemberIds}
				referenceBpm={masterRef?.bpm ?? null}
				referenceKey={masterRef?.key ?? null}
				stableId={decks[1].stable_id}
				onload={(sid) => loadSuggest(sid)}
				onplay={(sid, pressT0Ms) => loadSuggest(sid, { play: true, pressT0Ms })}
				onhover={(sid) => (suggestHoverId = sid)}
			/>
		{/if}
	</div>
	<div class="bottom-bar">
		<button
			class="icon-btn rb-inert"
			disabled
			title={plannedTitle('export-eject')}
			aria-label="export/eject"
		>
			<svg viewBox="0 0 16 16" width="11" height="11" aria-hidden="true">
				<path d="M8 3l5 6H3zM3 11h10v2H3z" fill="currentColor" />
			</svg>
		</button>
		<!-- This is our own app, not the vendor whose library format it reads
		     (pin 571f4281ecea, the maintainer, Wed 2 Sep 2026). -->
		<span class="wordmark">open dj</span>
		<LibraryJobsChrome />
		<div class="library-health" aria-label="library processing health">
			{#each [frontendOnline, backendOnline, libraryHealth, vocalsCompletion, stemsCompletion, lyricsCompletion] as dot (dot.label)}
				<button
					type="button"
					class:complete={dot.state === 'complete'}
					class:incomplete={dot.state === 'incomplete'}
					class:unavailable={dot.state === 'unavailable'}
					class:error={dot.state === 'error'}
					class="health-dot"
					aria-label={`${dot.label}: ${dot.detail}`}
				>
					<span aria-hidden="true"></span>
				</button>
			{/each}
			<div class="health-popover" role="tooltip" use:viewportFloatingPopover={{ preferred: 'above', gap: 4 }}>
				{#each [frontendOnline, backendOnline, libraryHealth, vocalsCompletion, stemsCompletion, lyricsCompletion] as dot (dot.label)}
					<p><strong>{dot.label}</strong><br />{dot.detail}</p>
				{/each}
			</div>
		</div>
		<!-- The build identity lives at the RIGHT end of this tray on
		     /performance. It used to be position:fixed bottom-left, sitting on
		     top of the connectivity dots. The root layout mounts it in the app
		     shell's own tray instead, and that branch never renders for this
		     route, so exactly one is ever on screen. -->
		<BuildIdentity />
		<span class="grip" aria-hidden="true">
			<svg viewBox="0 0 12 12" width="10" height="10">
				<path d="M11 1L1 11M11 5L5 11M11 9L9 11" stroke="currentColor" stroke-width="1" />
			</svg>
		</span>
	</div>
</section>

<TrackEditModals
	{openModal}
	stableIds={pane.selected_ids}
	etags={modalEtags}
	rows={pane.rows}
	onclose={() => (openModal = null)}
	onapplied={onEditApplied}
/>

<AddToPlaylistPicker
	open={addToPlaylistIds !== null}
	playlists={treeNodes}
	onpick={(node) => void addTracksToPlaylist(node)}
	onclose={() => (addToPlaylistIds = null)}
/>

{#if folderDupPrompt !== null}
	<PlaylistFolderDupModal
		rows={folderDupPrompt.rows}
		ondone={(decisions) => {
			folderDupPrompt?.resolve(decisions);
			folderDupPrompt = null;
		}}
		oncancel={() => {
			folderDupPrompt?.resolve(null);
			folderDupPrompt = null;
			pushToast('Folder drop held duplicates unresolved; playlist may be partial', 'error');
		}}
	/>
{/if}

<style>
	.rb-browser {
		grid-area: browser;
		position: relative;
		display: grid;
		grid-template-areas:
			'rail tree divider list'
			'bottom bottom bottom bottom';
		grid-template-columns: 30px var(--playlist-tree-width) 6px minmax(0, 1fr);
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
	}
	.playlist-tree-resize {
		grid-area: divider;
		cursor: col-resize;
		background: var(--rb-border);
		touch-action: none;
	}
	.playlist-tree-resize:hover,
	.playlist-tree-resize:active { background: var(--rb-accent); }
	/* `overflow: hidden` is load-bearing, not tidiness. TrackTable's LIBUX-01
	 * `.tt-root` min-height is an ABSOLUTE floor, so on a window too short to
	 * honor it the table stays at its floor height and, with the default
	 * `overflow: visible`, simply paints outside this panel: measured at the
	 * repo's standard 1280x800 /performance viewport, `.tt-root` ran to
	 * y=869 in an 800px window (69px off-screen), `.perf-root` reported
	 * scrollHeight 895 against clientHeight 800, and the rows painted over
	 * the browser's 18px bottom bar. Clipping here keeps the panel's own
	 * chrome intact and confines the shortfall to "fewer rows visible",
	 * which is the documented degradation (PR #1007 discussion
	 * r3921443899). Safe for the panel's popovers: `.unload-offer` is
	 * absolutely positioned INSIDE this box, and TrackTable's
	 * `.load-confirm` is `position: fixed`, which this rule cannot clip
	 * because `.list-panel` establishes no containing block for it (no
	 * transform/filter/contain here, only `position: relative`). */
	.list-panel {
		grid-area: list;
		display: flex;
		flex-direction: column;
		min-height: 0;
		min-width: 0;
		overflow: hidden;
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
	/* min-height, not height: the control row below is allowed to wrap onto a
	   second line when it cannot fit, and this header grows with it. Measured
	   at 1280x800: `.list-panel` is 944px there, and an editable playlist's
	   header-right is 808px + the 190px AddTrackSearch = 998px, so something
	   HAD to give. A fixed height gave `.list-panel`'s `overflow: hidden` the
	   rightmost controls (Bulk Edit, MyTags) silently; growing instead costs
	   one row of library, which is this panel's documented degradation.
	   `library-min-5-rows.test.mjs` reads this number as the header's floor. */
	.pane-header {
		flex: none;
		display: flex;
		align-items: stretch;
		justify-content: space-between;
		min-height: 24px;
		border-bottom: 1px solid var(--rb-border);
		background: var(--rb-panel);
		min-width: 0;
	}
	.header-right {
		display: flex;
		align-items: flex-start;
		gap: 4px;
		padding: 0 6px;
		flex: 0 1 auto;
		min-width: 0;
	}
	.header-controls-cluster {
		display: flex;
		flex-wrap: nowrap;
		align-items: center;
		gap: 4px;
		flex: 1 1 auto;
		min-width: 0;
	}
	.edit-actions-stack {
		display: flex;
		flex-direction: column;
		align-items: flex-end;
		gap: 3px;
		flex: none;
	}
	.edit-actions-fold {
		display: flex;
		flex-direction: column;
		align-items: flex-end;
		gap: 3px;
	}
	.autoplay-snapshot-notice {
		flex: none;
		padding: 3px 8px;
		border-bottom: 1px solid color-mix(in srgb, var(--rb-orange) 65%, var(--rb-border));
		background: color-mix(in srgb, var(--rb-orange) 12%, var(--rb-panel));
		color: var(--rb-orange);
		font-size: var(--rb-fs-label);
		line-height: 16px;
		text-align: center;
	}
	.filter-fallback-note {
		flex: none;
		padding: 3px 8px;
		border-top: 1px solid color-mix(in srgb, var(--rb-accent) 55%, var(--rb-border));
		background: color-mix(in srgb, var(--rb-accent) 10%, var(--rb-panel));
		color: var(--rb-text-dim);
		font-size: var(--rb-fs-label);
		line-height: 16px;
		text-align: center;
	}
	.search-stack {
		display: flex;
		flex-direction: column;
		align-items: flex-end;
		gap: 3px;
		flex: 1 1 auto;
		min-width: 0;
	}
	.search-options {
		display: flex;
		align-items: center;
		gap: 8px;
		font-size: var(--rb-fs-label);
		white-space: nowrap;
	}
	.master-dd {
		font-size: var(--rb-fs-label);
	}
	.hide-broken,
	.next-only,
	.remixes-filter,
	.vocals-filter,
	.offline-filter {
		display: inline-flex;
		align-items: center;
		gap: 3px;
		color: var(--rb-text-dim);
		font-size: var(--rb-fs-label);
		white-space: nowrap;
		cursor: pointer;
	}
	.hide-broken:hover,
	.next-only:hover,
	.remixes-filter:hover,
	.vocals-filter:hover,
	.offline-filter:hover {
		color: var(--rb-text);
	}
	.hide-broken input,
	.next-only input,
	.remixes-filter input,
	.vocals-filter input,
	.offline-filter input {
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
	/* PERF-UI-01: at 1280x720 leftover is ~107px after the compact
	 * wavestack. A wrapping pane-header (~45px) would steal the one
	 * visible track row. Keep the 24px floor; overflow clips inert
	 * header-right buttons rather than wrapping. */
	@media (max-height: 799px) {
		.pane-header {
			max-height: 24px;
			overflow: hidden;
		}
		.header-controls-cluster {
			flex-wrap: nowrap;
		}
	}
	.library-panels-collapse-bar {
		flex: none;
		display: flex;
		align-items: center;
		justify-content: flex-start;
		min-height: 12px;
		background: var(--rb-panel);
	}
	.library-panels-collapse-bar.collapsed {
		justify-content: flex-end;
	}
	.panels-chevron {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 16px;
		height: 12px;
		padding: 0;
		background: transparent;
		border: none;
		color: var(--rb-text-dim);
		font-size: 10px;
		line-height: 1;
		cursor: pointer;
	}
	.panels-chevron:hover {
		color: var(--rb-accent);
	}
	.bottom-bar {
		grid-area: bottom;
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 0 6px;
		/* Issue #3097: this row and PerformanceAppNav (position: fixed,
		 * bottom-left, height 18px) are both anchored to the exact same
		 * bottom-left rectangle - this row via normal grid flow, the nav via
		 * `position: fixed` on top of it (z-index 50). Left-padding by the
		 * nav's reserved width keeps this row's own content (the wordmark
		 * first of all) from rendering underneath it, instead of merely
		 * being covered by a higher stacking context. */
		padding-left: var(--rb-perf-nav-w);
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
	.library-health {
		position: relative;
		display: flex;
		align-items: center;
		gap: 3px;
		flex-shrink: 0;
		z-index: 6;
	}
	.health-dot {
		position: relative;
		width: 12px;
		height: 12px;
		padding: 0;
		border: none;
		background: transparent;
		cursor: help;
	}
	.health-dot > span:first-child {
		display: block;
		width: 7px;
		height: 7px;
		border-radius: 50%;
		background: #3a4048;
		box-shadow: inset 0 0 0 1px #23282f;
	}
	.health-dot.complete > span:first-child {
		background: var(--rb-green, #35c04f);
		box-shadow: 0 0 4px color-mix(in srgb, var(--rb-green, #35c04f) 70%, transparent);
	}
	.health-dot.incomplete > span:first-child { background: var(--rb-orange); }
	.health-dot.unavailable > span:first-child { background: var(--rb-text-dim); }
	.health-dot.error > span:first-child { background: var(--rb-red, #d9534f); }
	.health-popover {
		display: none;
		position: fixed;
		min-width: 180px;
		max-width: 320px;
		padding: 6px 8px;
		border: 1px solid var(--rb-border);
		background: var(--rb-panel-raised);
		color: var(--rb-text);
		font: inherit;
		font-size: var(--rb-fs-label);
		text-align: left;
		white-space: normal;
		box-shadow: 0 3px 10px rgb(0 0 0 / 40%);
	}
	.health-popover p {
		margin: 0 0 4px;
	}
	.health-popover p:last-child {
		margin-bottom: 0;
	}
	.library-health:hover .health-popover,
	.library-health:focus-within .health-popover { display: block; }
	.wordmark {
		color: var(--rb-text-dim);
		font-size: var(--rb-fs-label);
		font-weight: 600;
		letter-spacing: 0.5px;
	}
	.grip {
		/* No auto margin: the build identity that now precedes it already
		   carries one, and TWO auto margins split the free space between them
		   instead of pushing the pair to the right. The chip absorbs the gap;
		   the grip stays welded to its right, in the corner. */
		color: var(--rb-text-dim);
	}
</style>
