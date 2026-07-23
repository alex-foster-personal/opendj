<script module lang="ts">
	// Row + sort vocabulary now lives in the pane contract (browser-surface
	// unit); re-exported here so existing importers keep working.
	export type { BrowserRow, SortKey } from './pane-contract.svelte';
</script>

<script lang="ts">
	// Browser track table (SCREENSHOT-SPEC 5c). Columns in screenshot order:
	// funnel | cloud | # | Preview | Artwork | Track Title | Artist | K | B |
	// Rating | Comments | Time | Genre. Preview strips + file_exists arrive
	// INLINE (contract 1/4); the IntersectionObserver now only reveals rows
	// (one-time canvas draw) and triggers the lazy rb-meta fetch (artwork).
	// Row states: green title+artist = loaded on a deck; blue full row =
	// selected; grayed row = audio file missing on disk (FR-1).
	// Rows arrive via the RowProvider contract (pane-contract.svelte.ts).
	// The provider materializes the full result set (parent no longer caps
	// fetches at 500 rows - track-list-virtualization lane); THIS component
	// DOM-virtualizes the render: only the scrolled window (+overscan) is
	// ever mounted, so a multi-thousand-row pane stays cheap regardless of
	// provider.total.
	import { untrack } from 'svelte';
	import { artworkUrl, type Vocals } from '$lib/rb/api-rb';
	import { camelotKeyColor, camelotKeyHoverLabel } from '$lib/rb/camelot-color';
	import { bpmHeatColor } from '$lib/rb/bpm-heat';
	import { genreHoverColor } from '$lib/rb/genre-color';
	import { highlightSpans, rowMatchesFind } from '$lib/rb/find-highlight';
	import { camelotKeysAreCompatible, DECK_IDS, deckStates } from '$lib/rb/audio-engine.svelte';
	import { deckHoverUi } from '$lib/rb/deck-hover.svelte';
	import { uiPrefs } from '$lib/rb/prefs.svelte';
	import { quickDrawUi } from '$lib/rb/quick-draw-ui.svelte';
	import type { DeckId } from '$lib/rb/types';
	import type { BrowserRow, RowProvider, SortDir, SortKey } from './pane-contract.svelte';
	import PreviewStrip from './PreviewStrip.svelte';
	import RatingStars from './RatingStars.svelte';
	import { computeVirtualWindow } from './virtual-window';

	const DECKS: DeckId[] = [1, 2, 3, 4];
	// Fixed row heights (virtualization window math requires constant height).
	// compact = current tight rows; cosy = taller + slightly roomier cell pad.
	const ROW_HEIGHT_COMPACT = 22;
	const ROW_HEIGHT_COSY = 30;
	const OVERSCAN = 10;

	type ColId =
		| 'funnel'
		| 'cloud'
		| 'order'
		| 'preview'
		| 'art'
		| 'title'
		| 'artist'
		| 'key'
		| 'bpm'
		| 'rating'
		| 'comments'
		| 'time'
		| 'genre';

	const COL_DEFAULTS: Record<ColId, number> = {
		funnel: 20,
		cloud: 24,
		order: 34,
		preview: 177,
		art: 54,
		title: 220,
		artist: 140,
		key: 40,
		bpm: 46,
		rating: 80,
		comments: 110,
		time: 48,
		genre: 90
	};

	let colWidths = $state<Record<ColId, number>>({ ...COL_DEFAULTS });
	let resizeCol: ColId | null = null;
	let resizeStartX = 0;
	let resizeStartW = 0;
	let loadConfirm = $state<{
		row: BrowserRow;
		deck: 1 | 2;
		x: number;
		y: number;
	} | null>(null);

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

	const masterKey = $derived(
		DECK_IDS.map((d) => deckStates[d]).find((d) => d.is_master)?.key ?? null
	);
	const masterKeyColor = $derived(camelotKeyColor(masterKey));
	const masterBpm = $derived(
		DECK_IDS.map((d) => deckStates[d]).find((d) => d.is_master)?.bpm ?? null
	);
	const masterStableId = $derived(
		DECK_IDS.map((d) => deckStates[d]).find((d) => d.is_master)?.stable_id ?? null
	);
	const hoverStableId = $derived(
		deckHoverUi.deckId === null ? null : (deckStates[deckHoverUi.deckId].stable_id ?? null)
	);

	function keyCompat(key: string | null): boolean {
		return camelotKeysAreCompatible(key, masterKey);
	}

	function keyCompatStyle(key: string | null): string | undefined {
		const base = keyCellStyle(key);
		if (!keyCompat(key) || masterKeyColor === null) return base;
		const border = `box-shadow: inset 0 0 0 2px ${masterKeyColor}`;
		return base === undefined ? border : `${base};${border}`;
	}

	function bpmCellStyle(bpm: number | null): string | undefined {
		const color = bpmHeatColor(bpm, masterBpm);
		return color === null ? undefined : `color:${color}`;
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
		findQuery = ''
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
		onloadrow: (row: BrowserRow, deck: DeckId | null) => void;
		/** Preferred CH1/CH2 for double-click load confirm. */
		onpickdoubledeck?: (row: BrowserRow) => 1 | 2;
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
		const deck = onpickdoubledeck?.(row) ?? 1;
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
	const MIME_TRACK = 'application/x-mdt-stable-id';
	let _dragSourceOrder: number | null = null;

	function onRowDragStart(event: DragEvent, row: BrowserRow): void {
		if (!row.file_exists || row.is_streaming) {
			event.preventDefault();
			return;
		}
		event.dataTransfer?.setData(MIME_TRACK, row.stable_id);
		if (event.dataTransfer) event.dataTransfer.effectAllowed = 'copy';
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

{#snippet sortableTh(key: SortKey, label: string, col: ColId)}
	<!-- svelte-ignore a11y_click_events_have_key_events -->
	<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
	<th
		class={`h-${key} sortable`}
		style={`width:${colWidths[col]}px`}
		onclick={(e) => {
			if ((e.target as HTMLElement).closest('.col-resize')) return;
			onsort(key);
		}}
		title={`Sort by ${label} (asc → desc → clear)`}
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
		<table>
			<colgroup>
				<col style={`width:${colWidths.funnel}px`} />
				<col style={`width:${colWidths.cloud}px`} />
				<col style={`width:${colWidths.order}px`} />
				<col style={`width:${colWidths.preview}px`} />
				<col style={`width:${colWidths.art}px`} />
				<col style={`width:${colWidths.title}px`} />
				<col style={`width:${colWidths.artist}px`} />
				<col style={`width:${colWidths.key}px`} />
				<col style={`width:${colWidths.bpm}px`} />
				<col style={`width:${colWidths.rating}px`} />
				<col style={`width:${colWidths.comments}px`} />
				<col style={`width:${colWidths.time}px`} />
				<col style={`width:${colWidths.genre}px`} />
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
					<th class="h-preview" style={`width:${colWidths.preview}px`}>
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
					<th class="h-art" style={`width:${colWidths.art}px`}>
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
					{@render sortableTh('key', 'K', 'key')}
					{@render sortableTh('bpm', 'B', 'bpm')}
					{@render sortableTh('rating', 'Rating', 'rating')}
					{@render sortableTh('comments', 'Comments', 'comments')}
					{@render sortableTh('time', 'Time', 'time')}
					{@render sortableTh('genre', 'Genre', 'genre')}
				</tr>
			</thead>
			<tbody>
				{#if windowInfo.topPad > 0}
					<tr class="tt-spacer" style={`height:${windowInfo.topPad}px`} aria-hidden="true">
						<td colspan="13"></td>
					</tr>
				{/if}
				{#each visibleRows as row (`${row.stable_id}:${row.order}`)}
					<!-- key includes order: playlists CAN repeat a track -->
					<!-- svelte-ignore a11y_click_events_have_key_events -->
					<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
					<tr
						use:observeRow={row}
						data-stable-id={row.stable_id}
						draggable="true"
						class:rb-row-selected={selectedIdSet.has(row.stable_id)}
						class:rb-row-menu={quickDrawUi.menuHighlightStableId === row.stable_id}
						class:rb-row-key-compat={keyCompat(row.key)}
						class:loaded={loadedIds.has(row.stable_id)}
						class:rb-row-master={masterStableId !== null && row.stable_id === masterStableId}
						class:rb-row-deck-hover={hoverStableId !== null &&
							row.stable_id === hoverStableId &&
							row.stable_id !== masterStableId}
						class:rb-row-find={findQuery !== '' && rowMatchesFind(row, findQuery)}
						class:broken={!row.file_exists}
						onclick={(event) => onRowPointer(event, row)}
						ondblclick={(e) => onRowDblClick(e, row)}
						ondragstart={(e) => onRowDragStart(e, row)}
						ondragover={onRowDragOver}
						ondrop={(e) => onRowDrop(e, row)}
					>
						<td class="c-funnel"></td>
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
						<td class="c-art">
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
						<td class="c-bpm" style={bpmCellStyle(row.bpm)}>{_fmtBpm(row.bpm)}</td>
						<td class="c-rating">
							<RatingStars rating={row.rating} onrate={(n) => onrate(row, n)} />
						</td>
						<td class="c-comments" title={row.comments ?? ''}>
							{#each hl(row.comments) as part, i (i)}
								{#if part.hit}<mark class="find-hit">{part.text}</mark>{:else}{part.text}{/if}
							{/each}
						</td>
						<td class="c-time">{_fmtTime(row.duration_ms)}</td>
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
					</tr>
				{/each}
				{#if windowInfo.bottomPad > 0}
					<tr class="tt-spacer" style={`height:${windowInfo.bottomPad}px`} aria-hidden="true">
						<td colspan="13"></td>
					</tr>
				{/if}
			</tbody>
		</table>
		{#if rows.length === 0 && emptyMessage !== null}
			<div class="empty">{emptyMessage}</div>
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
		aria-label={`Load to CH${loadConfirm.deck}?`}
	>
		<span class="load-confirm-q">Load CH{loadConfirm.deck}?</span>
		<button
			type="button"
			class="load-confirm-yes"
			onclick={() => {
				const pending = loadConfirm;
				loadConfirm = null;
				if (pending) onloadrow(pending.row, pending.deck);
			}}>Yes</button
		>
		<button type="button" class="load-confirm-no" onclick={() => (loadConfirm = null)}>No</button>
	</div>
{/if}

<style>
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
	th.sortable {
		padding: 0;
		cursor: pointer;
	}
	th.sortable:hover {
		color: var(--rb-text);
		background: color-mix(in srgb, var(--rb-panel-raised) 70%, #2a3140);
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
	}
	/* virtualization spacers stand in for the un-mounted rows above/below
	 * the current window - zero out padding/border so their inline height
	 * (set from windowInfo.topPad/bottomPad) stays exact. */
	.tt-spacer td {
		padding: 0;
		border: none;
	}
	tbody tr:hover:not(.rb-row-selected):not(.rb-row-menu):not(.rb-row-master) {
		background: var(--rb-panel-raised);
	}
	tbody tr.rb-row-menu {
		background: var(--rb-panel-raised);
		outline: 1px solid color-mix(in srgb, var(--rb-accent) 45%, transparent);
		outline-offset: -1px;
	}
	/* Loaded on any deck: green edge + wash (title/artist stay green via theme). */
	tbody tr.loaded:not(.rb-row-master):not(.rb-row-selected) {
		background: color-mix(in srgb, var(--rb-green) 11%, transparent);
		box-shadow: inset 3px 0 0 var(--rb-green);
	}
	tbody tr.loaded:hover:not(.rb-row-master):not(.rb-row-selected):not(.rb-row-menu) {
		background: color-mix(in srgb, var(--rb-green) 18%, var(--rb-panel-raised));
	}
	/* Master: biggest pop - gold edge + strong wash. */
	tbody tr.rb-row-master {
		background: color-mix(in srgb, #c9b35a 28%, transparent);
		box-shadow:
			inset 5px 0 0 #c9b35a,
			inset -1px 0 0 color-mix(in srgb, #c9b35a 55%, transparent);
		outline: 1px solid color-mix(in srgb, #c9b35a 70%, transparent);
		outline-offset: -1px;
	}
	tbody tr.rb-row-master:hover {
		background: color-mix(in srgb, #c9b35a 36%, var(--rb-panel-raised));
	}
	tbody tr.rb-row-master .c-title,
	tbody tr.rb-row-master .c-artist {
		color: #e8d78a;
		font-weight: 700;
	}
	/* Hovered deck's library track (non-master): light pulse to help find it. */
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
	tbody tr.rb-row-key-compat:not(.rb-row-selected):not(.rb-row-menu):not(.loaded):not(.rb-row-master) {
		background: color-mix(in srgb, #c9b35a 9%, transparent);
	}
	tbody tr.rb-row-key-compat:hover:not(.rb-row-selected):not(.rb-row-menu):not(.loaded):not(
			.rb-row-master
		) {
		background: color-mix(in srgb, #c9b35a 16%, var(--rb-panel-raised));
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
	.c-time {
		text-align: right;
		font-variant-numeric: tabular-nums;
		color: var(--rb-text-dim);
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
		align-items: center;
		gap: 6px;
		padding: 4px 8px;
		background: var(--rb-panel-raised);
		border: 1px solid var(--rb-border);
		box-shadow: 0 2px 8px rgba(0, 0, 0, 0.45);
		font-size: 11px;
		color: var(--rb-text);
		pointer-events: auto;
	}
	.load-confirm-q {
		white-space: nowrap;
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
