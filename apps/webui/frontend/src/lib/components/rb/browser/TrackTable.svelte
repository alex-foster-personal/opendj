<script module lang="ts">
	// Row + sort vocabulary now lives in the pane contract (browser-surface
	// unit); re-exported here so existing importers keep working.
	export type { BrowserRow, SortKey } from './pane-contract.svelte';
</script>

<script lang="ts">
	// Browser track table (SCREENSHOT-SPEC 5c). Columns in screenshot order:
	// funnel | cloud | # | Preview | Artwork | Track Title | Artist | K | B |
	// Rating | Comments | Time | Venue (quality badge) | Energy | Genre | Stems.
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
	import { tick, untrack, type Snippet } from 'svelte';
	import { clampToViewport } from '$lib/ui/clamp-to-viewport';
	import { artworkUrl, artworkStatusLabel, type Vocals } from '$lib/rb/api-rb';
	import { autoMusicalWidths, COL_DEFAULTS, compactMusicalWidths, compactUtilityWidths, type ColId } from '$lib/rb/library-column-widths';
	import {
		analysisIssuesFor,
		camelotKeyColor,
		camelotKeyHoverLabel,
		columnHeaderTitle,
		type LibraryColTipId,
		masterFoldCenterPx,
		highlightSpans,
		rowMatchesFind,
		buildCurveSegments,
		segmentPath,
		describeAutoPlayMode,
		installTrackDragGhost,
		removeTrackDragGhost,
		trackDragRefusal,
		bpmHeatColor,
		bpmHeatLabel,
		classifyBpmHeat,
		genreHoverColor,
		columnExplainer,
		type TrackEditModalKind
	} from './track-table-support';
	import { camelotKeysAreCompatible, DECK_IDS, deckStates } from '$lib/rb/audio-engine.svelte';
	import {
		previewCue,
		previewCueRatioFor,
		stopPreviewCue
	} from '$lib/player/preview-cue.svelte';
	import type { AnlzData } from '$lib/rb/anlz-types';
	import { autoPlayOrder } from '$lib/rb/auto-play.svelte';
	import { autoPlayQueue } from '$lib/rb/autoplay-queue.svelte';
	import { deckHoverUi } from '$lib/rb/deck-hover.svelte';
	import { setConfirmPref, uiPrefs } from '$lib/rb/prefs.svelte';
	import { quickDrawUi } from '$lib/rb/quick-draw-ui.svelte';
	import { beginTrackDrag, endTrackDrag, TRACK_STABLE_MIME } from '$lib/rb/track-drag.svelte';
	import type { BrowserRow, RowProvider, SortDir, SortKey } from './pane-contract.svelte';
	import AutoPlayExplainer from './AutoPlayExplainer.svelte';
	import AutoPlayRankCell from './AutoPlayRankCell.svelte';
	import AutoPlayWalkthrough from './AutoPlayWalkthrough.svelte';
	import LyricColumn from './LyricColumn.svelte';
	import PreviewStrip from './PreviewStrip.svelte';
	import QualityBadge from '../QualityBadge.svelte';
	import RatingStars from './RatingStars.svelte';
	import AnalysisDotsPopover from './AnalysisDotsPopover.svelte';
	import StemTags from './StemTags.svelte';
	import VocalAnalyzeButton from './VocalAnalyzeButton.svelte';
	import {
		computeVirtualWindow,
		createRowVisibilityObserver,
		TRACK_TABLE_THEAD_PX,
		masterFoldVisibility,
		scrollTopForRowIndex,
		scrollTopForDeckLayoutAnchor
	} from './virtual-window';
	import {
		ANALYSIS_COLORS,
		jobProgress,
		type AnalysisBadge,
		type AnalysisIssues
	} from '$lib/rb/job-progress.svelte';
	import { audioPrefetchStatus } from '$lib/rb/audio-prefetch-cache.svelte';
	import { performanceCommandStatus } from '$lib/rb/performance-ipc.svelte';
	import SpinnerIcon from './SpinnerIcon.svelte';
	import RelocatePopover from './RelocatePopover.svelte';
	import TrackContextMenu from './TrackContextMenu.svelte';
	import TrackPlaylistsPopover from './TrackPlaylistsPopover.svelte';
	import { trackCloudView } from './track-cloud-state';

	type DeckId = (typeof DECK_IDS)[number];

	const DECKS: DeckId[] = [1, 2, 3, 4];
	// Fixed row heights (virtualization window math requires constant height).
	// compact = current tight rows; cosy = taller + slightly roomier cell pad.
	const ROW_HEIGHT_COMPACT = 22;
	const ROW_HEIGHT_COSY = 30;
	const OVERSCAN = 10;

	// ----- AUTOPLAY-COL -----------------------------------------------------
	const AUTOPLAY_COL_COUNT = 18;
	let colWidths = $state<Record<ColId, number>>({ ...COL_DEFAULTS });
	const manuallyResizedColumns = new Set<ColId>();
	let resizeCol: ColId | null = null;
	let resizeStartX = 0;
	let resizeStartW = 0;
	let loadConfirm = $state<{
		row: BrowserRow;
		deck: DeckId;
		/** Set only when `deck` came from onpickdoubledeck (it reserved this
		 * deck, at this generation); null for the shift-with-no-picker
		 * fallback below, which never reserved anything. Passed straight
		 * through to onloadrow/onloadconfirmcancelled so a release matches
		 * the exact reservation this dialog owns - see BrowserPanel's
		 * _releaseDeckReservation for why a generation, not a boolean. */
		reservation: number | null;
		x: number;
		y: number;
	} | null>(null);
	let loadConfirmEl = $state<HTMLDivElement | null>(null);
	let loadConfirmStyle = $state('');
	let loadConfirmEveryTime = $state(false);

	$effect(() => {
		if (loadConfirm === null) return;
		void (async () => {
			await tick();
			if (loadConfirmEl === null || loadConfirm === null) return;
			const rect = loadConfirmEl.getBoundingClientRect();
			const box = clampToViewport(
				loadConfirm.x,
				loadConfirm.y,
				{ width: rect.width, height: rect.height },
				{ width: window.innerWidth, height: window.innerHeight }
			);
			loadConfirmStyle = `left:${Math.round(box.x)}px;top:${Math.round(box.y)}px`;
		})();
	});
	let trackContextMenu = $state<TrackContextMenu | null>(null);
	let playlistsMenu = $state<{ x: number; y: number; stableId: string } | null>(null);
	let relocateMenu = $state<{ x: number; y: number; stableId: string; title: string | null } | null>(
		null
	);

	function onColResizeStart(event: PointerEvent, col: ColId): void {
		event.preventDefault();
		event.stopPropagation();
		const handle = event.currentTarget as HTMLElement;
		handle.setPointerCapture(event.pointerId);
		resizeCol = col;
		manuallyResizedColumns.add(col);
		resizeStartX = event.clientX;
		resizeStartW = colWidths[col];
	}

	function onColResizeMove(event: PointerEvent): void {
		if (resizeCol === null) return;
		const minimum = Math.min(28, COL_DEFAULTS[resizeCol]);
		const next = Math.max(minimum, resizeStartW + (event.clientX - resizeStartX));
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
	const keyCompatRef = $derived(
		uiPrefs.next_only_filter && compatibleReferenceKey !== null
			? compatibleReferenceKey
			: masterKey
	);
	const masterKeyColor = $derived(camelotKeyColor(keyCompatRef));
	const masterBpm = $derived(masterDeck?.bpm ?? null);
	/** Header BPM color: heat vs itself = on-tempo white when a master exists. */
	const masterBpmColor = $derived(bpmHeatColor(masterBpm, masterBpm));
	const masterStableId = $derived(masterDeck?.stable_id ?? null);
	const hoverStableId = $derived(
		deckHoverUi.deckId === null ? null : (deckStates[deckHoverUi.deckId].stable_id ?? null)
	);

	function keyCompat(key: string | null): boolean {
		return camelotKeysAreCompatible(key, keyCompatRef);
	}

	function keyCompatStyle(key: string | null): string | undefined {
		const base = keyCellStyle(key);
		if (!keyCompat(key) || masterKeyColor === null) return base;
		const border = `box-shadow: inset 0 0 0 1px ${masterKeyColor}`;
		return base === undefined ? border : `${base};${border}`;
	}

	function keyCellInert(row: BrowserRow): boolean {
		return row.key_status === 'failed' || row.key_status === 'missing';
	}

	function keyCellTitle(row: BrowserRow): string {
		if (row.key_status === 'failed') {
			return row.key_reason ?? 'key analysis failed';
		}
		if (row.key_status === 'missing') {
			return row.key_reason ?? 'key not analyzed yet';
		}
		return `${camelotKeyHoverLabel(row.key) ?? 'Key not analyzed'} Dynamic key, musical mode, and chord progression analysis: not analyzed.`;
	}

	function bpmCellHeat(bpm: number | null) {
		return classifyBpmHeat(bpm, masterBpm);
	}

	function bpmCellStyle(bpm: number | null): string | undefined {
		const heat = bpmCellHeat(bpm);
		return heat === null ? undefined : `color:${heat.color}`;
	}

	function bpmCellInert(row: BrowserRow): boolean {
		return (
			row.bpm_status === 'failed' ||
			row.bpm_status === 'missing' ||
			row.bpm_status === 'available-not-selected'
		);
	}

	function bpmCellTitle(row: BrowserRow): string {
		if (row.bpm_status === 'failed') {
			return row.bpm_reason ?? 'bpm analysis failed';
		}
		if (row.bpm_status === 'missing') {
			return row.bpm_reason ?? 'bpm not analyzed yet';
		}
		if (row.bpm_status === 'available-not-selected') {
			return row.bpm_reason ?? 'beatgrid analysis available but not selected';
		}
		return `${bpmHeatLabel(bpmCellHeat(row.bpm), masterBpm) ?? 'BPM not analyzed'}${row.bpm === null ? '' : ` Exact BPM: ${row.bpm.toFixed(1)}.`} Dynamic tempo analysis: not analyzed.`;
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
		// CUEOUT-15: a deck always wins the playhead, because a deck can be on
		// air and the preview never is. Only a row on no deck shows a preview.
		return fallback ?? previewCueRatioFor(stableId);
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
		selectedOrders,
		loadedIds,
		vocalsById,
		markerAnlzById,
		sortKey,
		sortDir,
		emptyMessage,
		onemptyretry = undefined,
		filterBypassNote = null,
		restoreKey,
		scrollTop,
		removable = false,
		reorderable = false,
		onscrollcursor,
		onrenderedrowcapacity,
		onsort,
		onselectrow,
		onloadrow,
		onpickdoubledeck,
		onloadconfirmcancelled,
		onpreviewseek,
		onrate,
		onrefused,
		onrowvisible,
		onremoverow,
		onreorder,
		ongenrefilter,
		genreFilterUntil = 0,
		searchQuery = '',
		findQuery = '',
		/** Suggest-next hover: temporarily highlight + scroll to this row. */
		suggestHoverId = null as string | null,
		/** pin 02717d4ea496. Rendered INSIDE the table region, pinned just
		 * below the sticky column-header row, so a panel-owned status
		 * surface (the library load indicator) cannot push the headers down
		 * the page or sit above them. The table renders the snippet and
		 * knows nothing about what is in it, so this stays a pure row
		 * renderer; the one thing it contributes is the offset, which only
		 * it can measure. */
		bodyOverlay = undefined as Snippet | undefined,
		onstemsdonext = undefined as ((stableIds: string[]) => void) | undefined,
		onlyricsdonext = undefined as ((stableIds: string[]) => void) | undefined,
		onopeneditmodal = undefined,
		/** When next-only filter is on, highlight keys against this ref (issue #3983). */
		compatibleReferenceKey = null as string | null,
		onremovefromlibrary = undefined,
		onrelocated = undefined,
		onaddtoplaylist = undefined
	}: {
		/** Read contract: { rows, total, truncated, fetchWindow } - see
		 * pane-contract.svelte.ts. */
		provider: RowProvider;
		selectedIds: string[];
		selectedOrders: number[];
		loadedIds: Set<string>;
		/** Vocals ALREADY known client-side (loaded decks / anlz cache) -
		 * v1 scope: strips never fetch /anlz themselves (see BrowserPanel). */
		vocalsById: Record<string, Vocals>;
		/** Strip marker ANLZ ALREADY in memory (loaded decks / anlz cache),
		 * resolved by BrowserPanel (LIBUX-12); absent = markerless strip. */
		markerAnlzById: Record<string, AnlzData>;
		sortKey: SortKey | null;
		sortDir: SortDir;
		emptyMessage: string | null;
		/** Retry a failed whole-collection search from the empty state. */
		onemptyretry?: (() => void) | undefined;
		/** Honest note when a tiny search result bypasses the compatible filter. */
		filterBypassNote?: string | null;
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
		/** Reports the number of actual rows the viewport can show after its
		 * header and current density are accounted for. */
		onrenderedrowcapacity?: (count: number) => void;
		onsort: (key: SortKey) => void;
		onselectrow: (row: BrowserRow, event?: MouseEvent) => void;
		/** deck null = legacy free-deck load; prefer onpickdoubledeck for dblclick.
		 * `reservation` must be set only when `deck` came from onpickdoubledeck
		 * (it reserved that deck, at that generation) - never for an explicit
		 * "Load onto deck N" pick, which owns no reservation to release. */
		onloadrow: (
			row: BrowserRow,
			deck: DeckId | null,
			opts?: { play?: boolean; reservation?: number; pressT0Ms?: number }
		) => void;
		/**
		 * Preferred deck for double-click load+play. Shift -> CH3/CH4 when
		 * free/stopped. Cmd/Ctrl -> replace whatever deck the last plain
		 * double-click targeted, instead of advancing to the next deck.
		 * Returns the reservation's generation alongside the deck - see
		 * BrowserPanel's _releaseDeckReservation.
		 */
		onpickdoubledeck?: (
			row: BrowserRow,
			opts?: { shift?: boolean; replace?: boolean }
		) => { deck: DeckId; reservation: number } | null;
		/** The load+play confirm dialog was dismissed WITHOUT loading -
		 * onpickdoubledeck already reserved this deck, and no onloadrow call
		 * is coming to release it, so the caller must release it itself. */
		onloadconfirmcancelled?: (deck: DeckId, reservation: number) => void;
		/** Preview strip click: 0..1 ratio along the track. */
		onpreviewseek?: (row: BrowserRow, ratio: number) => void;
		onrate: (row: BrowserRow, next: number) => void;
		onrowvisible: (row: BrowserRow) => void;
		/** Remove this row's membership position from the playlist. */
		onremoverow?: (row: BrowserRow) => void;
		/** Move the track at `fromOrder` (1-based) to `toOrder`'s slot. */
		onreorder?: (fromOrder: number, toOrder: number, count?: number) => void;
		/** A drag the table refused, with the reason. The table does not own a
		 * toast channel, so the panel says it (pins 8ba0b15d975b /
		 * 72be3e505510: a silent refusal reads as a broken feature). */
		onrefused?: (reason: string) => void;
		onstemsdonext?: (stableIds: string[]) => void;
		onlyricsdonext?: (stableIds: string[]) => void;
		onopeneditmodal?: (kind: TrackEditModalKind) => void;
		/** Remove selected tracks from the library (files stay on disk). */
		onremovefromlibrary?: (stableIds: string[]) => void;
		/** FLOW-07: called after a relocate apply succeeds, so the pane can
		 * reload and pick up the row's fresh file_exists/broken state. */
		onrelocated?: (() => void) | undefined;
		/** Open the add-to-playlist picker for the selected tracks. */
		onaddtoplaylist?: (stableIds: string[]) => void;
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
		/** Panel-owned status surface, pinned below the column headers. */
		bodyOverlay?: Snippet;
	} = $props();

	/** Measured, not the hardcoded 22px .master-fold uses: the header row's
	 * height is density-dependent (`--tt-row-h`), so a constant would drift
	 * the overlay into or away from the headers on a density change. */
	let theadHeightPx = $state(0);

	const GENRE_CLICK_MS = 320;
	const LOAD_DBLCLICK_SEL = '.c-preview, .c-art, .c-title, .c-artist';
	let _genreClickTimer: ReturnType<typeof setTimeout> | null = null;
	let _rowGenreTimer: ReturnType<typeof setTimeout> | null = null;

	/** Issue #1558: must clear the platform double-click interval (~400ms)
	 * with margin, so a real double-click's second click always lands inside
	 * it, but a deliberate single click on a deck button - which arrives well
	 * after human reaction time - is unaffected. */
	const DBLCLICK_GUARD_MS = 500;
	/** Rows currently within their post-click guard window: the quick-load
	 * box's buttons stay pointer-events: none for these ids even while the
	 * row is selected and hovered (see .dblclick-guard-active below). A JS
	 * timer, not a CSS transition-delay: pointer-events is a discrete
	 * property, so a browser only honors transition-delay on it with
	 * `transition-behavior: allow-discrete` (Chrome 117+/Safari 17.4+) -
	 * verified empirically on PR #1570 (Codex review) - and this app's
	 * packaged WKWebView targets macOS 11, whose system WebKit predates that
	 * entirely, so the CSS-only guard silently did nothing there. A plain
	 * class-gated selector has no such floor. */
	let dblclickGuardRowIds = $state(new Set<string>());
	const _dblclickGuardTimers = new Map<string, ReturnType<typeof setTimeout>>();

	function _armDblclickGuard(rowId: string): void {
		const existing = _dblclickGuardTimers.get(rowId);
		if (existing !== undefined) clearTimeout(existing);
		dblclickGuardRowIds = new Set(dblclickGuardRowIds).add(rowId);
		_dblclickGuardTimers.set(
			rowId,
			setTimeout(() => {
				_dblclickGuardTimers.delete(rowId);
				const next = new Set(dblclickGuardRowIds);
				next.delete(rowId);
				dblclickGuardRowIds = next;
			}, DBLCLICK_GUARD_MS)
		);
	}

	/** Pin 27f889893790's hide corridor: keep deck buttons hittable for
	 * 100ms after the pointer leaves .c-art / .c-title, so travelling the
	 * gap over the hanging box's dead area can still land on a button.
	 * Same JS-timer + class shape as `_armDblclickGuard` - a CSS
	 * `transition-delay` on `pointer-events` is discrete and only runs with
	 * `transition-behavior: allow-discrete` (Chrome 117+/Safari 17.4+),
	 * which this app's packaged WKWebView floor (macOS 11) does not have.
	 * That is why the #1558 reveal guard was ported off CSS on PR #1570;
	 * the hide twin was the same inert rule. Issue #1588. */
	const CORRIDOR_GRACE_MS = 100;
	let corridorGraceRowIds = $state(new Set<string>());
	const _corridorGraceTimers = new Map<string, ReturnType<typeof setTimeout>>();

	function _armCorridorGrace(rowId: string): void {
		const existing = _corridorGraceTimers.get(rowId);
		if (existing !== undefined) clearTimeout(existing);
		corridorGraceRowIds = new Set(corridorGraceRowIds).add(rowId);
		_corridorGraceTimers.set(
			rowId,
			setTimeout(() => {
				_corridorGraceTimers.delete(rowId);
				const next = new Set(corridorGraceRowIds);
				next.delete(rowId);
				corridorGraceRowIds = next;
			}, CORRIDOR_GRACE_MS)
		);
	}

	function _onDeckTriggerPointerLeave(event: PointerEvent, row: BrowserRow): void {
		const related = event.relatedTarget;
		const trigger = event.currentTarget;
		if (related instanceof Node && trigger instanceof HTMLElement) {
			const triggerRow = trigger.closest('tr');
			if (triggerRow !== null) {
				const art = triggerRow.querySelector('.c-art');
				const title = triggerRow.querySelector('.c-title');
				if (
					(art !== null && art.contains(related)) ||
					(title !== null && title.contains(related))
				) {
					return;
				}
			}
		}
		if (!selectedOrderSet.has(row.order)) return;
		if (dblclickGuardRowIds.has(row.stable_id)) return;
		_armCorridorGrace(row.stable_id);
	}

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
		// Any click on the row is where its FIRST double-click click would
		// land, whether the row was already selected or is only selecting
		// now - both cases must stay guarded (issue #1558).
		_armDblclickGuard(row.stable_id);
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
		const picked = onpickdoubledeck?.(row, {
			shift: event.shiftKey,
			replace: event.metaKey || event.ctrlKey
		});
		const deck = picked?.deck ?? (event.shiftKey ? null : 1);
		if (deck === null) return;
		// onpickdoubledeck reserved this deck (and returned its generation);
		// the shift-with-no-picker fallback above never did, so it owns
		// nothing to release later.
		const reservation = picked != null ? picked.reservation : null;
		// Missing key = ask; false = skip (remembered "do this every time").
		if (uiPrefs.confirm.dblclick_load_play === false) {
			onloadrow(
				row,
				deck,
				reservation !== null
					? { play: true, reservation, pressT0Ms: event.timeStamp }
					: { play: true, pressT0Ms: event.timeStamp }
			);
			return;
		}
		// A second double-click before the first confirm is answered
		// overwrites `loadConfirm` below - the No button's release only fires
		// for the VISIBLE dialog, so the reservation this is about to
		// discard would otherwise stay pending forever.
		if (loadConfirm !== null && loadConfirm.reservation !== null) {
			onloadconfirmcancelled?.(loadConfirm.deck, loadConfirm.reservation);
		}
		loadConfirmEveryTime = false;
		loadConfirm = {
			row,
			deck,
			reservation,
			x: event.clientX,
			y: Math.max(8, event.clientY - 20)
		};
	}

	function hl(text: string | null): Array<{ text: string; hit: boolean }> {
		return highlightSpans(text ?? '', findQuery);
	}

	const rows = $derived(provider.rows);
	const maxRowOrder = $derived(rows.reduce((max, row) => Math.max(max, row.order), 0));
	$effect(() => {
		const compact = compactUtilityWidths(maxRowOrder);
		const musical = compactMusicalWidths(rows);
		void wrapWidth;
		// Manual K/B drags stay authoritative; other utility widths reflow with data.
		const current = untrack(() => colWidths);
		colWidths = {
			...current,
			...compact,
			...autoMusicalWidths(current, musical, manuallyResizedColumns)
		};
	});
	const selectedOrderSet = $derived(new Set(selectedOrders));
	const rowHeight = $derived(
		uiPrefs.library_density === 'cosy' ? ROW_HEIGHT_COSY : ROW_HEIGHT_COMPACT
	);

	function onTrackKeydown(event: KeyboardEvent, row: BrowserRow): void {
		const target = event.target;
		if (
			target instanceof HTMLInputElement ||
			target instanceof HTMLTextAreaElement ||
			(target instanceof HTMLElement && target.isContentEditable)
		) {
			return;
		}
		if (
			removable &&
			(event.key === 'Delete' || event.key === 'Backspace') &&
			onremoverow
		) {
			event.preventDefault();
			event.stopPropagation();
			if (selectedOrderSet.has(row.order)) {
				for (const visible of rows) {
					if (selectedOrderSet.has(visible.order)) {
						onremoverow(visible);
					}
				}
			} else {
				onremoverow(row);
			}
			return;
		}
		if (event.key !== 'ContextMenu' && !(event.shiftKey && event.key === 'F10')) return;
		trackContextMenu?.openFromKeyboard(event, row);
	}

	// ----- AUTOPLAY-COL helpers ---------------------------------------------
	let hoveredApId = $state<string | null>(null);
	const autoPlayMode = $derived(describeAutoPlayMode(uiPrefs).mode);
	const showLyricsCol = $derived(uiPrefs.lyrics_library_col && uiPrefs.lyrics_global);
	const colCount = $derived(
		(autoPlayMode === 'off' ? AUTOPLAY_COL_COUNT - 1 : AUTOPLAY_COL_COUNT) + (showLyricsCol ? 1 : 0)
	);

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
			scrollTop: liveScrollTop - TRACK_TABLE_THEAD_PX,
			viewportHeight,
			pad: rowHeight * 2
		});
	});

	const apCurveX = $derived(Math.max(8, colWidths.autoplay / 2));
	const apCurveArrowId = $derived(`ap-curve-arrow-${restoreKey}`);

	/** r3919185343: `table-layout: fixed` at `width: 100%` redistributes any
	 * extra space beyond the configured column total across the columns on a
	 * wide wrap, so the rendered title column drifts right of where
	 * masterFoldCenterPx (computed from the raw colWidths) puts the badge.
	 * Pinning the table to exactly this sum removes the extra space there is
	 * to redistribute - narrower than the wrap just leaves blank space to the
	 * right, same as any wrap wider than its content. */
	const tableWidthPx = $derived(
		Object.entries(colWidths).reduce(
			(sum, [id, w]) => sum + (id === 'lyrics' && !showLyricsCol ? 0 : w),
			0
		)
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
	/* Only the master-jump badge needs this, and the wrap's existing onscroll
	 * already has the value in hand - so tracking it costs one assignment, not
	 * a listener (pin b44c957f082f). */
	let liveScrollLeft = $state(0);
	let viewportHeight = $state(0);
	/** Deck layout toggle anchor: preserve selected row across viewport resize. */
	let deckLayoutAnchor: {
		rowIndex: number;
		priorScrollTop: number;
		priorViewportHeight: number;
	} | null = $state(null);
	let lastDeckLayout = uiPrefs.deck_layout;

	function applyDeckLayoutAnchorScroll(): void {
		const el = wrapEl;
		const anchor = deckLayoutAnchor;
		if (el === null || anchor === null || viewportHeight <= 0) return;
		const next = scrollTopForDeckLayoutAnchor({
			rowIndex: anchor.rowIndex,
			rowHeight,
			headerOffsetPx: TRACK_TABLE_THEAD_PX,
			viewportHeight,
			priorScrollTop: anchor.priorScrollTop,
			priorViewportHeight: anchor.priorViewportHeight
		});
		el.scrollTop = next;
		liveScrollTop = next;
		onscrollcursor?.(next);
		deckLayoutAnchor = null;
	}
	/** Wrap's own rendered width, for the master-fold badge's right-edge
	 * clamp - same ResizeObserver as viewportHeight, so this costs nothing
	 * extra (pin b44c957f082f follow-up). */
	let wrapWidth = $state(0);
	/** The master-jump badge's own rendered width, so its clamp can account
	 * for `translateX(-50%)` pushing its edge half a badge-width past the
	 * clamped centre (Sol review r3941617671). `bind:clientWidth` below is a
	 * one-off read of an element whose size only changes with its own text/
	 * font, never with a window resize, so this is not the listener the
	 * pin's "lazy on resize" requirement was written against. */
	let masterFoldBadgeWidth = $state(0);

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
		const layout = uiPrefs.deck_layout;
		if (layout !== lastDeckLayout) {
			const ids = untrack(() => selectedIds);
			const map = untrack(() => rowIndexOf);
			const anchorId = ids.length > 0 ? ids[ids.length - 1] : null;
			const rowIndex = anchorId === null ? -1 : (map.get(anchorId) ?? -1);
			if (rowIndex >= 0) {
				deckLayoutAnchor = {
					rowIndex,
					priorScrollTop: untrack(() => liveScrollTop),
					priorViewportHeight: viewportHeight
				};
			}
			lastDeckLayout = layout;
		}
	});

	$effect(() => {
		if (deckLayoutAnchor === null) return;
		const duration = uiPrefs.deck_layout_animate ? uiPrefs.deck_layout_duration_ms : 0;
		const timer = setTimeout(() => applyDeckLayoutAnchorScroll(), duration + 32);
		return () => clearTimeout(timer);
	});

	$effect(() => {
		const el = wrapEl;
		if (el === null) return;
		viewportHeight = el.clientHeight;
		wrapWidth = el.clientWidth;
		if (typeof ResizeObserver === 'undefined') return; // SSR guard
		const ro = new ResizeObserver((entries) => {
			for (const entry of entries) {
				viewportHeight = entry.contentRect.height;
				wrapWidth = entry.contentRect.width;
				const anchor = deckLayoutAnchor;
				if (
					anchor !== null &&
					entry.contentRect.height > 0 &&
					entry.contentRect.height !== anchor.priorViewportHeight
				) {
					applyDeckLayoutAnchorScroll();
				}
			}
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
			overscan: OVERSCAN,
			headerOffsetPx: TRACK_TABLE_THEAD_PX
		})
	);
	const visibleRows = $derived(rows.slice(windowInfo.startIndex, windowInfo.endIndex));
	const renderedRowCapacity = $derived(
		Math.min(
			rows.length,
			Math.max(0, Math.floor((viewportHeight - TRACK_TABLE_THEAD_PX) / rowHeight))
		)
	);

	$effect(() => {
		onrenderedrowcapacity?.(renderedRowCapacity);
	});

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
		const top = scrollTopForRowIndex({
			rowIndex: idx,
			rowHeight: rh,
			headerOffsetPx: TRACK_TABLE_THEAD_PX,
			offsetFromTopPx: Math.floor(vh / 3)
		});
		el.scrollTop = top;
		liveScrollTop = top;
		onscrollcursor(top);
	});

	/** Master track fold cue: above / below viewport (null = on-screen or absent). */
	const masterIndex = $derived(
		masterStableId === null ? -1 : rows.findIndex((r) => r.stable_id === masterStableId)
	);
	const masterOrder = $derived(masterIndex < 0 ? null : (rows[masterIndex]?.order ?? null));
	const masterFold = $derived.by((): 'above' | 'below' | null =>
		masterFoldVisibility({
			rowIndex: masterIndex,
			rowHeight,
			scrollTop: liveScrollTop,
			viewportHeight,
			headerOffsetPx: TRACK_TABLE_THEAD_PX
		})
	);

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
		const target = scrollTopForRowIndex({
			rowIndex: idx,
			rowHeight: rh,
			headerOffsetPx: TRACK_TABLE_THEAD_PX,
			offsetFromTopPx: Math.floor(vh / 3)
		});
		el.scrollTop = target;
		liveScrollTop = target;
	});

	function jumpToMaster(): void {
		if (wrapEl === null || masterIndex < 0) return;
		const target = scrollTopForRowIndex({
			rowIndex: masterIndex,
			rowHeight,
			headerOffsetPx: TRACK_TABLE_THEAD_PX,
			offsetFromTopPx: viewportHeight * 0.35
		});
		wrapEl.scrollTop = target;
		liveScrollTop = target;
		onscrollcursor(target);
	}

	// ------------------------------------------- lazy-hydration observer
	const _rowVisibility = createRowVisibilityObserver<BrowserRow>({
		onRowVisible: (row) => onrowvisible(row)
	});
	const observeRow = _rowVisibility.observeRow;

	$effect(() => {
		return () => {
			_rowVisibility.disconnect();
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

	function orderCellTitle(order: number): string {
		return `Playlist position ${order}`;
	}

	function timeCellTitle(ms: number | null): string {
		if (ms === null) return 'Duration unknown';
		return `Duration ${_fmtTime(ms)} (mm:ss)`;
	}

	function _fmtBpm(bpm: number | null): string {
		// Screenshot truncates to '132.' - one decimal is the sanctioned improvement.
		return bpm === null ? '' : String(Math.round(bpm));
	}

	function _hideBrokenImg(event: Event): void {
		(event.currentTarget as HTMLImageElement).style.display = 'none';
	}

	// ----------------------------------------- drag-to-reorder (native DnD)
	// Grip-initiated only (not the whole row): the row's own click/dblclick
	// keep selecting/loading a deck. _dragSourceOrder is plain state, not a
	// rune - it only matters for the lifetime of one drag gesture.
	let _dragSourceOrder: { start: number; count: number } | null = null;

	function onRowDragStart(event: DragEvent, row: BrowserRow): void {
		// A refused drag used to just preventDefault and return: no cursor
		// change, no message, nothing - indistinguishable from drag-to-deck
		// being broken, which is how it was reported. Same reasons, same
		// wording as the double-click path (pins 8ba0b15d975b, 72be3e505510).
		const refusal = trackDragRefusal({
			file_exists: row.file_exists,
			file_availability: row.file_availability,
			// All Tracks rows start row.is_streaming at null and hydrate the
			// real value into row.rb_meta later - same effective flag
			// _loadOntoDeck already checks, so the two refusal paths agree.
			is_streaming: row.is_streaming ?? row.rb_meta?.is_streaming ?? false
		});
		if (refusal !== null) {
			event.preventDefault();
			onrefused?.(refusal);
			return;
		}
		const ids =
			selectedOrderSet.has(row.order) && selectedIds.length > 1
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
		beginTrackDrag(ids, {
			[row.stable_id]: {
				file_exists: row.file_exists,
				is_streaming: row.is_streaming ?? row.rb_meta?.is_streaming ?? false
			}
		});
	}

	function onRowDragEnd(): void {
		removeTrackDragGhost();
		endTrackDrag();
	}

	function onGripDragStart(event: DragEvent, row: BrowserRow): void {
		event.stopPropagation();
		const selected = [...new Set(selectedOrders)].sort((a, b) => a - b);
		if (
			selectedOrderSet.has(row.order) &&
			selected.length > 0 &&
			selected[selected.length - 1] - selected[0] + 1 === selected.length &&
			selected.includes(row.order)
		) {
			_dragSourceOrder = { start: selected[0], count: selected.length };
		} else {
			_dragSourceOrder = { start: row.order, count: 1 };
		}
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
		const source = _dragSourceOrder;
		_dragSourceOrder = null;
		if (source === null) return;
		const { start, count } = source;
		if (row.order >= start && row.order < start + count) return;
		if (start === row.order) return;
		onreorder?.(start, row.order, count);
	}
</script>

{#snippet sortableTh(key: SortKey, label: string, col: ColId & LibraryColTipId)}
	<!-- svelte-ignore a11y_click_events_have_key_events -->
	<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
	<th
		class={`h-${key} sortable`}
		style={`width:${colWidths[col]}px`}
		use:columnExplainer={{ text: columnHeaderTitle(col, `Sort by ${label} (asc → desc → clear)`) }}
		onclick={(e) => {
			if ((e.target as HTMLElement).closest('.col-resize')) return;
			onsort(key);
		}}
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

<div
	class="tt-root"
	data-density={uiPrefs.library_density}
	data-truncated={provider.truncated ? 'true' : 'false'}
>
	<TrackContextMenu
		bind:this={trackContextMenu}
		{selectedIds}
		{selectedOrders}
		{onselectrow}
		{onloadrow}
		{onaddtoplaylist}
		{onstemsdonext}
		{onlyricsdonext}
		{onopeneditmodal}
		{onremovefromlibrary}
		{onremoverow}
		{removable}
		onshowinplaylists={(row, x, y) => {
			playlistsMenu = { x, y, stableId: row.stable_id };
		}}
		onrelocate={(row, x, y) => {
			relocateMenu = { x, y, stableId: row.stable_id, title: row.title };
		}}
	/>
	{#if playlistsMenu !== null}
		<TrackPlaylistsPopover
			stableId={playlistsMenu.stableId}
			x={playlistsMenu.x}
			y={playlistsMenu.y}
			onclose={() => (playlistsMenu = null)}
		/>
	{/if}
	{#if relocateMenu !== null}
		<RelocatePopover
			stableId={relocateMenu.stableId}
			trackTitle={relocateMenu.title}
			x={relocateMenu.x}
			y={relocateMenu.y}
			onclose={() => (relocateMenu = null)}
			onrelocated={() => onrelocated?.()}
		/>
	{/if}
	{#if masterFold === 'above'}
		<button
			type="button"
			class="master-fold above"
			style={`left:${masterFoldCenterPx(colWidths, liveScrollLeft, wrapWidth, masterFoldBadgeWidth / 2)}px`}
			onclick={jumpToMaster}
			title="Master track is above - click to jump"
			bind:clientWidth={masterFoldBadgeWidth}
		>
			▲ MASTER
		</button>
	{/if}
	{#if masterFold === 'below'}
		<button
			type="button"
			class="master-fold below"
			style={`left:${masterFoldCenterPx(colWidths, liveScrollLeft, wrapWidth, masterFoldBadgeWidth / 2)}px`}
			onclick={jumpToMaster}
			title="Master track is below - click to jump"
			bind:clientWidth={masterFoldBadgeWidth}
		>
			▼ MASTER
		</button>
	{/if}
	{#if bodyOverlay !== undefined}
		<!-- `bind:clientHeight` only lands after the first resize callback, so on
		     the very first paint theadHeightPx is still 0 and this would sit ON
		     the sticky header instead of below it (blinded review, PR #1672).
		     A pane that is already loading at mount is exactly when that
		     happens. Unmeasured means invisible, not misplaced. -->
		<div
			class="tt-body-overlay"
			class:measured={theadHeightPx > 0}
			style={`top:${theadHeightPx}px`}
		>
			{@render bodyOverlay()}
		</div>
	{/if}
	<div
		class="table-wrap"
		bind:this={wrapEl}
		onscroll={(e) => {
			const top = e.currentTarget.scrollTop;
			liveScrollTop = top;
			liveScrollLeft = e.currentTarget.scrollLeft;
			onscrollcursor(top);
		}}
	>
		<table data-testid="track-table" style={`width:${tableWidthPx}px`}>
			<colgroup>
				<col style={`width:${colWidths.funnel}px`} />
				<col style={`width:${colWidths.err}px`} />
				<col style={`width:${colWidths.cloud}px`} />
				<col style={`width:${colWidths.order}px`} />
				{#if autoPlayMode !== 'off'}<col style={`width:${colWidths.autoplay}px`} />{/if}
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
				<col style={`width:${colWidths.energy}px`} />
				<col style={`width:${colWidths.genre}px`} />
				<col style={`width:${colWidths.stems}px`} />
				{#if showLyricsCol}
					<col style={`width:${colWidths.lyrics}px`} />
				{/if}
			</colgroup>
			<thead bind:clientHeight={theadHeightPx}>
				<tr>
					<th
						class="h-icon"
						style={`width:${colWidths.funnel}px`}
						use:columnExplainer={{ text: 'filter - not implemented, see PARITY-TODO' }}
					>
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
						use:columnExplainer={{ text: 'Err - detected analysis data-quality issues, hover a square for detail' }}
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
					<th
						class="h-icon"
						style={`width:${colWidths.cloud}px`}
						use:columnExplainer={{
							text: 'CloudSync audio - crossed cloud: not on cloud; red cloud: on cloud, not local; plain cloud: on cloud and local. Hover a row icon for detail.'
						}}
					>
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
					{#if autoPlayMode !== 'off'}
						<th
							class="h-icon h-autoplay"
							style={`width:${colWidths.autoplay}px`}
							title={autoPlayQueue.active
								? `AutoPlay queue - ${autoPlayQueue.entries.length} planned handoff${autoPlayQueue.entries.length === 1 ? '' : 's'}`
								: 'AutoPlay queue - starts when AutoPlay is enabled'}
							data-autoplay-queue-active={autoPlayQueue.active}
						>
							<AutoPlayExplainer queue={autoPlayQueue.entries}>
								{#snippet demo()}
									{#if autoPlayMode === 'greedy' || autoPlayMode === 'reach' || autoPlayMode === 'enforce'}<AutoPlayWalkthrough mode={autoPlayMode} />{/if}
								{/snippet}
								<button
									class="autoplay-sort"
									aria-pressed={sortKey === 'autoplay'}
									aria-label={sortKey === 'autoplay' ? 'AutoPlay order, sorted 1 to n - activate to restore pane order' : 'AutoPlay order - activate to sort 1 to n'}
									onclick={() => onsort('autoplay')}
								>
									<svg
										class="autoplay-icon"
										viewBox="0 0 16 16"
										width="12"
										height="12"
										aria-hidden="true"
									>
										<path
											d="M8 1.5v2M5.5 2.5h5M4 5.5h8v6H4zM6.25 8h.01M9.75 8h.01M6.25 10h3.5M2.5 7.5H4M12 7.5h1.5"
											fill="none"
											stroke="currentColor"
											stroke-width="1.25"
											stroke-linecap="round"
											stroke-linejoin="round"
										/>
									</svg>
								</button>
							</AutoPlayExplainer>
						</th>
					{/if}
					<th
						class="h-preview"
						style={`width:${colWidths.preview}px`}
						use:columnExplainer={{ text: columnHeaderTitle('preview') }}
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
						class="h-icon h-art"
						style={`width:${colWidths.art}px`}
						use:columnExplainer={{ text: columnHeaderTitle('art') }}
					>
						<svg viewBox="0 0 16 16" width="11" height="11" aria-hidden="true">
							<path d="M1 3h14v10H1zm1 9 3.4-3.9 2.3 2.6 2.1-2.3L14 12V4H2z" fill="currentColor" />
							<circle cx="5" cy="6" r="1" fill="currentColor" />
						</svg>
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
						use:columnExplainer={{
							text: columnHeaderTitle(
								'key',
								masterKey !== null
									? `Master key ${masterKey} - sort by key (asc → desc → clear)`
									: 'Sort by key (asc → desc → clear)'
							)
						}}
						onclick={(e) => {
							if ((e.target as HTMLElement).closest('.col-resize')) return;
							onsort('key');
						}}
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
						use:columnExplainer={{
							text: columnHeaderTitle(
								'bpm',
								masterBpm !== null
									? `Master BPM ${_fmtBpm(masterBpm)} - sort by BPM (asc → desc → clear)`
									: 'Sort by BPM (asc → desc → clear)'
							)
						}}
						onclick={(e) => {
							if ((e.target as HTMLElement).closest('.col-resize')) return;
							onsort('bpm');
						}}
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
						use:columnExplainer={{ text: "QLT - audio quality as the biggest venue this file survives, from effective bitrate (size over duration) and container. The badge shows the rung's initial and its colour; hover a badge for the full rung name, the kbps and the container" }}
					>
						<span class="th-label"><span>QLT</span></span>
						<!-- svelte-ignore a11y_no_static_element_interactions -->
						<span
							class="col-resize"
							onpointerdown={(e) => onColResizeStart(e, 'quality')}
							onpointermove={onColResizeMove}
							onpointerup={onColResizeEnd}
							onpointercancel={onColResizeEnd}
						></span>
					</th>
					<!-- svelte-ignore a11y_click_events_have_key_events -->
					<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
					<th
						class="h-energy"
						class:h-icon={true}
						class:sortable={true}
						style={`width:${colWidths.energy}px`}
						onclick={(e) => {
							if ((e.target as HTMLElement).closest('.col-resize')) return;
							onsort('energy');
						}}
						title="Energy 1-9, from Mixed In Key - sort ascending, descending, then clear"
						aria-label="Energy 1-9, from Mixed In Key"
					>
						<span class="th-label"><svg class="energy-icon" aria-hidden="true" viewBox="0 0 24 24"><path d="M13 2 3 14h7l-1 8 10-12h-7z" /></svg><span class="energy-glyph" aria-hidden="true">⚡</span>{#if sortKey === 'energy'}<span class="arrow">{sortDir === 1 ? '▲' : '▼'}</span>{/if}</span>
						<!-- svelte-ignore a11y_no_static_element_interactions -->
						<span
							class="col-resize"
							onpointerdown={(e) => onColResizeStart(e, 'energy')}
							onpointermove={onColResizeMove}
							onpointerup={onColResizeEnd}
							onpointercancel={onColResizeEnd}
						></span>
					</th>
					{@render sortableTh('genre', 'Genre', 'genre')}
					<th
						class="h-stems"
						style={`width:${colWidths.stems}px`}
						use:columnExplainer={{ text: 'Stems - [V] vocals, [I] instruments (bass+other), [D] drums. Hover for model, overlap, format, sizes' }}
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
					{#if showLyricsCol}
						{@render sortableTh('lyrics', 'Lyrics', 'lyrics')}
					{/if}
				</tr>
			</thead>
			<tbody>
				{#if windowInfo.topPad > 0}
					<tr class="tt-spacer" style={`height:${windowInfo.topPad}px`} aria-hidden="true">
						<td colspan={colCount}></td>
					</tr>
				{/if}
				{#each visibleRows as row, i (`${row.stable_id}:${row.order}`)}
					{@const cloudView = trackCloudView({
						fileExists: row.file_exists === true,
						isStreaming: row.is_streaming ?? row.rb_meta?.is_streaming ?? false,
						hasRemoteCopy: row.has_remote_copy === true,
						transfer:
							row.cloud_transfer === null || row.cloud_transfer === undefined
								? null
								: {
										direction: row.cloud_transfer.direction,
										bytesTransferred: row.cloud_transfer.bytes_transferred,
										bytesTotal: row.cloud_transfer.bytes_total
									}
					})}
					<!-- key includes order: playlists CAN repeat a track -->
					<!-- svelte-ignore a11y_click_events_have_key_events -->
					<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
					<!-- Svelte reuses this keyed node across pane switches; observeRow.update() rebinds the WeakMap + observer. Pane identity stays out of the each-key so rows are not remounted and focus is not dropped. -->
					<tr
						use:observeRow={row}
						data-testid="track-row"
						data-stable-id={row.stable_id}
						tabindex="0"
						draggable="true"
						class:rb-row-selected={selectedOrderSet.has(row.order)}
						class:rb-row-first={windowInfo.topPad === 0 && i === 0}
						class:dblclick-guard-active={dblclickGuardRowIds.has(row.stable_id)}
						class:corridor-grace-active={corridorGraceRowIds.has(row.stable_id)}
						class:rb-row-menu={quickDrawUi.menuHighlightStableId === row.stable_id}
						class:rb-row-key-compat={keyCompat(row.key)}
						class:rb-row-spotify-pending={row.spotify_pending === true ||
							row.stable_id.startsWith('spotify-pending:')}
						class:loaded={loadedIds.has(row.stable_id)}
						class:rb-row-master={masterOrder !== null && row.order === masterOrder}
						class:rb-row-deck-hover={hoverStableId !== null &&
							row.stable_id === hoverStableId &&
							row.stable_id !== masterStableId}
						class:rb-row-suggest-hover={suggestHoverId !== null &&
							row.stable_id === suggestHoverId}
						class:rb-row-find={findQuery !== '' && rowMatchesFind(row, findQuery)}
						class:broken={row.file_exists === false &&
							row.file_availability !== 'AVAILABILITY_PENDING' &&
							!(row.is_streaming ?? row.rb_meta?.is_streaming) &&
							row.is_remote !== true &&
							row.spotify_pending !== true &&
							!row.stable_id.startsWith('spotify-pending:')}
						class:rb-row-availability-pending={row.file_availability ===
							'AVAILABILITY_PENDING'}
						title={row.file_availability === 'AVAILABILITY_PENDING'
							? 'availability still checking (wait for disk probe)'
							: undefined}
						class:rb-row-job={jobProgress.activeFor(row.stable_id) !== null}
						style={_jobRowStyle(row.stable_id)}
						onclick={(event) => onRowPointer(event, row)}
						ondblclick={(e) => onRowDblClick(e, row)}
						oncontextmenu={(e) => trackContextMenu?.open(e, row)}
						onkeydown={(e) => onTrackKeydown(e, row)}
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
							<AnalysisDotsPopover badge={_badgeFor(row)} stableId={row.stable_id} />
						</td>
						<td class="c-err">
							<AnalysisDotsPopover issues={_issuesFor(row)} mode="issues" stableId={row.stable_id} />
						</td>
						<!-- CloudSync presence, local availability, and transfer bytes are
						     separate backend facts; this cell never guesses a percentage. -->
						<td class="c-cloud">
							{#if cloudView.showIcon}
								<span class="cloud-state-wrap" data-cloud-state={cloudView.kind}>
									{#if cloudView.kind === 'streaming'}
										<span class="cloud" title={cloudView.title} aria-label={cloudView.title}>
											<svg viewBox="0 0 16 16" width="11" height="11" aria-hidden="true">
												<path
													d="M4.5 12a3 3 0 0 1-.4-5.97A4 4 0 0 1 12 6.5 2.75 2.75 0 0 1 11.5 12z"
													fill="currentColor"
												/>
											</svg>
										</span>
									{:else}
										<span
											class="cloud-copy"
											class:not-on-cloud={cloudView.kind === 'not-on-cloud'}
											class:on-cloud-not-local={cloudView.kind === 'on-cloud-not-local'}
											class:on-cloud-and-local={cloudView.kind === 'on-cloud-and-local'}
											title={cloudView.title}
											aria-label={cloudView.title}
										>
											<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
												<path
													d="M4.5 12a3 3 0 0 1-.4-5.97A4 4 0 0 1 12 6.5 2.75 2.75 0 0 1 11.5 12z"
													fill={cloudView.kind === 'not-on-cloud' ? 'none' : 'currentColor'}
													stroke="currentColor"
													stroke-width="1.25"
												/>
												{#if cloudView.kind === 'not-on-cloud'}
													<path
														d="M3 13 13 3"
														fill="none"
														stroke="currentColor"
														stroke-width="1.5"
														stroke-linecap="round"
													/>
												{/if}
											</svg>
										</span>
									{/if}
									{#if cloudView.transfer !== null}
										<span
											class="cloud-transfer-track"
											role="progressbar"
											aria-label={`CloudSync ${cloudView.transfer.direction}`}
											aria-valuemin="0"
											aria-valuemax="100"
											aria-valuenow={cloudView.transfer.percent ?? undefined}
											aria-valuetext={cloudView.transfer.label}
											title={cloudView.transfer.label}
										>
											<span
												class="cloud-transfer-indicator"
												class:indeterminate={cloudView.transfer.percent === null}
												style={cloudView.transfer.percent === null
													? undefined
													: `width:${cloudView.transfer.percent}%`}
											></span>
										</span>
									{/if}
								</span>
							{/if}
						</td>
						<td class="c-order" title={orderCellTitle(row.order)}>
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
						{#if autoPlayMode !== 'off'}
							<td class="c-autoplay">
								{#if _autoPlayRank(row.stable_id) !== null}
									{@const rank = _autoPlayRank(row.stable_id)!}
									<!-- svelte-ignore a11y_no_static_element_interactions -->
									<span
										title={`AutoPlay queue position ${rank}: hand off after ${rank - 1} more, from the current AutoPlay view`}
										onpointerenter={() => (hoveredApId = row.stable_id)}
										onpointerleave={() => {
											if (hoveredApId === row.stable_id) hoveredApId = null;
										}}
										onfocus={() => (hoveredApId = row.stable_id)}
										onblur={() => {
											if (hoveredApId === row.stable_id) hoveredApId = null;
										}}
									>
										<AutoPlayRankCell
											stableId={row.stable_id}
											{rank}
											isHot={hoveredApId === row.stable_id}
											onHover={(id) => {
												if (id === null) {
													if (hoveredApId === row.stable_id) hoveredApId = null;
												} else {
													hoveredApId = id;
												}
											}}
										/>
									</span>
								{/if}
							</td>
						{/if}
						<td class="c-preview">
							<PreviewStrip
								strip={row.strip}
								vocals={vocalsById[row.stable_id] ?? null}
								markerAnlz={markerAnlzById[row.stable_id] ?? null}
								duration_ms={row.duration_ms}
								revealed={row.revealed}
								nowRatio={_nowRatioFor(row.stable_id)}
								previewing={previewCue.stable_id === row.stable_id}
								onstop={stopPreviewCue}
								stable_id={row.stable_id}
								enabled={row.lyrics?.has_words === true}
								onseek={(ratio) => onpreviewseek?.(row, ratio)}
							/>
						</td>
						<td
							class="c-art"
							title={row.artwork_available !== true
								? row.artwork_available === null
									? 'artwork could not be checked (tag reader not installed in this build)'
									: (artworkStatusLabel(row.artwork_status) ??
										'artwork unavailable')
								: undefined}
							onpointerleave={(e) => _onDeckTriggerPointerLeave(e, row)}
						>
							<span class="art-slate" aria-hidden="true"></span>
							{#if row.artwork_available === true}
								<img
									src={artworkUrl(row.stable_id, 's')}
									alt=""
									loading="lazy"
									onerror={_hideBrokenImg}
								/>
							{/if}
						</td>
						<td class="c-title" class:rb-row-loaded={loadedIds.has(row.stable_id)} title={row.title ?? ''} onpointerleave={(e) => _onDeckTriggerPointerLeave(e, row)}>
							<!-- Pin 27f889893790: the quick-load box is anchored here (not
							     .c-preview) so its hitbox can never sit over the mini preview
							     strip (.preview-hit) - hovering it must never block the
							     journey from artwork/title to the mini preview. Revealed by
							     hovering .c-art or .c-title specifically (CSS below), never
							     the bare row or the preview cell.
							     Pin fce26c7493b0: it floats ABOVE this row rather than on the
							     row's own line, so it can never swallow the row's own
							     double-click. The title text moved into .title-text because
							     THAT span now owns the ellipsis clip - the cell itself has to
							     stop clipping for the box to escape upwards. -->
							<span class="deck-btns">
								<span class="deck-btns-title">load to deck:</span>
								{#each DECKS as d (d)}
									{@const target = deckStates[d]}
									{@const isLoading = performanceCommandStatus.deck_pending[d] > 0}
									{@const isThisTrack = target.stable_id === row.stable_id}
									<button
										class="deck-target"
										class:master-target={target.is_master}
										class:loading-target={isLoading}
										class:loaded-target={isThisTrack}
										title={`Load onto deck ${d}`}
										onclick={(e) => {
											e.stopPropagation();
											onloadrow(row, d);
										}}
										ondblclick={(e) => e.stopPropagation()}
									>
										{#if isLoading}
											<SpinnerIcon size={9} />
										{:else}
											{d}
										{/if}
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
							<span class="title-text"
								>{#each hl(row.title) as part, i (i)}{#if part.hit}<mark
											class="find-hit">{part.text}</mark
										>{:else}{part.text}{/if}{/each}</span
							>
						</td>
						<td class="c-artist" class:rb-row-loaded={loadedIds.has(row.stable_id)} title={row.artist ?? ''}>
							{#each hl(row.artist) as part, i (i)}
								{#if part.hit}<mark class="find-hit">{part.text}</mark>{:else}{part.text}{/if}
							{/each}
						</td>
						<td
							class="c-key"
							class:key-compat={keyCompat(row.key)}
							class:key-inert={keyCellInert(row)}
							style={keyCompatStyle(row.key)}
							title={keyCellTitle(row)}
						>
							{#if row.key_status === 'failed' || row.key_status === 'missing'}
								<span class="key-status">{row.key_status === 'failed' ? 'failed' : 'missing'}</span>
							{:else}
								{#snippet keyCharacters(text: string)}
									{#each text as character}<span class:camelot-suffix={character === 'A' || character === 'B'}>{character}</span>{/each}
								{/snippet}
								{#each hl(row.key) as part, i (i)}
									{#if part.hit}<mark class="find-hit">{@render keyCharacters(part.text)}</mark>{:else}{@render keyCharacters(part.text)}{/if}
								{/each}
							{/if}
						</td>
						<td
							class="c-bpm"
							class:bpm-sweet={bpmCellHeat(row.bpm)?.lane === 'sweet'}
							class:bpm-half={bpmCellHeat(row.bpm)?.lane === 'half'}
							class:bpm-far={bpmCellHeat(row.bpm)?.lane === 'far'}
							class:bpm-inert={bpmCellInert(row)}
							style={bpmCellStyle(row.bpm)}
							title={bpmCellTitle(row)}
						>
							{#if row.bpm_status === 'failed' || row.bpm_status === 'missing'}
								<span class="bpm-status" title={bpmCellTitle(row)}>{row.bpm_status === 'failed' ? 'failed' : 'missing'}</span>
							{:else if row.bpm_status === 'available-not-selected'}
								<span class="bpm-status" title={bpmCellTitle(row)}>alt</span>
							{:else}
								{_fmtBpm(row.bpm)}
							{/if}
						</td>
						<td
							class="c-plays"
							title="play count (rekordbox history + djay)"
						>{row.play_count > 0 ? String(row.play_count) : ''}</td>
						<!-- The cell hands its own width to CSS so the stars can
						     tighten then shrink to fit it (pin 8f60606750c6). -->
						<td class="c-rating" style={`--rating-w:${colWidths.rating}px`}>
							<RatingStars rating={row.rating} onrate={(n) => onrate(row, n)} />
						</td>
						<td class="c-comments" title={row.comments ?? ''}>
							{#each hl(row.comments) as part, i (i)}
								{#if part.hit}<mark class="find-hit">{part.text}</mark>{:else}{part.text}{/if}
							{/each}
						</td>
						<td class="c-time" title={timeCellTitle(row.duration_ms)}>{_fmtTime(row.duration_ms)}</td>
						<td class="c-quality">
							<QualityBadge quality={row.quality} compact showContainer={false} />
						</td>
						<td class="c-energy" class:energy-unset={row.energy === null} title={row.energy_reason}>
							{row.energy ?? ''}
						</td>
						<td class="c-genre">
							{#if splitGenreTags(row.genre ?? row.rb_meta?.genre ?? '').length > 0}
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
							{:else if row.genre_reason}
								<span class="genre-reason" title={row.genre_reason}>{row.genre_reason}</span>
							{/if}
						</td>
						<td class="c-stems">
							<StemTags stems={row.stems} />
							<VocalAnalyzeButton stableId={row.stable_id} stems={row.stems} />
						</td>
						{#if showLyricsCol}
							<LyricColumn {row} />
						{/if}
					</tr>
				{/each}
				{#if windowInfo.bottomPad > 0}
					<tr class="tt-spacer" style={`height:${windowInfo.bottomPad}px`} aria-hidden="true">
						<td colspan={colCount}></td>
					</tr>
				{/if}
			</tbody>
		</table>
		{#if rows.length === 0 && emptyMessage !== null}
			<div class="empty">
				{emptyMessage}
				{#if onemptyretry !== undefined}
					<button type="button" class="empty-retry" onclick={onemptyretry}>Retry search</button>
				{/if}
			</div>
		{/if}
		{#if filterBypassNote !== null}
			<p class="filter-bypass-note" data-testid="filter-bypass-note">{filterBypassNote}</p>
		{/if}
		{#if apCurveSegments.length > 0}
			<svg
				class="ap-curve"
				aria-hidden="true"
				style={`--ap-curve-w:${colWidths.autoplay}px;--ap-curve-left:${colWidths.funnel + colWidths.err + colWidths.cloud + colWidths.order}px`}
			>
				<defs>
					<marker id={apCurveArrowId} viewBox="0 0 6 6" refX="5" refY="3" markerWidth="4" markerHeight="4" orient="auto">
						<path d="M0 0L6 3L0 6z" class="ap-curve-arrow" />
					</marker>
				</defs>
				{#each apCurveSegments as seg (`${seg.from.stable_id}-${seg.to.stable_id}`)}
					<path
						d={segmentPath(seg, apCurveX)}
						class="ap-curve-seg"
						class:skips={seg.skips}
						fill="none"
						marker-end={`url(#${apCurveArrowId})`}
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
		bind:this={loadConfirmEl}
		style={loadConfirmStyle}
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
			onclick={(e) => {
				const pending = loadConfirm;
				const remember = loadConfirmEveryTime;
				loadConfirm = null;
				loadConfirmEveryTime = false;
				if (pending === null) return;
				if (remember) setConfirmPref('dblclick_load_play', false);
				// The dialog paused for a human decision of unknown length, so
				// the felt wait for THIS gesture starts at the Yes click, not
				// at the double-click that only opened it (r3974057968).
				onloadrow(
					pending.row,
					pending.deck,
					pending.reservation !== null
						? { play: true, reservation: pending.reservation, pressT0Ms: e.timeStamp }
						: { play: true, pressT0Ms: e.timeStamp }
				);
			}}>Yes</button
		>
		<button
			type="button"
			class="load-confirm-no"
			onclick={() => {
				const pending = loadConfirm;
				loadConfirm = null;
				loadConfirmEveryTime = false;
				if (pending !== null && pending.reservation !== null) {
					onloadconfirmcancelled?.(pending.deck, pending.reservation);
				}
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
	.autoplay-sort {
		display: inline-flex;
		padding: 0;
		border: 0;
		background: transparent;
		color: inherit;
		cursor: pointer;
	}
	.h-autoplay :global(.ap-explain-wrap) {
		color: inherit;
	}
	.ap-curve {
		/* r3919669577: anchored to the sum of the columns before autoplay
		   (funnel/err/cloud/order), not table-wrap's right edge - table-wrap
		   can be wider than the table, and `right: 0` against table-wrap left
		   the curve floating in that blank space instead of over the column. */
		position: absolute;
		top: 0;
		left: var(--ap-curve-left);
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
	.ap-curve-arrow {
		fill: var(--rb-accent);
	}

	.tt-root {
		display: flex;
		flex-direction: column;
		flex: 1;
		/* LIBUX-01: floor, not 0 - guarantees >= 5 rows survive flex-shrink
		 * against SuggestNextStrip/RecommendedSection (thead 20px + 5 rows
		 * at the current density's own --tt-row-h, so compact and cosy each
		 * get their own correct floor from the same declaration, plus a
		 * 17px classic-scrollbar-gutter allowance: table-wrap is
		 * `overflow: auto` and the default column widths already exceed a
		 * 1280px window's list-panel, so a horizontal scrollbar is real on
		 * platforms without overlay scrollbars (Windows, many Linux
		 * themes) - without this the scrollbar eats into the 5-row content
		 * area from inside the same box height, PR #1007 discussion
		 * r3921198996). The enclosing .perf-root grid reserves enough
		 * total height for this floor to actually fit without overflowing
		 * (see +page.svelte).
		 *
		 * `--tt-truncation-h` is the fourth term: when a whole-collection
		 * search hits its 200-row safety cap, `.truncated-note` renders as a
		 * `flex: none` SIBLING of `.table-wrap` INSIDE this same box, so its
		 * height comes straight out of the space budgeted for the thead, the
		 * five rows and the scrollbar gutter - one full row disappears at
		 * cosy density on non-overlay-scrollbar platforms. The banner is
		 * given a declared 17px height below (rather than letting font
		 * metrics decide) so this floor and the +page.svelte reservation can
		 * both add the SAME number, and it is added only while the banner is
		 * actually showing (PR #1007 discussion r3923591731). */
		--tt-truncation-note-h: 17px;
		--tt-truncation-h: 0px;
		min-height: calc(20px + 5 * var(--tt-row-h) + 17px + var(--tt-truncation-h));
		position: relative;
		/* compact = current tight rows; cosy = taller + roomier cell pad */
		--tt-row-h: 22px;
		--tt-art: 22px;
		--tt-td-pad-x: 6px;
	}
	/* Only while the banner is on screen - an unconditional term would steal
	 * 17px from the deck area on every window that never truncates. */
	.tt-root[data-truncated='true'] {
		--tt-truncation-h: var(--tt-truncation-note-h);
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
	.table-wrap::-webkit-scrollbar {
		height: 2px;
	}
	.table-wrap::-webkit-scrollbar-thumb:horizontal {
		background: var(--rb-text-dim);
	}
	.table-wrap::-webkit-scrollbar-track:horizontal {
		background: transparent;
	}
	.table-wrap::-webkit-scrollbar-button:horizontal {
		display: none;
	}
	/* Static column explanations are portalled to body by columnExplainer(), so
	 * scope this deliberately-global skin here and outrank sticky headers and
	 * the table's own curve/load overlays. */
	:global(.column-explain-panel) {
		position: fixed;
		z-index: 100;
		width: min(280px, calc(100vw - 16px));
		max-height: calc(100vh - 16px);
		overflow-y: auto;
		padding: 6px 8px;
		background: var(--rb-panel-raised, #0a0c0f);
		border: 1px solid var(--rb-border);
		border-radius: 3px;
		box-shadow: 0 6px 18px rgba(0, 0, 0, 0.55);
		color: var(--rb-text);
		font-family: var(--rb-font);
		font-size: 10px;
		line-height: 1.4;
		pointer-events: none;
	}
	table {
		/* width set inline below, from the sum of colWidths (r3919185343) -
		   never 100%, so table-layout: fixed has no extra space to
		   redistribute across columns on a wrap wider than the content. */
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
		/* `thead th` is `position: sticky` (below), which gives every th its
		 * own stacking context - z-index only orders paint WITHIN one th, so
		 * it can never win against a later-DOM-order sibling th regardless
		 * of this element's z-index. `right: -3px` used to let half this
		 * handle's box sit outside its own th (poking into the next
		 * column's th box), which that later th always painted over. Fixed
		 * by absorbing the whole 7px width leftward (`right: 0`) so the
		 * handle never depends on painting above a sibling's stacking
		 * context - verified via document.elementFromPoint at the handle's
		 * own center, see performance-col-resize-hit-target.spec.ts. */
		position: absolute;
		top: 0;
		right: 0;
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
	thead th:nth-child(-n + 3),
	.c-funnel,
	.c-err,
	.c-cloud {
		padding: 0 2px;
	}
	thead th:nth-child(4) .th-label {
		padding: 0 2px;
		font-size: 9px;
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
	tbody tr::after {
		content: '';
		position: absolute;
		left: 0;
		right: 0;
		bottom: 0;
		height: 1px;
		background: #131519;
		pointer-events: none;
		z-index: 1;
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

	/* `left` comes from masterFoldCenterPx as an inline style: the middle of
	 * the TABLE pointed at Rating / Comments, which is not what the badge is
	 * about. See src/lib/rb/master-fold-anchor.ts (pin b44c957f082f). */
	.tt-body-overlay {
		position: absolute;
		left: 0;
		right: 0;
		z-index: 3;
		display: flex;
		justify-content: center;
		/* Never eats a click meant for the row underneath it. */
		pointer-events: none;
		visibility: hidden;
	}
	.tt-body-overlay.measured {
		visibility: visible;
	}
	.master-fold {
		position: absolute;
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
		border-bottom: none;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		vertical-align: middle;
	}
	tbody td {
		position: relative;
		z-index: 0;
	}
	thead th {
		border-bottom: 1px solid var(--rb-border);
	}
	.c-order {
		text-align: center;
		font-variant-numeric: tabular-nums;
		padding: 0 2px;
	}
	.c-bpm,
	.c-plays,
	.c-time {
		text-align: right;
		font-variant-numeric: tabular-nums;
		color: var(--rb-text-dim);
	}
	.h-order .th-label {
		justify-content: center;
		padding: 0 2px;
	}
	.c-plays {
		font-size: 10px;
	}
	.c-order {
		font-size: 9px;
		padding: 0 2px;
	}

	/* Five stars are a fixed-width control, so truncating them is never the
	 * right answer: at 11px with 1px gaps they come to ~67px of advance width
	 * inside 68px of usable cell, and the generic td rule then hangs an
	 * ellipsis off the sliver that did not fit - all five stars painted, and a
	 * '..' after them (pin 8f60606750c6, the maintainer, Wed 2 Sep 2026).
	 *
	 * Two stages, in the order the pin asks for: give back the 1px gaps first,
	 * and only shrink the glyphs once there is no gap left to give. Both
	 * measure against --rating-w, which the cell publishes from colWidths -
	 * state the table already owns, so no ResizeObserver and no layout read.
	 * STAR_ADV (1.2em) is the ★ glyph's advance, which is wider than 1em; using
	 * 1em here would under-measure and let the overflow back in. */
	.c-rating {
		--rating-avail: calc(var(--rating-w, 80px) - 2 * var(--tt-td-pad-x));
		/* Four gaps, each tripled for unrated stars. */
		--rb-star-gap: clamp(
			0px,
			calc((var(--rating-avail) - 5 * 1.2 * var(--rb-fs-browser)) / 12),
			1px
		);
		--rb-star-size: min(var(--rb-fs-browser), calc(var(--rating-avail) / 6));
		text-overflow: clip;
	}
	.c-quality {
		padding: 0 2px;
		overflow: hidden;
		white-space: nowrap;
	}
	.c-energy {
		text-align: center;
		font-variant-numeric: tabular-nums;
		padding: 0;
	}
	.c-energy.energy-unset {
		color: var(--rb-text-dim);
	}
	.energy-icon {
		width: 11px;
		height: 11px;
		fill: currentColor;
	}
	.energy-glyph { display: none; }
	/* At the compact 34px default the base 6-8px td/th padding plus
	 * .th-label's own inner padding leaves too little room for the "QLT"
	 * label and clips the compact badge's border (the maintainer, review thread on
	 * PR #1095): give this column the same tight 2px treatment as the
	 * funnel/err/cloud utility columns instead of widening it back out. */
	.h-quality,
	.h-quality .th-label {
		padding: 0 2px;
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
	.camelot-suffix { font-size: 0.67em; }
	.c-key.key-compat {
		border-radius: 2px;
		padding-left: 4px;
		padding-right: 4px;
	}
	.c-key.key-inert {
		color: var(--rb-text-dim);
		font-size: 0.85em;
		text-transform: lowercase;
	}
	.key-status {
		opacity: 0.85;
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
	.cloud-state-wrap {
		position: relative;
		display: inline-flex;
		width: 18px;
		height: 18px;
		align-items: flex-start;
		justify-content: center;
		vertical-align: middle;
	}
	.cloud,
	.cloud-copy {
		display: inline-flex;
		align-items: center;
		justify-content: center;
	}
	.missing {
		color: var(--rb-red);
		font-weight: 600;
	}
	.not-on-cloud {
		color: var(--rb-text-dim);
	}
	.on-cloud-not-local {
		color: var(--rb-red);
	}
	.on-cloud-and-local {
		color: var(--rb-text);
	}
	.cloud-transfer-track {
		position: absolute;
		right: 0;
		bottom: 1px;
		left: 0;
		height: 2px;
		overflow: hidden;
		border-radius: 1px;
		background: color-mix(in srgb, var(--rb-text-dim) 28%, transparent);
	}
	.cloud-transfer-indicator {
		display: block;
		width: 0;
		height: 100%;
		border-radius: inherit;
		background: var(--rb-orange);
		transition: width 120ms linear;
	}
	.cloud-transfer-indicator.indeterminate {
		width: 45%;
		animation: cloud-transfer-slide 850ms linear infinite;
	}
	@keyframes cloud-transfer-slide {
		from { transform: translateX(-110%); }
		to { transform: translateX(245%); }
	}
	@media (prefers-reduced-motion: reduce) {
		.cloud-transfer-indicator.indeterminate {
			width: 100%;
			animation: none;
			opacity: 0.75;
		}
	}

	/* PERF-RB-01: pending rows stay neutral while disk truth is probed. */
	tbody tr.rb-row-availability-pending td {
		color: var(--rb-text);
	}
	tbody tr.rb-row-availability-pending .c-art img,
	tbody tr.rb-row-availability-pending .art-slate {
		opacity: 0.85;
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
	/* Inherits the td nowrap + ellipsis: a wrapping reason grows the
	 * fixed-height row (22.5px -> 25px), which the virtualization math and
	 * right-click anchored popovers both assume never happens. */
	.genre-reason {
		color: var(--text-muted, #8b949e);
		font-size: 0.85em;
		font-style: italic;
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

	/* preview cell hosts only the mini preview strip: pin 27f889893790 moved
	 * the quick-load box off this cell entirely (onto .c-title, below) so
	 * its hitbox can never sit over .preview-hit and block the mouse
	 * journey to the mini preview. */
	.c-preview {
		position: relative;
	}
	/* The loader opens only from artwork or title (pin 27f889893790), and is
	 * anchored inside .c-title so it renders clear of the preview column.
	 * Pin fce26c7493b0: it sits ABOVE the row (bottom: 100%), never on the
	 * row's own line - inline it covered the title's right-hand side and its
	 * buttons stopPropagation on dblclick, so a double-click aimed at the row
	 * hit a button and the row's own load-and-play never fired. It must never
	 * extend below the row either: that would cover the following row's normal
	 * targets. Because a `td` clips (`overflow: hidden`, for title
	 * truncation), an absolutely positioned box can only escape upwards if the
	 * cell stops clipping - so .c-title is `overflow: visible` and the
	 * ellipsis moved onto the inner .title-text span, which clips the text and
	 * nothing else. Visible
	 * on hover+selected (mouse), per pin 616aaf77b792: hover-only used to
	 * block visibility outright. display stays inline-flex always (never
	 * `none`) so the buttons remain Tab-reachable regardless of
	 * hover/selection - a keyboard user tabbing through the row must have
	 * an equal path to the mouse's hover, and a display:none element cannot
	 * receive the very focus that would reveal it. opacity+pointer-events
	 * do the hiding instead, and :focus-within always wins so Tab landing
	 * on any of these buttons reveals the whole group before the very next
	 * Tab press. Hiding pointer-events lags 100ms behind leaving .c-art/
	 * .c-title (pin 27f889893790's corridor, now a JS timer flipping
	 * `.corridor-grace-active` - CORRIDOR_GRACE_MS): the pointer can leave
	 * those cells, cross the short gap, and still land on a deck button
	 * before the group goes fully inert. The delay is not a CSS
	 * `transition-delay` on `pointer-events`: that property is discrete
	 * and only honours a delay with `transition-behavior: allow-discrete`
	 * (Chrome 117+/Safari 17.4+), which this app's packaged WKWebView
	 * floor (macOS 11) does not have - the same floor that made the #1558
	 * CSS reveal guard inert (PR #1570, issue #1588). Showing has no such
	 * delay for the corridor-travel and keyboard-focus paths - but the
	 * row-selection-driven reveal DOES delay showing (DBLCLICK_GUARD_MS,
	 * issue #1558): that trigger can fire on the first click of a
	 * double-click aimed at the row, and an instantly-clickable box there
	 * hijacked the gesture's second click. */
	.c-title {
		position: relative;
		/* The deck box escapes this cell upwards (pin fce26c7493b0), so the
		 * cell cannot clip. The text keeps its own clip on .title-text. */
		overflow: visible;
	}
	.c-title .title-text {
		display: block;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.deck-btns {
		display: inline-flex;
		position: absolute;
		bottom: 100%;
		top: auto;
		right: 0;
		height: auto;
		max-width: 100%;
		box-sizing: border-box;
		gap: 2px;
		align-items: center;
		padding: 1px 4px;
		border: 1px solid var(--rb-line, #2a3140);
		border-radius: 3px;
		background: var(--rb-panel-raised, #0a0c0f);
		opacity: 0;
		/* The box hangs over the PREVIOUS row (bottom: 100%), so the box
		 * ITSELF must never take a pointer: its padding, border and
		 * background would swallow that row's hover and double-click exactly
		 * the way the on-the-line version swallowed its own row's (Sol P1 on
		 * pin fce26c7493b0). Only the buttons are hittable, and only while
		 * revealed - so the pointer travelling up from .c-title to a deck
		 * button passes THROUGH the box's dead area onto the row above
		 * instead of latching onto it. Interactivity therefore lives on
		 * .deck-btns button below, corridor grace and all. */
		pointer-events: none;
		z-index: 5;
		transition: opacity 120ms ease;
	}
	/* The buttons carry BOTH the interactivity and the footprint, because over
	 * the row above those are the same thing.
	 * Hitbox: hiding pointer-events lags 100ms behind leaving the trigger
	 * cells via `.corridor-grace-active` (pin 27f889893790's corridor, JS
	 * timer - same allow-discrete floor as #1570 / issue #1588); showing
	 * has no such delay.
	 * Size: the global `button` rule (padding 0.4rem 0.9rem) made a
	 * single-digit target 37px wide - measured, the whole box came to 220px,
	 * the ENTIRE width of the title column, and 32px tall against a 22px row,
	 * so a selected row blanked its neighbour's whole title cell. Sized to the
	 * row instead, the cluster keeps to that cell's right-hand side, clear of
	 * its midpoint (the point a click on that row uses), and the box is
	 * shorter than one row so it cannot reach past its immediate neighbour
	 * into the header. */
	.deck-btns button {
		pointer-events: none;
		padding: 0 4px;
		min-width: 16px;
		height: 16px;
		line-height: 1;
		font-size: 10px;
		border-radius: 2px;
	}
	tr.rb-row-selected:has(.c-art:hover, .c-title:hover) .deck-btns,
	tr.corridor-grace-active .deck-btns,
	.deck-btns:hover,
	.deck-btns:focus-within {
		opacity: 1;
	}
	/* Issue #1602 round 2 (Sol P1 on pin 8f064eafc, review thread
	 * PRRT_kwDOSEvNd86gwaHm): a runway spacer row gave row 0's box somewhere
	 * to hang, but its added layout height was invisible to
	 * computeVirtualWindow's scrollTop math (virtual-window.ts) - the
	 * runway vanished the instant startIndex left 0, a real DOM height
	 * discontinuity the JS offset math never accounted for.
	 * Raising .deck-btns's OWN z-index cannot fix the header collision
	 * either - `tbody tr` is `position: relative` with z-index: auto
	 * (needed to contain every row's absolutely-positioned children, e.g.
	 * .audio-cache-chevron), which makes EACH row its own stacking context,
	 * the identical mechanism `.col-resize` documents for sticky `th`
	 * above. .deck-btns's z-index: 5 is trapped inside its OWN row's
	 * auto-level context and can never escape to outrank a sibling
	 * context's explicit z-index (thead th, z-index: 1) - only the ROW's
	 * own z-index decides that contest. Every row already beats the row
	 * above it in paint order for free (a later DOM sibling outranks an
	 * earlier one among z-index: auto contexts), which is why only row 0 -
	 * the one row with the thead, not another row, ahead of it in DOM order
	 * - ever needed anything raised. So: bump row 0's own z-index above
	 * thead's, and ONLY while its box is genuinely revealed (identical
	 * predicate to the opacity reveal directly above), so row 0 still
	 * renders behind the sticky header the rest of the time, exactly like
	 * every other row. Zero added layout height, so
	 * computeVirtualWindow needs no change and no coupling to the runway's
	 * failure mode is possible. */
	tr.rb-row-first.rb-row-selected:has(.c-art:hover, .c-title:hover),
	tr.rb-row-first.corridor-grace-active,
	tr.rb-row-first:has(.deck-btns:hover),
	tr.rb-row-first:has(.deck-btns:focus-within) {
		z-index: 2;
	}
	/* Issue #1558: selecting a row makes this :has() match true at the exact
	 * instant of the FIRST click of a double-click, with the pointer already
	 * resting on .c-art/.c-title (a click cannot happen anywhere else). An
	 * instant pointer-events here put a deck button under the gesture's
	 * SECOND click, which the button's own ondblclick stopPropagation then
	 * ate before the row's own dblclick handler ever ran - "Load onto deck
	 * N" fired (no play) instead of the row's smart-load-and-play. The box
	 * itself stays pointer-events: none throughout (see above), so while the
	 * guard is active the click passes through to the row beneath, exactly
	 * like the box was never there.
	 * `.dblclick-guard-active` is a plain JS-timed class (DBLCLICK_GUARD_MS,
	 * TrackTable.svelte script - armed on every row click), not a CSS
	 * transition-delay: pointer-events is a discrete property, so a browser
	 * only honors transition-delay on it with `transition-behavior:
	 * allow-discrete` (Chrome 117+/Safari 17.4+ only) - tried first on this
	 * PR and verified empirically to do nothing on this app's packaged
	 * WKWebView floor (macOS 11, Codex review PR #1570). A class flip has no
	 * such requirement.
	 * `.deck-btns:hover`/`:focus-within` below are the corridor-travel and
	 * keyboard-focus cases (the box is already open), so those stay instant;
	 * only the SELECT-driven reveal needs the guard. */
	tr.rb-row-selected:not(.dblclick-guard-active):has(.c-art:hover, .c-title:hover) .deck-btns button {
		pointer-events: auto;
	}
	tr.corridor-grace-active:not(.dblclick-guard-active) .deck-btns button {
		pointer-events: auto;
	}
	.deck-btns:hover button,
	.deck-btns:focus-within button {
		pointer-events: auto;
	}
	@media (prefers-reduced-motion: reduce) {
		/* Pin 27f889893790's corridor grace is a JS timer (CORRIDOR_GRACE_MS
		 * / `.corridor-grace-active`), not a CSS transition, so reduced
		 * motion cannot collapse it. Only the opacity fade is decorative;
		 * disable that alone. */
		.deck-btns {
			transition: opacity 0s;
		}
	}
	.deck-btns-title {
		font-size: 8px;
		color: var(--rb-text-dim);
		margin-right: 2px;
		white-space: nowrap;
	}
	/* Per-deck state on the quick-load targets, per the pin: dim by default
	   (empty deck = blank, no extra class), yellow border while that deck is
	   master, the shared loading-wheel spinner while a command for that deck
	   is in flight, slightly grey once that deck already holds THIS row's
	   track. Numerals stay white throughout so they read against every
	   state. */
	.deck-btns button.deck-target {
		color: #fff;
		opacity: 0.55;
	}
	.deck-btns button.deck-target.loaded-target {
		opacity: 0.75;
		background: color-mix(in srgb, var(--rb-panel-raised) 60%, #000 20%);
	}
	.deck-btns button.deck-target.master-target {
		opacity: 1;
		border-color: var(--rb-yellow, #e8c13a);
	}
	.deck-btns button.deck-target.loading-target {
		opacity: 1;
		color: var(--rb-accent);
	}
	.deck-btns button.remove-btn {
		color: var(--rb-red);
		font-size: 11px;
		font-weight: 700;
		line-height: 1;
		cursor: pointer;
	}
	tbody tr:hover .row-remove {
		display: block;
	}
	.row-remove:hover {
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
		overflow: visible;
		vertical-align: middle;
	}
	.c-artist {
		overflow: visible;
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
	.empty-retry {
		display: block;
		margin: 12px auto 0;
		color: var(--rb-text);
		background: var(--rb-surface-2);
		border: 1px solid var(--rb-border);
		border-radius: 4px;
		padding: 6px 12px;
		font: inherit;
		cursor: pointer;
	}
	.empty-retry:focus-visible {
		outline: 2px solid var(--rb-accent);
		outline-offset: 2px;
	}
	.filter-bypass-note {
		margin: 0;
		padding: 3px 8px;
		border-top: 1px solid var(--rb-border);
		color: var(--rb-orange);
		font-size: var(--rb-fs-label);
	}
	/* Declared height, not font-metric height: `.tt-root`'s min-height floor
	 * and `+page.svelte`'s library reservation both have to add this exact
	 * number, and a box whose height depends on the rendered line box cannot
	 * be added to a static budget (PR #1007 discussion r3923591731).
	 * border-box so the 1px border and 2px padding are inside the 17px. */
	.truncated-note {
		flex: none;
		box-sizing: border-box;
		height: var(--tt-truncation-note-h);
		padding: 2px 8px;
		border-top: 1px solid var(--rb-border);
		color: var(--rb-text-dim);
		font-size: var(--rb-fs-label);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.load-confirm {
		position: fixed;
		z-index: 80;
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
