<script module lang="ts">
	// Row + sort vocabulary now lives in the pane contract (browser-surface
	// unit); re-exported here so existing importers keep working.
	export type { BrowserRow, SortKey } from './pane-contract.svelte';
</script>

<script lang="ts">
	// Browser track table (SCREENSHOT-SPEC 5c). Columns in screenshot order:
	// funnel | cloud | # | Preview | Artwork | Track Title | Artist | K | B |
	// Rating | Comments | Time | Venue (quality badge) | Genre | Stems.
	// Preview strips + file_exists arrive
	// INLINE (contract 1/4); the IntersectionObserver now only reveals rows
	// (one-time canvas draw) and triggers the lazy rb-meta fetch (artwork).
	// Row states: yellow title+artist = loaded on a non-master deck; gold =
	// master; faint green wash = Camelot-compatible suggested next; slightly
	// stronger light-green = Spotify unmatched/pending placeholder; blue =
	// selected; grayed row = audio file missing on disk (FR-1).
	// Rows arrive via the RowProvider contract (pane-contract.svelte.ts).
	// The provider materializes the full result set (parent no longer caps
	// fetches at 500 rows - track-list-virtualization lane); THIS component
	// DOM-virtualizes the render: only the scrolled window (+overscan) is
	// ever mounted, so a multi-thousand-row pane stays cheap regardless of
	// provider.total.
	import { untrack } from 'svelte';
	import { artworkUrl, artworkStatusLabel, type Vocals } from '$lib/rb/api-rb';
	import { analysisIssuesFor } from '$lib/rb/analysis-issues';
	import { camelotKeyColor, camelotKeyHoverLabel } from '$lib/rb/camelot-color';
	import { columnHeaderTitle, type LibraryColTipId } from '$lib/rb/column-tips';
	import { bpmHeatColor, bpmHeatLabel, classifyBpmHeat } from '$lib/rb/bpm-heat';
	import { genreHoverColor } from '$lib/rb/genre-color';
	import { highlightSpans, rowMatchesFind } from '$lib/rb/find-highlight';
	import { camelotKeysAreCompatible, DECK_IDS, deckStates } from '$lib/rb/audio-engine.svelte';
	import { autoPlayOrder } from '$lib/rb/auto-play.svelte';
	import { buildCurveSegments, segmentPath } from '$lib/rb/autoplay-curve';
	import { describeAutoPlayMode } from '$lib/rb/autoplay-mode';
	import { deckHoverUi } from '$lib/rb/deck-hover.svelte';
	import { setConfirmPref, uiPrefs } from '$lib/rb/prefs.svelte';
	import { quickDrawUi } from '$lib/rb/quick-draw-ui.svelte';
	import { installTrackDragGhost, removeTrackDragGhost } from '$lib/rb/drag-ghost';
	import { beginTrackDrag, endTrackDrag, TRACK_STABLE_MIME } from '$lib/rb/track-drag.svelte';
	import type { DeckId } from '$lib/rb/types';
	import type { BrowserRow, RowProvider, SortDir, SortKey } from './pane-contract.svelte';
	import AutoPlayExplainer from './AutoPlayExplainer.svelte';
	import AutoPlayWalkthrough from './AutoPlayWalkthrough.svelte';
	import PreviewStrip from './PreviewStrip.svelte';
	import QualityBadge from '../QualityBadge.svelte';
	import RatingStars from './RatingStars.svelte';
	import AnalysisDots from './AnalysisDots.svelte';
	import StemTags from './StemTags.svelte';
	import { computeVirtualWindow } from './virtual-window';
	import {
		ANALYSIS_COLORS,
		jobProgress,
		type AnalysisBadge,
		type AnalysisIssues
	} from '$lib/rb/job-progress.svelte';
	import { audioPrefetchStatus } from '$lib/rb/audio-prefetch-cache.svelte';

	const DECKS: DeckId[] = [1, 2, 3, 4];
	// Fixed row heights (virtualization window math requires constant height).
	// compact = current tight rows; cosy = taller + slightly roomier cell pad.
	const ROW_HEIGHT_COMPACT = 22;
	const ROW_HEIGHT_COSY = 30;
	const OVERSCAN = 10;

	type ColId =
		| 'funnel'
		| 'err'
		| 'cloud'
		| 'order'
		| 'preview'
		| 'art'
		| 'title'
		| 'artist'
		| 'key'
		| 'bpm'
		| 'plays'
		| 'rating'
		| 'comments'
		| 'time'
		| 'quality'
		| 'genre'
		| 'stems'
		| 'autoplay';

	// ----- AUTOPLAY-COL -----------------------------------------------------
	const AUTOPLAY_ARROW = '\u2193'; // down; flip to \u2191 without re-plumbing
	const AUTOPLAY_COL_COUNT = 18;
	const AUTOPLAY_THEAD_H = 20;

	const COL_DEFAULTS: Record<ColId, number> = {
		funnel: 24,
		err: 24,
		cloud: 24,
		order: 34,
		preview: 177,
		art: 54,
		title: 220,
		artist: 140,
		key: 48,
		bpm: 54,
		plays: 36,
		rating: 80,
		comments: 110,
		time: 48,
		quality: 92,
		genre: 90,
		stems: 148,
		autoplay: 46
	};

	let colWidths = $state<Record<ColId, number>>({ ...COL_DEFAULTS });
	let resizeCol: ColId | null = null;
	let resizeStartX = 0;
	let resizeStartW = 0;
	let loadConfirm = $state<{
		row: BrowserRow;
		deck: DeckId;
		x: number;
		y: number;
	} | null>(null);
	let loadConfirmEveryTime = $state(false);

	function onColResizeStart(event: PointerEvent, col: ColId): void {
		event.preventDefault();
		event.stopPropagation();
		const handle = event.currentTarget as HTMLElement;
		handle.setPointerCapture(event.pointerId);
		resizeCol = col;
		resizeStartX = event.clientX;
		resizeStartW = colWidths[col];
	}

	function onColResizeMove(event: PointerEvent): void {
		if (resizeCol === null) return;
		const next = Math.max(28, resizeStartW + (event.clientX - resizeStartX));
		colWidths = { ...colWidths, [resizeCol]: next };
	}

	function onColResizeEnd(event: PointerEvent): void {
		if (resizeCol === null) return;
		const handle = event.currentTarget as HTMLElement;
		if (handle.hasPointerCapture(event.pointerId)) handle.releasePointerCapture(event.pointerId);
		resizeCol = null;
	}

	function keyCellStyle(key: string | null): string | undefined {
		const color = camelotKeyColor(key);
		return color === null ? undefined : `color:${color}`;
	}

	const masterDeck = $derived(DECK_IDS.map((d) => deckStates[d]).find((d) => d.is_master) ?? null);
	const masterKey = $derived(masterDeck?.key ?? null);
	const masterKeyColor = $derived(camelotKeyColor(masterKey));
	const masterBpm = $derived(masterDeck?.bpm ?? null);
	/** Header BPM color: heat vs itself = on-tempo white when a master exists. */
	const masterBpmColor = $derived(bpmHeatColor(masterBpm, masterBpm));
	const masterStableId = $derived(masterDeck?.stable_id ?? null);
	const hoverStableId = $derived(
		deckHoverUi.deckId === null ? null : (deckStates[deckHoverUi.deckId].stable_id ?? null)
	);

	function keyCompat(key: string | null): boolean {
		return camelotKeysAreCompatible(key, masterKey);
	}

	function keyCompatStyle(key: string | null): string | undefined {
		const base = keyCellStyle(key);
		if (!keyCompat(key) || masterKeyColor === null) return base;
		const border = `box-shadow: inset 0 0 0 1px ${masterKeyColor}`;
		return base === undefined ? border : `${base};${border}`;
	}

	function bpmCellHeat(bpm: number | null) {
		return classifyBpmHeat(bpm, masterBpm);
	}

	function bpmCellStyle(bpm: number | null): string | undefined {
		const heat = bpmCellHeat(bpm);
		return heat === null ? undefined : `color:${heat.color}`;
	}

	/** Red now-line on library preview when this track is on a deck. Prefer
	 * a playing deck when the same stable_id is loaded on more than one. */
	function _nowRatioFor(stableId: string): number | null {
		let fallback: number | null = null;
		for (const d of DECK_IDS) {
			const st = deckStates[d];
			if (st.stable_id !== stableId || st.duration_ms === null || st.duration_ms <= 0) continue;
			const ratio = st.position_ms / st.duration_ms;
			if (st.playing) return ratio;
			if (fallback === null) fallback = ratio;
		}
		return fallback;
	}

	function _badgeFor(row: BrowserRow): AnalysisBadge {
		const fromStore = jobProgress.badges[row.stable_id] ?? {};
		const vocals = vocalsById[row.stable_id];
		const vocalsDone =
			fromStore.vocals === true ||
			(vocals !== undefined && vocals !== null && vocals.status !== 'not_analyzed');
		return {
			...fromStore,
			vocals: vocalsDone,
			beatgrid: fromStore.beatgrid ?? row.rb_meta?.analysis_available === true,
			key: fromStore.key ?? (row.key !== null && row.key !== undefined && String(row.key) !== ''),
			waveform: fromStore.waveform ?? row.rb_meta?.analysis_available === true
		};
	}

	/** Err column: reads the backend-cached beatgrid diagnostic off rb_meta
	 * (see apps/webui/server/rb_vendor.cached_beatgrid_issue) - never parses
	 * the full ANLZ beat-grid here, since this runs per visible row. Other
	 * analysis kinds have no working detector yet, so they stay empty/off
	 * rather than fabricate a state (house rule: no mocked data). The rule
	 * itself lives in $lib/rb/analysis-issues so it can be unit-tested. */
	function _issuesFor(row: BrowserRow): AnalysisIssues {
		return analysisIssuesFor(row);
	}

	function _jobRowStyle(stableId: string): string | undefined {
		const job = jobProgress.activeFor(stableId);
		if (job === null) return undefined;
		const pct = Math.max(0.02, Math.min(1, job.progress)) * 100;
		return `--job-color:${ANALYSIS_COLORS[job.kind]}; --job-pct:${pct}%`;
	}

	let {
		provider,
		selectedIds,
		loadedIds,
		vocalsById,
		sortKey,
		sortDir,
		emptyMessage,
		restoreKey,
		scrollTop,
		removable = false,
		reorderable = false,
		onscrollcursor,
		onsort,
		onselectrow,
		onloadrow,
		onpickdoubledeck,
		onpreviewseek,
		onrate,
		onrowvisible,
		onremoverow,
		onreorder,
		ongenrefilter,
		genreFilterUntil = 0,
		searchQuery = '',
		findQuery = '',
		/** Suggest-next hover: temporarily highlight + scroll to this row. */
		suggestHoverId = null as string | null
	}: {
		/** Read contract: { rows, total, truncated, fetchWindow } - see
		 * pane-contract.svelte.ts. */
		provider: RowProvider;
		selectedIds: string[];
		loadedIds: Set<string>;
		/** Vocals ALREADY known client-side (loaded decks / anlz cache) -
		 * v1 scope: strips never fetch /anlz themselves (see BrowserPanel). */
		vocalsById: Record<string, Vocals>;
		sortKey: SortKey | null;
		sortDir: SortDir;
		emptyMessage: string | null;
		/** Identity of the pane being rendered (e.g. pane index) - the
		 * scroll cursor restores when this changes, NOT on row updates. */
		restoreKey: string | number;
		/** Pane's persisted scroll cursor (PaneStore.scroll_top). */
		scrollTop: number;
		/** add-remove-reorder-tracks: true when the pane is a real playlist
		 * (not All Tracks / blank) - shows the per-row remove control. */
		removable?: boolean;
		/** True when removable AND the pane is in its natural membership
		 * order (no client sort/search) - drag handles only render then,
		 * since row.order would otherwise not match the visual position. */
		reorderable?: boolean;
		/** Reports the live table-wrap scrollTop back to the pane store. */
		onscrollcursor: (top: number) => void;
		onsort: (key: SortKey) => void;
		onselectrow: (row: BrowserRow, event: MouseEvent) => void;
		/** deck null = legacy free-deck load; prefer onpickdoubledeck for dblclick. */
		onloadrow: (row: BrowserRow, deck: DeckId | null, opts?: { play?: boolean }) => void;
		/**
		 * Preferred deck for double-click load+play. Shift -> CH3/CH4 when
		 * free/stopped. Cmd/Ctrl -> replace whatever deck the last plain
		 * double-click targeted, instead of advancing to the next deck.
		 */
		onpickdoubledeck?: (
			row: BrowserRow,
			opts?: { shift?: boolean; replace?: boolean }
		) => DeckId | null;
		/** Preview strip click: 0..1 ratio along the track. */
		onpreviewseek?: (row: BrowserRow, ratio: number) => void;
		onrate: (row: BrowserRow, next: number) => void;
		onrowvisible: (row: BrowserRow) => void;
		/** Remove this row's membership position from the playlist. */
		onremoverow?: (row: BrowserRow) => void;
		/** Move the track at `fromOrder` (1-based) to `toOrder`'s slot. */
		onreorder?: (fromOrder: number, toOrder: number) => void;
		/** Genre chip / post-filter gestures. */
		ongenrefilter?: (mode: 'strict' | 'loose' | 'clear' | 'undo', tag?: string) => void;
		/** Epoch ms until which library dbl/triple remap to clear/undo. */
		genreFilterUntil?: number;
		/** Live pane search (for same-tag toggle + active chip). */
		searchQuery?: string;
		/** In-place find highlight (≥3 chars); empty = off. */
		findQuery?: string;
		/** Suggest-next hover: temporarily highlight + scroll to this row. */
		suggestHoverId?: string | null;
	} = $props();

	const GENRE_CLICK_MS = 320;
	const LOAD_DBLCLICK_SEL = '.c-preview, .c-art, .c-title, .c-artist';
	let _genreClickTimer: ReturnType<typeof setTimeout> | null = null;
	let _rowGenreTimer: ReturnType<typeof setTimeout> | null = null;

	function genreWindowOpen(): boolean {
		return genreFilterUntil > 0 && Date.now() < genreFilterUntil;
	}

	function splitGenreTags(raw: string): string[] {
		return raw
			.split(',')
			.map((t) => t.trim())
			.filter((t) => t !== '');
	}

	function genreTagStyle(tag: string): string | undefined {
		const color = genreHoverColor(tag);
		return color === null ? undefined : `--genre-glow:${color}`;
	}

	function onGenreTagClick(event: MouseEvent, tag: string): void {
		event.stopPropagation();
		event.preventDefault();
		if (ongenrefilter === undefined) return;
		if (_genreClickTimer !== null) clearTimeout(_genreClickTimer);
		const detail = event.detail;
		if (detail >= 3) {
			ongenrefilter('undo');
			_genreClickTimer = null;
			return;
		}
		if (detail === 2) {
			_genreClickTimer = setTimeout(() => {
				_genreClickTimer = null;
				ongenrefilter('loose', tag);
			}, GENRE_CLICK_MS);
			return;
		}
		_genreClickTimer = setTimeout(() => {
			_genreClickTimer = null;
			ongenrefilter('strict', tag);
		}, GENRE_CLICK_MS);
	}

	function onRowPointer(event: MouseEvent, row: BrowserRow): void {
		onselectrow(row, event);
		if (!genreWindowOpen() || ongenrefilter === undefined || event.detail < 2) return;
		if (_rowGenreTimer !== null) clearTimeout(_rowGenreTimer);
		if (event.detail >= 3) {
			ongenrefilter('undo');
			_rowGenreTimer = null;
			return;
		}
		_rowGenreTimer = setTimeout(() => {
			_rowGenreTimer = null;
			ongenrefilter('clear');
		}, GENRE_CLICK_MS);
	}

	function onRowDblClick(event: MouseEvent, row: BrowserRow): void {
		event.preventDefault();
		event.stopPropagation();
		// Post-filter window: dblclick means "lose filters" (handled via
		// click detail=2 timer) - never open the deck-load confirm.
		if (genreWindowOpen()) return;
		const target = event.target as HTMLElement | null;
		if (target === null || target.closest(LOAD_DBLCLICK_SEL) === null) return;
		const deck =
			onpickdoubledeck?.(row, {
				shift: event.shiftKey,
				replace: event.metaKey || event.ctrlKey
			}) ?? (event.shiftKey ? null : 1);
		if (deck === null) return;
		// Missing key = ask; false = skip (remembered "do this every time").
		if (uiPrefs.confirm.dblclick_load_play === false) {
			onloadrow(row, deck, { play: true });
			return;
		}
		loadConfirmEveryTime = false;
		loadConfirm = {
			row,
			deck,
			x: event.clientX,
			y: Math.max(8, event.clientY - 20)
		};
	}

	function hl(text: string | null): Array<{ text: string; hit: boolean }> {
		return highlightSpans(text ?? '', findQuery);
	}

	const rows = $derived(provider.rows);
	const selectedIdSet = $derived(new Set(selectedIds));
	const rowHeight = $derived(
		uiPrefs.library_density === 'cosy' ? ROW_HEIGHT_COSY : ROW_HEIGHT_COMPACT
	);

	// ----- AUTOPLAY-COL helpers ---------------------------------------------
	let hoveredApId = $state<string | null>(null);
	const autoPlayMode = $derived(describeAutoPlayMode(uiPrefs).mode);

	function _autoPlayRank(stableId: string): number | null {
		return autoPlayOrder.rankOf.get(stableId) ?? null;
	}

	const rowIndexOf = $derived.by(() => {
		const map = new Map<string, number>();
		for (let i = 0; i < rows.length; i++) map.set(rows[i].stable_id, i);
		return map;
	});

	const apCurveSegments = $derived.by(() => {
		if (hoveredApId === null || autoPlayOrder.chain.length < 2) return [];
		// Offset scroll so Y is relative to sticky-thead scrollport.
		return buildCurveSegments({
			chain: autoPlayOrder.chain,
			rankOf: autoPlayOrder.rankOf,
			rowIndexOf,
			rowHeight,
			scrollTop: liveScrollTop - AUTOPLAY_THEAD_H,
			viewportHeight,
			pad: rowHeight * 2
		});
	});

	const apCurveX = $derived(Math.max(8, colWidths.autoplay / 2));

	// ------------------------------------------- per-pane scroll cursor
	// Restore ONLY when the rendered pane changes (restoreKey): reading
	// scrollTop through untrack keeps live scrolling from re-triggering.
	let wrapEl = $state<HTMLDivElement | null>(null);
	// Live scroll position for window math - distinct from the `scrollTop`
	// prop (the pane's PERSISTED cursor, only meaningful at restore time).
	// Starts at 0; the restore $effect below syncs it from the prop before
	// paint (same tick it sets el.scrollTop), so there is no row-0 flash.
	let liveScrollTop = $state(0);
	let viewportHeight = $state(0);

	$effect(() => {
		void restoreKey; // the one tracked dependency
		const el = wrapEl;
		if (el !== null) {
			const restored = untrack(() => scrollTop);
			el.scrollTop = restored;
			liveScrollTop = restored;
		}
	});

	// ------------------------------------------------- DOM row virtualization
	$effect(() => {
		const el = wrapEl;
		if (el === null) return;
		viewportHeight = el.clientHeight;
		if (typeof ResizeObserver === 'undefined') return; // SSR guard
		const ro = new ResizeObserver((entries) => {
			for (const entry of entries) viewportHeight = entry.contentRect.height;
		});
		ro.observe(el);
		return () => ro.disconnect();
	});

	const windowInfo = $derived(
		computeVirtualWindow({
			scrollTop: liveScrollTop,
			viewportHeight,
			rowHeight,
			rowCount: rows.length,
			overscan: OVERSCAN
		})
	);
	const visibleRows = $derived(rows.slice(windowInfo.startIndex, windowInfo.endIndex));

	/** Jump to first in-place find match when the query becomes active. */
	$effect(() => {
		const q = findQuery;
		if (q === '') return;
		const list = untrack(() => rows);
		const rh = untrack(() => rowHeight);
		const vh = untrack(() => viewportHeight);
		const idx = list.findIndex((r) => rowMatchesFind(r, q));
		if (idx < 0) return;
		const el = wrapEl;
		if (el === null) return;
		const top = Math.max(0, idx * rh - Math.floor(vh / 3));
		el.scrollTop = top;
		liveScrollTop = top;
		onscrollcursor(top);
	});

	/** Master track fold cue: above / below viewport (null = on-screen or absent). */
	const masterIndex = $derived(
		masterStableId === null ? -1 : rows.findIndex((r) => r.stable_id === masterStableId)
	);
	const masterFold = $derived.by((): 'above' | 'below' | null => {
		if (masterIndex < 0 || viewportHeight <= 0) return null;
		const top = masterIndex * rowHeight;
		const bottom = top + rowHeight;
		if (bottom <= liveScrollTop + 2) return 'above';
		if (top >= liveScrollTop + viewportHeight - 2) return 'below';
		return null;
	});

	/** Suggest-next hover: jump to row while hovered, restore scroll on leave. */
	let _suggestSavedScroll: number | null = null;
	$effect(() => {
		const sid = suggestHoverId;
		const el = wrapEl;
		if (el === null) return;
		if (sid === null || sid === '') {
			if (_suggestSavedScroll !== null) {
				el.scrollTop = _suggestSavedScroll;
				liveScrollTop = _suggestSavedScroll;
				_suggestSavedScroll = null;
			}
			return;
		}
		const list = untrack(() => rows);
		const idx = list.findIndex((r) => r.stable_id === sid);
		if (idx < 0) return;
		if (_suggestSavedScroll === null) _suggestSavedScroll = el.scrollTop;
		const rh = untrack(() => rowHeight);
		const vh = untrack(() => viewportHeight);
		const target = Math.max(0, idx * rh - Math.floor(vh / 3));
		el.scrollTop = target;
		liveScrollTop = target;
	});

	function jumpToMaster(): void {
		if (wrapEl === null || masterIndex < 0) return;
		const target = Math.max(0, masterIndex * rowHeight - viewportHeight * 0.35);
		wrapEl.scrollTop = target;
		liveScrollTop = target;
		onscrollcursor(target);
	}

	// ------------------------------------------- lazy-hydration observer
	// One-shot per row element: fetch fires the first time a row scrolls into
	// view (ancestor overflow clipping is honoured by IntersectionObserver, so
	// root null is correct for the scrolling table wrap).
	const _rowByEl = new WeakMap<Element, BrowserRow>();
	let _observer: IntersectionObserver | null = null;

	function _ensureObserver(): IntersectionObserver | null {
		if (typeof IntersectionObserver === 'undefined') return null; // SSR guard
		if (_observer === null) {
			_observer = new IntersectionObserver(
				(entries) => {
					for (const entry of entries) {
						if (!entry.isIntersecting) continue;
						const row = _rowByEl.get(entry.target);
						_observer?.unobserve(entry.target);
						if (row !== undefined) onrowvisible(row);
					}
				},
				{ rootMargin: '120px 0px' }
			);
		}
		return _observer;
	}

	function observeRow(node: HTMLElement, row: BrowserRow): { destroy(): void } {
		_rowByEl.set(node, row);
		_ensureObserver()?.observe(node);
		return {
			destroy(): void {
				_observer?.unobserve(node);
			}
		};
	}

	$effect(() => {
		return () => {
			_observer?.disconnect();
		};
	});

	// ----------------------------------------------------- cell formatting
	function _fmtTime(ms: number | null): string {
		if (ms === null) return '';
		const total = Math.round(ms / 1000);
		const m = Math.floor(total / 60);
		const s = total % 60;
		return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
	}

	function _fmtBpm(bpm: number | null): string {
		// Screenshot truncates to '132.' - one decimal is the sanctioned improvement.
		return bpm === null ? '' : bpm.toFixed(1);
	}

	function _hideBrokenImg(event: Event): void {
		(event.currentTarget as HTMLImageElement).style.display = 'none';
	}

	// ----------------------------------------- drag-to-reorder (native DnD)
	// Grip-initiated only (not the whole row): the row's own click/dblclick
	// keep selecting/loading a deck. _dragSourceOrder is plain state, not a
	// rune - it only matters for the lifetime of one drag gesture.
	let _dragSourceOrder: number | null = null;

	function onRowDragStart(event: DragEvent, row: BrowserRow): void {
		if (!row.file_exists || row.is_streaming) {
			event.preventDefault();
			return;
		}
		const ids =
			selectedIds.includes(row.stable_id) && selectedIds.length > 1
				? selectedIds
				: [row.stable_id];
		// The MIME is still set for cross-app interop; drop targets accept on
		// the in-app state because WKWebView hides it during dragover.
		event.dataTransfer?.setData(TRACK_STABLE_MIME, ids.join(','));
		if (event.dataTransfer) event.dataTransfer.effectAllowed = 'copy';
		// Without an explicit drag image WebKit snapshots this <tr> plus every
		// composited layer overlapping it, so the whole browser panel appears
		// to come along for the ride.
		installTrackDragGhost(event, {
			title: row.title ?? row.stable_id,
			artist: row.artist ?? '',
			count: ids.length
		});
		beginTrackDrag(ids);
	}

	function onRowDragEnd(): void {
		removeTrackDragGhost();
		endTrackDrag();
	}

	function onGripDragStart(event: DragEvent, row: BrowserRow): void {
		event.stopPropagation();
		_dragSourceOrder = row.order;
		event.dataTransfer?.setData('text/plain', String(row.order));
		if (event.dataTransfer) event.dataTransfer.effectAllowed = 'move';
	}

	function onRowDragOver(event: DragEvent): void {
		if (_dragSourceOrder === null) return;
		event.preventDefault();
		if (event.dataTransfer) event.dataTransfer.dropEffect = 'move';
	}

	function onRowDrop(event: DragEvent, row: BrowserRow): void {
		event.preventDefault();
		const from = _dragSourceOrder;
		_dragSourceOrder = null;
		if (from === null || from === row.order) return;
		onreorder?.(from, row.order);
	}
</script>

{#snippet sortableTh(key: SortKey, label: string, col: ColId & LibraryColTipId)}
	<!-- svelte-ignore a11y_click_events_have_key_events -->
	<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
	<th
		class={`h-${key} sortable`}
		style={`width:${colWidths[col]}px`}
		onclick={(e) => {
			if ((e.target as HTMLElement).closest('.col-resize')) return;
			onsort(key);
		}}
		title={columnHeaderTitle(col, `Sort by ${label} (asc → desc → clear)`)}
	>
		<span class="th-label">
			<span>{label}</span>
			{#if sortKey === key}
				<span class="arrow">{sortDir === 1 ? '▲' : '▼'}</span>
			{/if}
		</span>
		<!-- svelte-ignore a11y_no_static_element_interactions -->
		<span
			class="col-resize"
			onpointerdown={(e) => onColResizeStart(e, col)}
			onpointermove={onColResizeMove}
			onpointerup={onColResizeEnd}
			onpointercancel={onColResizeEnd}
		></span>
	</th>
{/snippet}

<div class="tt-root" data-density={uiPrefs.library_density}>
	{#if masterFold === 'above'}
		<button
			type="button"
			class="master-fold above"
			onclick={jumpToMaster}
			title="Master track is above - click to jump"
		>
			▲ MASTER
		</button>
	{/if}
	{#if masterFold === 'below'}
		<button
			type="button"
			class="master-fold below"
			onclick={jumpToMaster}
			title="Master track is below - click to jump"
		>
			▼ MASTER
		</button>
	{/if}
	<div
		class="table-wrap"
		bind:this={wrapEl}
		onscroll={(e) => {
			const top = e.currentTarget.scrollTop;
			liveScrollTop = top;
			onscrollcursor(top);
		}}
	>
		<table data-testid="track-table">
			<colgroup>
				<col style={`width:${colWidths.funnel}px`} />
				<col style={`width:${colWidths.err}px`} />
				<col style={`width:${colWidths.cloud}px`} />
				<col style={`width:${colWidths.order}px`} />
				<col style={`width:${colWidths.preview}px`} />
				<col style={`width:${colWidths.art}px`} />
				<col style={`width:${colWidths.title}px`} />
				<col style={`width:${colWidths.artist}px`} />
				<col style={`width:${colWidths.key}px`} />
				<col style={`width:${colWidths.bpm}px`} />
				<col style={`width:${colWidths.plays}px`} />
				<col style={`width:${colWidths.rating}px`} />
				<col style={`width:${colWidths.comments}px`} />
				<col style={`width:${colWidths.time}px`} />
				<col style={`width:${colWidths.quality}px`} />
				<col style={`width:${colWidths.genre}px`} />
				<col style={`width:${colWidths.stems}px`} />
				<col style={`width:${colWidths.autoplay}px`} />
			</colgroup>
			<thead>
				<tr>
					<th class="h-icon" style={`width:${colWidths.funnel}px`} title="filter - not implemented, see PARITY-TODO">
						<svg viewBox="0 0 16 16" width="10" height="10" aria-hidden="true">
							<path d="M2 3h12l-4.5 5v5l-3-1.5V8z" fill="currentColor" />
						</svg>
						<!-- svelte-ignore a11y_no_static_element_interactions -->
						<span
							class="col-resize"
							onpointerdown={(e) => onColResizeStart(e, 'funnel')}
							onpointermove={onColResizeMove}
							onpointerup={onColResizeEnd}
							onpointercancel={onColResizeEnd}
						></span>
					</th>
					<th
						class="h-icon h-err"
						style={`width:${colWidths.err}px`}
						title="Err - detected analysis data-quality issues, hover a square for detail"
					>
						Err
						<!-- svelte-ignore a11y_no_static_element_interactions -->
						<span
							class="col-resize"
							onpointerdown={(e) => onColResizeStart(e, 'err')}
							onpointermove={onColResizeMove}
							onpointerup={onColResizeEnd}
							onpointercancel={onColResizeEnd}
						></span>
					</th>
					<th class="h-icon" style={`width:${colWidths.cloud}px`} title="cloud/streaming flag">
						<svg viewBox="0 0 16 16" width="11" height="11" aria-hidden="true">
							<path
								d="M4.5 12a3 3 0 0 1-.4-5.97A4 4 0 0 1 12 6.5 2.75 2.75 0 0 1 11.5 12z"
								fill="currentColor"
							/>
						</svg>
						<!-- svelte-ignore a11y_no_static_element_interactions -->
						<span
							class="col-resize"
							onpointerdown={(e) => onColResizeStart(e, 'cloud')}
							onpointermove={onColResizeMove}
							onpointerup={onColResizeEnd}
							onpointercancel={onColResizeEnd}
						></span>
					</th>
					{@render sortableTh('order', '#', 'order')}
					<th
						class="h-preview"
						style={`width:${colWidths.preview}px`}
						title={columnHeaderTitle('preview')}
					>
						Preview
						<!-- svelte-ignore a11y_no_static_element_interactions -->
						<span
							class="col-resize"
							onpointerdown={(e) => onColResizeStart(e, 'preview')}
							onpointermove={onColResizeMove}
							onpointerup={onColResizeEnd}
							onpointercancel={onColResizeEnd}
						></span>
					</th>
					<th
						class="h-art"
						style={`width:${colWidths.art}px`}
						title={columnHeaderTitle('art')}
					>
						Artwork
						<!-- svelte-ignore a11y_no_static_element_interactions -->
						<span
							class="col-resize"
							onpointerdown={(e) => onColResizeStart(e, 'art')}
							onpointermove={onColResizeMove}
							onpointerup={onColResizeEnd}
							onpointercancel={onColResizeEnd}
						></span>
					</th>
					{@render sortableTh('title', 'Track Title', 'title')}
					{@render sortableTh('artist', 'Artist', 'artist')}
					<!-- svelte-ignore a11y_click_events_have_key_events -->
					<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
					<th
						class="h-key sortable"
						style={`width:${colWidths.key}px`}
						onclick={(e) => {
							if ((e.target as HTMLElement).closest('.col-resize')) return;
							onsort('key');
						}}
						title={columnHeaderTitle(
							'key',
							masterKey !== null
								? `Master key ${masterKey} - sort by key (asc → desc → clear)`
								: 'Sort by key (asc → desc → clear)'
						)}
					>
						<span class="th-label">
							{#if masterKey !== null}
								<span
									class="th-master-val"
									style={masterKeyColor !== null ? `color:${masterKeyColor}` : undefined}
								>{masterKey}</span>
							{:else}
								<span>K</span>
							{/if}
							{#if sortKey === 'key'}
								<span class="arrow">{sortDir === 1 ? '▲' : '▼'}</span>
							{/if}
						</span>
						<!-- svelte-ignore a11y_no_static_element_interactions -->
						<span
							class="col-resize"
							onpointerdown={(e) => onColResizeStart(e, 'key')}
							onpointermove={onColResizeMove}
							onpointerup={onColResizeEnd}
							onpointercancel={onColResizeEnd}
						></span>
					</th>
					<!-- svelte-ignore a11y_click_events_have_key_events -->
					<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
					<th
						class="h-bpm sortable"
						style={`width:${colWidths.bpm}px`}
						onclick={(e) => {
							if ((e.target as HTMLElement).closest('.col-resize')) return;
							onsort('bpm');
						}}
						title={columnHeaderTitle(
							'bpm',
							masterBpm !== null
								? `Master BPM ${_fmtBpm(masterBpm)} - sort by BPM (asc → desc → clear)`
								: 'Sort by BPM (asc → desc → clear)'
						)}
					>
						<span class="th-label">
							{#if masterBpm !== null}
								<span
									class="th-master-val"
									style={masterBpmColor !== null ? `color:${masterBpmColor}` : undefined}
								>{_fmtBpm(masterBpm)}</span>
							{:else}
								<span>B</span>
							{/if}
							{#if sortKey === 'bpm'}
								<span class="arrow">{sortDir === 1 ? '▲' : '▼'}</span>
							{/if}
						</span>
						<!-- svelte-ignore a11y_no_static_element_interactions -->
						<span
							class="col-resize"
							onpointerdown={(e) => onColResizeStart(e, 'bpm')}
							onpointermove={onColResizeMove}
							onpointerup={onColResizeEnd}
							onpointercancel={onColResizeEnd}
						></span>
					</th>
					{@render sortableTh('plays', '▶', 'plays')}
					{@render sortableTh('rating', 'Rating', 'rating')}
					{@render sortableTh('comments', 'Comments', 'comments')}
					{@render sortableTh('time', 'Time', 'time')}
					<th
						class="h-quality"
						style={`width:${colWidths.quality}px`}
						title="biggest venue this file survives - from effective bitrate (size over duration) and container. Hover a badge for the kbps"
					>
						<span class="th-label"><span>Venue</span></span>
						<!-- svelte-ignore a11y_no_static_element_interactions -->
						<span
							class="col-resize"
							onpointerdown={(e) => onColResizeStart(e, 'quality')}
							onpointermove={onColResizeMove}
							onpointerup={onColResizeEnd}
							onpointercancel={onColResizeEnd}
						></span>
					</th>
					{@render sortableTh('genre', 'Genre', 'genre')}
					<th
						class="h-stems"
						style={`width:${colWidths.stems}px`}
						title="Stems - [V] vocals, [I] instruments (bass+other), [D] drums. Hover for model, overlap, format, sizes"
					>
						<span class="th-label"><span>Stems</span></span>
						<!-- svelte-ignore a11y_no_static_element_interactions -->
						<span
							class="col-resize"
							onpointerdown={(e) => onColResizeStart(e, 'stems')}
							onpointermove={onColResizeMove}
							onpointerup={onColResizeEnd}
							onpointercancel={onColResizeEnd}
						></span>
					</th>
					<th
						class="h-icon h-autoplay"
						style={`width:${colWidths.autoplay}px`}
						title="AutoPlay order - rank in AutoPlay's next handoffs for the open playlist"
					>
						{#if autoPlayMode !== 'off'}
							<AutoPlayExplainer>
								{#snippet demo()}
									{#if autoPlayMode === 'greedy' || autoPlayMode === 'reach' || autoPlayMode === 'enforce'}
										<AutoPlayWalkthrough mode={autoPlayMode} />
									{/if}
								{/snippet}
								<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
									<!-- robot head -->
									<rect x="3" y="4" width="10" height="8" rx="1.5" fill="currentColor" />
									<circle cx="6" cy="8" r="1.1" fill="var(--rb-bg, #0a0c0f)" />
									<circle cx="10" cy="8" r="1.1" fill="var(--rb-bg, #0a0c0f)" />
									<rect x="7.25" y="1.5" width="1.5" height="2.5" fill="currentColor" />
									<circle cx="8" cy="1.5" r="1" fill="currentColor" />
								</svg>
							</AutoPlayExplainer>
						{:else}
							<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
								<rect x="3" y="4" width="10" height="8" rx="1.5" fill="currentColor" />
								<circle cx="6" cy="8" r="1.1" fill="var(--rb-bg, #0a0c0f)" />
								<circle cx="10" cy="8" r="1.1" fill="var(--rb-bg, #0a0c0f)" />
								<rect x="7.25" y="1.5" width="1.5" height="2.5" fill="currentColor" />
								<circle cx="8" cy="1.5" r="1" fill="currentColor" />
							</svg>
						{/if}
						<!-- svelte-ignore a11y_no_static_element_interactions -->
						<span
							class="col-resize"
							onpointerdown={(e) => onColResizeStart(e, 'autoplay')}
							onpointermove={onColResizeMove}
							onpointerup={onColResizeEnd}
							onpointercancel={onColResizeEnd}
						></span>
					</th>
				</tr>
			</thead>
			<tbody>
				{#if windowInfo.topPad > 0}
					<tr class="tt-spacer" style={`height:${windowInfo.topPad}px`} aria-hidden="true">
						<td colspan={AUTOPLAY_COL_COUNT}></td>
					</tr>
				{/if}
				{#each visibleRows as row (`${row.stable_id}:${row.order}`)}
					<!-- key includes order: playlists CAN repeat a track -->
					<!-- svelte-ignore a11y_click_events_have_key_events -->
					<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
					<tr
						use:observeRow={row}
						data-testid="track-row"
						data-stable-id={row.stable_id}
						draggable="true"
						class:rb-row-selected={selectedIdSet.has(row.stable_id)}
						class:rb-row-menu={quickDrawUi.menuHighlightStableId === row.stable_id}
						class:rb-row-key-compat={keyCompat(row.key)}
						class:rb-row-spotify-pending={row.spotify_pending === true ||
							row.stable_id.startsWith('spotify-pending:')}
						class:loaded={loadedIds.has(row.stable_id)}
						class:rb-row-master={masterStableId !== null && row.stable_id === masterStableId}
						class:rb-row-deck-hover={hoverStableId !== null &&
							row.stable_id === hoverStableId &&
							row.stable_id !== masterStableId}
						class:rb-row-suggest-hover={suggestHoverId !== null &&
							row.stable_id === suggestHoverId}
						class:rb-row-find={findQuery !== '' && rowMatchesFind(row, findQuery)}
						class:broken={!row.file_exists &&
							!(row.is_streaming ?? row.rb_meta?.is_streaming) &&
							row.spotify_pending !== true &&
							!row.stable_id.startsWith('spotify-pending:')}
						class:rb-row-job={jobProgress.activeFor(row.stable_id) !== null}
						style={_jobRowStyle(row.stable_id)}
						onclick={(event) => onRowPointer(event, row)}
						ondblclick={(e) => onRowDblClick(e, row)}
						ondragstart={(e) => onRowDragStart(e, row)}
						ondragend={onRowDragEnd}
						ondragover={onRowDragOver}
						ondrop={(e) => onRowDrop(e, row)}
					>
						<!-- Prefetch markers live INSIDE the first cell, never as a bare
						     child of <tr>: a non-<td> row child gets wrapped in an anonymous
						     table cell, which under table-layout:fixed + <colgroup> eats a
						     column slot and shifts every real cell one column right. That
						     showed up as decked (= prefetched) rows rendering offset. -->
						<td class="c-funnel">
							{#if audioPrefetchStatus(row.stable_id) === 'ready'}
								<span
									class="audio-cache-chevron"
									title="Audio cached for fast deck load"
									aria-hidden="true"
								>▸</span>
							{:else if audioPrefetchStatus(row.stable_id) === 'loading'}
								<span
									class="audio-cache-dot"
									title="Prefetching audio"
									aria-hidden="true"
								></span>
							{/if}
							<AnalysisDots badge={_badgeFor(row)} />
						</td>
						<td class="c-err">
							<AnalysisDots issues={_issuesFor(row)} mode="issues" />
						</td>
						<!-- Cloud column is DATA-DRIVEN: rekordbox's per-row cloud icons
						     reflect Cloud Library Sync state we do not have locally, so a
						     cloud renders ONLY for real streaming rows and '!' for missing
						     files - an empty cell is the honest state for local tracks.
						     is_streaming is inline for playlist rows (contract 4); All
						     Tracks rows fall back to the lazily fetched rb-meta. -->
						<td class="c-cloud">
							{#if row.is_streaming ?? row.rb_meta?.is_streaming}
								<span
									class="cloud"
									title="streaming track (tidal/soundcloud/spotify) - deck load not implemented, see PARITY-TODO"
								>
									<svg viewBox="0 0 16 16" width="11" height="11" aria-hidden="true">
										<path
											d="M4.5 12a3 3 0 0 1-.4-5.97A4 4 0 0 1 12 6.5 2.75 2.75 0 0 1 11.5 12z"
											fill="currentColor"
										/>
									</svg>
								</span>
							{:else if !row.file_exists}
								<span class="missing" title="audio file missing on disk (broken link)">!</span>
							{/if}
						</td>
						<td class="c-order">
							{#if reorderable}
								<!-- svelte-ignore a11y_no_static_element_interactions -->
								<span
									class="grip"
									draggable="true"
									title="drag to reorder"
									aria-label="drag to reorder"
									ondragstart={(e) => onGripDragStart(e, row)}
									onclick={(e) => e.stopPropagation()}
								>
									&#8942;&#8942;
								</span>
							{/if}
							{row.order}
						</td>
						<td class="c-preview">
							<PreviewStrip
								strip={row.strip}
								vocals={vocalsById[row.stable_id] ?? null}
								duration_ms={row.duration_ms}
								revealed={row.revealed}
								nowRatio={_nowRatioFor(row.stable_id)}
								onseek={(ratio) => onpreviewseek?.(row, ratio)}
							/>
							<span class="deck-btns">
								{#each DECKS as d (d)}
									<button
										title={`Load onto deck ${d}`}
										onclick={(e) => {
											e.stopPropagation();
											onloadrow(row, d);
										}}
										ondblclick={(e) => e.stopPropagation()}
									>
										{d}
									</button>
								{/each}
								{#if removable}
									<button
										class="remove-btn"
										title="remove from playlist"
										onclick={(e) => {
											e.stopPropagation();
											onremoverow?.(row);
										}}
										ondblclick={(e) => e.stopPropagation()}
									>
										&times;
									</button>
								{/if}
							</span>
						</td>
						<td
							class="c-art"
							title={row.rb_meta !== null && !row.rb_meta.artwork_available
								? (artworkStatusLabel(row.rb_meta.artwork_status) ??
									'artwork unavailable')
								: undefined}
						>
							<span class="art-slate" aria-hidden="true"></span>
							{#if row.rb_meta !== null && row.rb_meta.artwork_available}
								<img
									src={artworkUrl(row.stable_id, 's')}
									alt=""
									loading="lazy"
									onerror={_hideBrokenImg}
								/>
							{/if}
						</td>
						<td class="c-title" class:rb-row-loaded={loadedIds.has(row.stable_id)} title={row.title ?? ''}>
							{#each hl(row.title) as part, i (i)}
								{#if part.hit}<mark class="find-hit">{part.text}</mark>{:else}{part.text}{/if}
							{/each}
						</td>
						<td class="c-artist" class:rb-row-loaded={loadedIds.has(row.stable_id)} title={row.artist ?? ''}>
							{#each hl(row.artist) as part, i (i)}
								{#if part.hit}<mark class="find-hit">{part.text}</mark>{:else}{part.text}{/if}
							{/each}
						</td>
						<td
							class="c-key"
							class:key-compat={keyCompat(row.key)}
							style={keyCompatStyle(row.key)}
							title={camelotKeyHoverLabel(row.key) ?? undefined}
						>
							{#each hl(row.key) as part, i (i)}
								{#if part.hit}<mark class="find-hit">{part.text}</mark>{:else}{part.text}{/if}
							{/each}
						</td>
						<td
							class="c-bpm"
							class:bpm-sweet={bpmCellHeat(row.bpm)?.lane === 'sweet'}
							class:bpm-half={bpmCellHeat(row.bpm)?.lane === 'half'}
							class:bpm-far={bpmCellHeat(row.bpm)?.lane === 'far'}
							style={bpmCellStyle(row.bpm)}
							title={bpmHeatLabel(bpmCellHeat(row.bpm), masterBpm) ?? undefined}
						>{_fmtBpm(row.bpm)}</td>
						<td
							class="c-plays"
							title="play count (rekordbox history + djay)"
						>{row.play_count > 0 ? String(row.play_count) : ''}</td>
						<td class="c-rating">
							<RatingStars rating={row.rating} onrate={(n) => onrate(row, n)} />
						</td>
						<td class="c-comments" title={row.comments ?? ''}>
							{#each hl(row.comments) as part, i (i)}
								{#if part.hit}<mark class="find-hit">{part.text}</mark>{:else}{part.text}{/if}
							{/each}
						</td>
						<td class="c-time">{_fmtTime(row.duration_ms)}</td>
						<td class="c-quality">
							<QualityBadge quality={row.quality} />
						</td>
						<td class="c-genre">
							{#each splitGenreTags(row.genre ?? row.rb_meta?.genre ?? '') as tag, i (tag + String(i))}
								{#if i > 0}<span class="genre-sep">, </span>{/if}
								<button
									type="button"
									class="genre-tag"
									class:active={/^genre:~?/i.test(searchQuery.trim()) &&
										searchQuery
											.trim()
											.replace(/^genre:~?/i, '')
											.toLowerCase() === tag.toLowerCase()}
									style={genreTagStyle(tag)}
									title="click to filter by this genre (again clears). double = loose. triple = undo. after filter: 20s library double clears, triple undoes"
									onclick={(e) => onGenreTagClick(e, tag)}
									ondblclick={(e) => {
										e.stopPropagation();
										e.preventDefault();
									}}
								>
									{#each hl(tag) as part, j (j)}
										{#if part.hit}<mark class="find-hit">{part.text}</mark>{:else}{part.text}{/if}
									{/each}
								</button>
							{/each}
						</td>
						<td class="c-stems">
							<StemTags stems={row.stems} />
						</td>
						<td class="c-autoplay">
							{#if _autoPlayRank(row.stable_id) !== null}
								{@const rank = _autoPlayRank(row.stable_id)!}
								<span
									class="ap-rank"
									class:ap-rank-hot={hoveredApId === row.stable_id}
									tabindex="0"
									title={`AutoPlay will hand off to this track after ${rank - 1} more, for the open playlist`}
									onpointerenter={() => (hoveredApId = row.stable_id)}
									onpointerleave={() => {
										if (hoveredApId === row.stable_id) hoveredApId = null;
									}}
									onfocus={() => (hoveredApId = row.stable_id)}
									onblur={() => {
										if (hoveredApId === row.stable_id) hoveredApId = null;
									}}
								>{rank}{AUTOPLAY_ARROW}</span>
							{/if}
						</td>
					</tr>
				{/each}
				{#if windowInfo.bottomPad > 0}
					<tr class="tt-spacer" style={`height:${windowInfo.bottomPad}px`} aria-hidden="true">
						<td colspan={AUTOPLAY_COL_COUNT}></td>
					</tr>
				{/if}
			</tbody>
		</table>
		{#if rows.length === 0 && emptyMessage !== null}
			<div class="empty">{emptyMessage}</div>
		{/if}
		{#if apCurveSegments.length > 0}
			<svg
				class="ap-curve"
				aria-hidden="true"
				style={`--ap-curve-w:${colWidths.autoplay}px`}
			>
				{#each apCurveSegments as seg (`${seg.from.stable_id}-${seg.to.stable_id}`)}
					<path
						d={segmentPath(seg, apCurveX)}
						class="ap-curve-seg"
						class:skips={seg.skips}
						fill="none"
					/>
					<circle
						cx={apCurveX}
						cy={seg.from.y}
						r={hoveredApId === seg.from.stable_id ? 3.5 : 2}
						class="ap-curve-node"
						class:hot={hoveredApId === seg.from.stable_id}
					/>
					<circle
						cx={apCurveX}
						cy={seg.to.y}
						r={hoveredApId === seg.to.stable_id ? 3.5 : 2}
						class="ap-curve-node"
						class:hot={hoveredApId === seg.to.stable_id}
					/>
				{/each}
			</svg>
		{/if}
	</div>
	{#if provider.truncated}
		<div class="truncated-note">
			list truncated - the source fetch hit its safety cap before completing
		</div>
	{/if}
</div>

{#if loadConfirm !== null}
	<div
		class="load-confirm"
		style={`left:${loadConfirm.x}px;top:${loadConfirm.y}px`}
		role="dialog"
		aria-label={`Load and play CH${loadConfirm.deck}?`}
	>
		<span class="load-confirm-q">Load+play CH{loadConfirm.deck}?</span>
		<label class="load-confirm-every">
			<input type="checkbox" bind:checked={loadConfirmEveryTime} />
			<span>do this every time</span>
		</label>
		<button
			type="button"
			class="load-confirm-yes"
			onclick={() => {
				const pending = loadConfirm;
				const remember = loadConfirmEveryTime;
				loadConfirm = null;
				loadConfirmEveryTime = false;
				if (pending === null) return;
				if (remember) setConfirmPref('dblclick_load_play', false);
				onloadrow(pending.row, pending.deck, { play: true });
			}}>Yes</button
		>
		<button
			type="button"
			class="load-confirm-no"
			onclick={() => {
				loadConfirm = null;
				loadConfirmEveryTime = false;
			}}>No</button
		>
	</div>
{/if}

<style>

	/* ----- AUTOPLAY-COL --------------------------------------------------- */
	.c-autoplay {
		text-align: center;
		font-variant-numeric: tabular-nums;
		padding: 0 2px;
	}
	.ap-rank {
		display: inline-block;
		font-style: italic;
		font-size: 10px;
		color: var(--rb-text-dim);
		cursor: default;
		outline: none;
	}
	.ap-rank-hot,
	.ap-rank:focus {
		color: var(--rb-accent);
	}
	.h-autoplay :global(.ap-explain-wrap) {
		color: inherit;
	}
	.ap-curve {
		position: absolute;
		top: 0;
		right: 0;
		bottom: 0;
		width: var(--ap-curve-w, 46px);
		pointer-events: none;
		z-index: 5;
		overflow: visible;
	}
	.ap-curve-seg {
		stroke: var(--rb-accent);
		stroke-width: 1.25;
		opacity: 0.75;
	}
	.ap-curve-seg.skips {
		stroke-dasharray: 3 3;
		opacity: 0.55;
	}
	.ap-curve-node {
		fill: var(--rb-accent);
		opacity: 0.85;
	}
	.ap-curve-node.hot {
		opacity: 1;
	}

	.tt-root {
		display: flex;
		flex-direction: column;
		flex: 1;
		min-height: 0;
		position: relative;
		/* compact = current tight rows; cosy = taller + roomier cell pad */
		--tt-row-h: 22px;
		--tt-art: 22px;
		--tt-td-pad-x: 6px;
	}
	.tt-root[data-density='cosy'] {
		--tt-row-h: 30px;
		--tt-art: 30px;
		--tt-td-pad-x: 8px;
	}
	.table-wrap {
		flex: 1;
		min-height: 0;
		overflow: auto;
		position: relative;
	}
	table {
		width: 100%;
		border-collapse: collapse;
		table-layout: fixed;
		font-size: var(--rb-fs-browser);
	}

	/* column widths are runtime-resizable via header drag handles */
	thead th {
		position: sticky;
		top: 0;
		z-index: 1;
		height: 20px;
		padding: 0 var(--tt-td-pad-x);
		background: var(--rb-panel-raised);
		border-bottom: 1px solid var(--rb-border);
		color: var(--rb-text-dim);
		font-weight: 400;
		text-align: left;
		white-space: nowrap;
		overflow: visible;
	}
	.col-resize {
		position: absolute;
		top: 0;
		right: -3px;
		width: 7px;
		height: 100%;
		cursor: col-resize;
		z-index: 2;
	}
	.col-resize:hover {
		background: rgba(232, 161, 58, 0.35);
	}
	.h-icon {
		text-align: center;
	}
	.h-err {
		font-size: 9px;
		font-weight: 600;
		letter-spacing: 0.02em;
	}
	th.sortable {
		padding: 0;
		cursor: pointer;
	}
	th.sortable:hover {
		color: var(--rb-text);
		background: color-mix(in srgb, var(--rb-panel-raised) 70%, #2a3140);
	}
	.th-master-val {
		font-variant-numeric: tabular-nums;
		font-weight: 700;
	}
	.th-label {
		display: flex;
		align-items: center;
		gap: 3px;
		height: 100%;
		width: 100%;
		padding: 0 var(--tt-td-pad-x);
		box-sizing: border-box;
		color: inherit;
		font-family: var(--rb-font);
		font-size: var(--rb-fs-browser);
		pointer-events: none;
	}
	.arrow {
		font-size: 7px;
	}

	tbody tr {
		height: var(--tt-row-h);
		cursor: default;
		position: relative;
	}
	/* Prefetch markers - top-left of row (same corner as job wash). */
	.audio-cache-chevron {
		position: absolute;
		top: 1px;
		left: 2px;
		z-index: 2;
		font-size: 9px;
		line-height: 1;
		color: #f2f4f7;
		pointer-events: none;
		opacity: 0.85;
	}
	.audio-cache-dot {
		position: absolute;
		top: 4px;
		left: 3px;
		z-index: 2;
		width: 5px;
		height: 5px;
		border-radius: 50%;
		background: #e89a3c;
		pointer-events: none;
		box-shadow: 0 0 0 1px color-mix(in srgb, #e89a3c 35%, transparent);
	}
	/* Job progress overrides loaded/playing wash (full-row fill). */
	tbody tr.rb-row-job {
		background: transparent !important;
		box-shadow: none !important;
		outline: none !important;
		animation: none !important;
	}
	tbody tr.rb-row-job::before {
		content: '';
		position: absolute;
		inset: 0;
		z-index: 0;
		pointer-events: none;
		background: linear-gradient(
			90deg,
			color-mix(in srgb, var(--job-color) 42%, transparent) 0%,
			color-mix(in srgb, var(--job-color) 42%, transparent) var(--job-pct),
			color-mix(in srgb, var(--job-color) 10%, transparent) var(--job-pct),
			color-mix(in srgb, var(--job-color) 10%, transparent) 100%
		);
	}
	tbody tr.rb-row-job > td {
		position: relative;
		z-index: 1;
	}
	/* virtualization spacers stand in for the un-mounted rows above/below
	 * the current window - zero out padding/border so their inline height
	 * (set from windowInfo.topPad/bottomPad) stays exact. */
	.tt-spacer td {
		padding: 0;
		border: none;
		position: relative;
		overflow: hidden;
	}
	/* Subtle TL→BR white sweep (same 135deg idea as Deck.svelte
	 * .rb-deck.loading::after / deck-load-sweep) so blank row-pad gaps
	 * feel alive while scrolling. Lower opacity + slower than deck load;
	 * ::after only - must not change spacer height math (padding stays 0). */
	.tt-spacer td::after {
		content: '';
		position: absolute;
		inset: 0;
		pointer-events: none;
		background: linear-gradient(
			135deg,
			transparent 0%,
			transparent 42%,
			rgba(255, 255, 255, 0.045) 50%,
			transparent 58%,
			transparent 100%
		);
		background-size: 220% 220%;
		animation: tt-spacer-sweep 2.4s ease-in-out infinite;
	}
	@keyframes tt-spacer-sweep {
		0% {
			background-position: 100% 100%;
			opacity: 0.4;
		}
		50% {
			opacity: 0.85;
		}
		100% {
			background-position: 0% 0%;
			opacity: 0.4;
		}
	}
	tbody tr:hover:not(.rb-row-selected):not(.rb-row-menu):not(.rb-row-master) {
		background: var(--rb-panel-raised);
	}
	tbody tr.rb-row-menu {
		background: var(--rb-panel-raised);
		outline: 1px solid color-mix(in srgb, var(--rb-accent) 45%, transparent);
		outline-offset: -1px;
	}
	/* Loaded on a non-master deck: yellow edge + wash (distinct from master gold). */
	tbody tr.loaded:not(.rb-row-master):not(.rb-row-selected) {
		background: color-mix(in srgb, var(--rb-yellow) 12%, transparent);
		box-shadow: inset 3px 0 0 var(--rb-yellow);
	}
	tbody tr.loaded:hover:not(.rb-row-master):not(.rb-row-selected):not(.rb-row-menu) {
		background: color-mix(in srgb, var(--rb-yellow) 20%, var(--rb-panel-raised));
	}
	/* Master: biggest pop - gold edge + strong wash. */
	tbody tr.rb-row-master {
		background: color-mix(in srgb, var(--rb-master-gold, #c9b35a) 28%, transparent);
		box-shadow:
			inset 5px 0 0 var(--rb-master-gold, #c9b35a),
			inset -1px 0 0 color-mix(in srgb, var(--rb-master-gold, #c9b35a) 55%, transparent);
		outline: 1px solid color-mix(in srgb, var(--rb-master-gold, #c9b35a) 70%, transparent);
		outline-offset: -1px;
	}
	tbody tr.rb-row-master:hover {
		background: color-mix(in srgb, var(--rb-master-gold, #c9b35a) 36%, var(--rb-panel-raised));
	}
	tbody tr.rb-row-master .c-title,
	tbody tr.rb-row-master .c-artist {
		color: #e8d78a;
		font-weight: 700;
	}
	/* Hovered deck's library track (non-master): light pulse to help find it. */
	tbody tr.rb-row-suggest-hover {
		outline: 1px solid #ff4da6;
		background: color-mix(in srgb, #ff4da6 16%, transparent) !important;
	}
	tbody tr.rb-row-deck-hover:not(.rb-row-selected):not(.rb-row-menu) {
		animation: deck-lib-pulse 0.9s ease-in-out infinite;
		box-shadow: inset 3px 0 0 rgba(255, 255, 255, 0.45);
	}
	@keyframes deck-lib-pulse {
		0%,
		100% {
			background: color-mix(in srgb, rgba(255, 255, 255, 0.06) 100%, transparent);
		}
		50% {
			background: color-mix(in srgb, rgba(255, 255, 255, 0.14) 100%, transparent);
		}
	}
	/* Camelot-compatible / suggested-next: faint green (go / mixable). */
	tbody tr.rb-row-key-compat:not(.rb-row-selected):not(.rb-row-menu):not(.loaded):not(.rb-row-master):not(
			.rb-row-spotify-pending
		) {
		background: color-mix(in srgb, var(--rb-green) 9%, transparent);
	}
	tbody tr.rb-row-key-compat:hover:not(.rb-row-selected):not(.rb-row-menu):not(.loaded):not(
			.rb-row-master
		):not(.rb-row-spotify-pending) {
		background: color-mix(in srgb, var(--rb-green) 15%, var(--rb-panel-raised));
	}
	/* Spotify unmatched / pending acquisition: light green tint (stronger than
	 * Camelot-compat so the missing-local rows read as intentional placeholders). */
	tbody tr.rb-row-spotify-pending:not(.rb-row-selected):not(.rb-row-menu):not(.loaded):not(
			.rb-row-master
		) {
		background: color-mix(in srgb, var(--rb-green) 18%, transparent);
		box-shadow: inset 3px 0 0 color-mix(in srgb, var(--rb-green) 55%, transparent);
	}
	tbody tr.rb-row-spotify-pending:hover:not(.rb-row-selected):not(.rb-row-menu):not(.loaded):not(
			.rb-row-master
		) {
		background: color-mix(in srgb, var(--rb-green) 26%, var(--rb-panel-raised));
	}

	.master-fold {
		position: absolute;
		left: 50%;
		transform: translateX(-50%);
		z-index: 4;
		padding: 3px 14px;
		border: 1px solid #c9b35a;
		border-radius: 3px;
		background: color-mix(in srgb, #c9b35a 88%, #1a1608);
		color: #1a1608;
		font-family: var(--rb-font);
		font-size: 10px;
		font-weight: 700;
		letter-spacing: 0.06em;
		cursor: pointer;
		box-shadow: 0 2px 10px rgba(0, 0, 0, 0.45);
		pointer-events: auto;
	}
	.master-fold:hover {
		background: #e0cc6e;
	}
	.master-fold.above {
		top: 22px;
	}
	.master-fold.below {
		bottom: 4px;
	}
	td {
		padding: 0 var(--tt-td-pad-x);
		border-bottom: 1px solid #131519;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		vertical-align: middle;
	}
	.c-order,
	.c-bpm,
	.c-plays,
	.c-time {
		text-align: right;
		font-variant-numeric: tabular-nums;
		color: var(--rb-text-dim);
	}
	.c-plays {
		font-size: 10px;
	}
	.c-quality {
		overflow: hidden;
		white-space: nowrap;
	}
	.c-order .grip {
		margin-right: 2px;
		font-size: 8px;
		letter-spacing: -2px;
		color: var(--rb-text-dim);
		cursor: grab;
	}
	.c-order .grip:hover {
		color: var(--rb-text);
	}
	.c-key {
		color: var(--rb-text-dim);
		box-sizing: border-box;
	}
	.c-key.key-compat {
		border-radius: 2px;
		padding-left: 4px;
		padding-right: 4px;
	}
	/* Sweet BPM: green wash only (no border). Half = purple wash. */
	.c-bpm.bpm-sweet {
		border-radius: 2px;
		background: color-mix(in srgb, var(--rb-green) 18%, transparent);
	}
	.c-bpm.bpm-half {
		border-radius: 2px;
		background: color-mix(in srgb, #a855f7 12%, transparent);
	}
	.c-bpm.bpm-far {
		border-radius: 2px;
	}
	.c-cloud {
		text-align: center;
		color: var(--rb-text-dim);
	}
	.missing {
		color: var(--rb-red);
		font-weight: 600;
	}

	/* FR-1: missing-file rows gray out (dim text + dim artwork) but stay
	 * selectable; deck load is blocked upstream with an explicit toast. */
	tbody tr.broken td {
		color: var(--rb-text-dim);
	}
	tbody tr.broken .c-art img,
	tbody tr.broken .art-slate {
		opacity: 0.35;
	}
	tbody tr.broken :global(canvas) {
		opacity: 0.45;
	}
	/* the '!' badge keeps its red even on grayed rows */
	tbody tr.broken .missing {
		color: var(--rb-red);
	}
	.c-comments,
	.c-genre {
		color: var(--rb-text-dim);
	}
	.c-stems {
		overflow: hidden;
		white-space: nowrap;
	}
	.genre-sep {
		color: var(--rb-text-dim);
	}
	.genre-tag {
		display: inline;
		margin: 0;
		padding: 0;
		border: none;
		background: transparent;
		color: inherit;
		font: inherit;
		cursor: pointer;
		border-radius: 1px;
		transition:
			color 80ms ease,
			text-shadow 80ms ease;
	}
	.genre-tag:hover {
		color: var(--genre-glow, #f2f5f8);
		text-shadow:
			0 0 6px color-mix(in srgb, var(--genre-glow, #e8f0ff) 80%, transparent),
			0 0 14px color-mix(in srgb, var(--genre-glow, #b4d2ff) 45%, transparent);
	}
	.genre-tag.active {
		color: var(--rb-text);
		text-decoration: underline;
		text-underline-offset: 2px;
	}
	tbody tr.rb-row-find {
		background: color-mix(in srgb, var(--rb-yellow, #e8a13a) 10%, transparent);
	}
	.find-hit {
		padding: 0;
		margin: 0;
		background: color-mix(in srgb, var(--rb-yellow, #e8a13a) 55%, transparent);
		color: inherit;
		border-radius: 1px;
	}

	/* preview cell hosts the hover deck-load buttons */
	.c-preview {
		position: relative;
	}
	.deck-btns {
		display: none;
		position: absolute;
		top: 2px;
		right: 4px;
		gap: 2px;
	}
	tbody tr:hover .deck-btns {
		display: inline-flex;
	}
	.deck-btns button {
		width: 16px;
		height: 16px;
		padding: 0;
		background: var(--rb-panel-raised);
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		color: var(--rb-text);
		font-size: 9px;
		line-height: 1;
		cursor: pointer;
	}
	.deck-btns button:hover {
		background: var(--rb-accent);
		color: #fff;
	}
	.deck-btns button.remove-btn {
		color: var(--rb-red);
		font-size: 11px;
		font-weight: 700;
	}
	.deck-btns button.remove-btn:hover {
		background: var(--rb-red);
		color: #fff;
	}

	/* Artwork fills the row with no v-pad/border so adjacent thumbs touch.
	 * Other columns keep --tt-td-pad-x. */
	.c-art {
		position: relative;
		padding: 0;
		width: var(--tt-art);
		border-bottom: none;
		overflow: hidden;
		vertical-align: middle;
	}
	.art-slate {
		display: block;
		width: var(--tt-art);
		height: var(--tt-art);
		background: #22262c;
		border: none;
	}
	.c-art img {
		position: absolute;
		inset: 0;
		width: var(--tt-art);
		height: var(--tt-art);
		object-fit: cover;
		display: block;
	}

	.empty {
		padding: 24px;
		text-align: center;
		color: var(--rb-text-dim);
	}
	.truncated-note {
		flex: none;
		padding: 2px 8px;
		border-top: 1px solid var(--rb-border);
		color: var(--rb-text-dim);
		font-size: var(--rb-fs-label);
	}

	.load-confirm {
		position: fixed;
		z-index: 80;
		transform: translate(-50%, -100%);
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 6px;
		padding: 4px 8px;
		background: var(--rb-panel-raised);
		border: 1px solid var(--rb-border);
		box-shadow: 0 2px 8px rgba(0, 0, 0, 0.45);
		font-size: 11px;
		color: var(--rb-text);
		pointer-events: auto;
		max-width: 280px;
	}
	.load-confirm-q {
		white-space: nowrap;
	}
	.load-confirm-every {
		display: inline-flex;
		align-items: center;
		gap: 3px;
		color: var(--rb-text-dim);
		cursor: pointer;
		white-space: nowrap;
		flex: 1 1 100%;
		order: 3;
	}
	.load-confirm-every input {
		width: 10px;
		height: 10px;
		margin: 0;
		accent-color: var(--rb-accent);
		cursor: pointer;
	}
	.load-confirm-yes {
		order: 1;
	}
	.load-confirm-no {
		order: 2;
	}
	.load-confirm button {
		border: 1px solid var(--rb-border);
		background: var(--rb-panel);
		color: var(--rb-text);
		padding: 2px 8px;
		font-size: 11px;
		cursor: pointer;
	}
	.load-confirm-yes:hover {
		background: var(--rb-accent);
		color: #fff;
		border-color: var(--rb-accent);
	}
	.load-confirm-no:hover {
		background: #444;
	}
</style>
