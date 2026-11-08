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
	import { onMount, tick, untrack, type Component, type Snippet } from 'svelte';
	import { clampToViewport } from '$lib/ui/clamp-to-viewport';
	import {
		artworkUrl,
		artworkStatusLabel,
		decodePreviewStrip,
		type PreviewStripData,
		type Vocals
	} from '$lib/rb/api-rb';
	import { artworkFetchAllowed, artworkRetry, artworkRetrySrc } from '$lib/rb/artwork-retry';
	import {
		autoMusicalWidths,
		COL_DEFAULTS,
		compactUtilityWidths,
		hiddenColumnClasses,
		shownColumnWidths,
		type ColId
	} from '$lib/rb/library-column-widths';
	import { bpmCellTitle as buildBpmCellTitle } from '$lib/rb/bpm-cell-title';
	import {
		nearestOffscreenPartner,
		partnerIndices,
		isPairedRow,
		pairFoldCenterPx,
		pairFoldGroupMasterCenterPx,
		pairRowState
	} from '$lib/rb/pairing-row';
	import {
		genreCellTitle,
		listingGenre,
		analysisIssuesFor,
		bpmGridHoverText,
		errColumnTitle,
		gridFlagFor,
		lengthFlagText,
		sourceFileFlagsFor,
		gridProvenanceFor,
		requestGridProvenance,
		camelotKeyColor,
		camelotKeyHoverLabel,
		columnHeaderTitle,
		type LibraryColTipId,
		masterFoldCenterPx,
		highlightSpans,
		keyCellSpans,
		rowMatchesFind,
		arrowHead,
		buildCurveSegments,
		hopStroke,
		paintChainHops,
		segmentPath,
		describeAutoPlayMode,
		installTrackDragGhost,
		removeTrackDragGhost,
		trackDragRefusal,
		bpmHeatLabel,
		classifyBpmHeat,
		genreHoverColor,
		columnExplainer,
		displayKey,
		formatKey,
		formatTagsCell,
		LastPlayedCell,
		loadBpmKeyEditPopover,
		formatDraftBpm,
		commitBpmEdit,
		commitKeyEdit,
		editedBpm,
		editedKey,
		hasBpmEdit,
		hasKeyEdit,
		canvasMeasureText,
		fitMusicalColumns,
		renderedTableFont,
		bpmCellText,
		hasUsableBpm,
		type TrackEditModalKind,
		compatibleRowKeyShift,
		compatibleKeyChecked,
		keyShiftSuperscript,
		shiftedKeyText,
		loadKeyHoverCard,
		type CompatibleKeyShift,
		animateSectionToggle,
		draggedDeckRows,
		rowWindow,
		slotCountOf,
		slotOfRow,
		trackDropGapFromPointer,
		trackDropGapTransform,
		type EntriesMove,
		type SectionHeader,
		type TrackDropGap,
		OPTIONAL_COLUMNS as OPTIONAL_COLUMN_IDS,
		OPTIONAL_COLUMN_LABELS,
		VersionStackPanel,
		WorksModals,
		classicalArtist,
		classicalTitle,
		composerOf,
		filedUnderText,
		labelOf,
		performerOf,
		primaryWorkId,
		refreshSuggestionFlags,
		reportWorksFailure,
		shiftDropAction,
		stackIndicator,
		startWorksOverlay,
		viaWorkHits,
		workCellText,
		worksView,
		yearOf,
		type OptionalColumn
	} from './track-table-support';
	import { tagsCellFor } from '$lib/rb/library-tags.svelte';
	import { classifyBpmCompatibility } from '$lib/rb/bpm-heat';
	import { audioQualityHover, ensureLibraryScans, setGridFlagDismissed } from '$lib/rb/library-scans';
	import { camelotKeysAreCompatible, DECK_IDS, deckStates, pitchRanges } from '$lib/rb/audio-engine.svelte';
	import { KEY_SOURCE_HINT } from '$lib/player/key/camelot';
	import { plannedTitle } from '$lib/rb/planned-explainers';
	import {
		previewCue,
		previewCueRatioFor,
		stopPreviewCue
	} from '$lib/player/preview-cue.svelte';
	import type { AnlzData } from '$lib/rb/anlz-types';
	import { autoPlayOrder } from '$lib/rb/auto-play.svelte';
	import { autoPlayQueue } from '$lib/rb/autoplay-queue.svelte';
	import { deckHoverLinkId } from '$lib/rb/deck-hover.svelte';
	import {
		setConfirmPref,
		setKeyNotation,
		setLibraryDensity,
		setUiSkin,
		showAllLibraryColumns,
		toggleLibraryColumn,
		uiPrefs
	} from '$lib/rb/prefs.svelte';
	import { quickDrawUi } from '$lib/rb/quick-draw-ui.svelte';
	import { beginTrackDrag, endTrackDrag, TRACK_STABLE_MIME } from '$lib/rb/track-drag.svelte';
	import { collapseDuration } from '$lib/motion/collapse';
	import TrackSectionRow from './TrackSectionRow.svelte';
	import {
		computeVirtualWindow,
		createRowVisibilityObserver,
		TRACK_TABLE_THEAD_PX,
		masterFoldVisibility,
		scrollTopForRowIndex,
		scrollTopForDeckLayoutAnchor
	} from './virtual-window';
	import { browseScrollTarget, nextMeasuredViewportHeight, shouldApplyBrowseScroll, shouldRestorePaneScroll } from './browse-scroll';
	import type { BrowserRow, RowProvider, SortDir, SortKey } from './pane-contract.svelte';
	import AutoPlayExplainer from './AutoPlayExplainer.svelte';
	import AutoPlayRankCell from './AutoPlayRankCell.svelte';
	import AutoPlayWalkthrough from './AutoPlayWalkthrough.svelte';
	import LyricColumn from './LyricColumn.svelte';
	import PreviewStrip from './PreviewStrip.svelte';
	import BpmStabilityCell from './BpmStabilityCell.svelte';
	import { bpmStabilityTitle } from './bpm-stability-title';
	import {
		fetchRowAssetsLazily,
		holdArtworkForBoot,
		PreviewStripFiller,
		stripLessIdsNear
	} from '$lib/rb/preview-strip-fill';
	import QualityBadge from '../QualityBadge.svelte';
	import RatingStars from './RatingStars.svelte';
	import CommentCellEditor from './CommentCellEditor.svelte';
	import { truncationTip } from '$lib/ui/truncation-tip';
	import AnalysisDotsPopover from './AnalysisDotsPopover.svelte';
	import StemTags from './StemTags.svelte';
	import VocalAnalyzeButton from './VocalAnalyzeButton.svelte';
	import { ANALYSIS_COLORS } from '$lib/rb/analysis-kinds';
	import {
		jobProgress,
		jobRowStyleVars,
		type AnalysisBadge,
		type AnalysisIssues
	} from '$lib/rb/job-progress.svelte';
	import { audioPrefetchStatus } from '$lib/rb/audio-prefetch-cache.svelte';
	import { performanceCommandStatus } from '$lib/rb/performance-ipc.svelte';
	import SpinnerIcon from './SpinnerIcon.svelte';
	import TrackContextMenu, {
		TagCell,
		tagTypeChooserEntries,
		tagTypeColumns,
		toggleTagTypeColumn,
		toggleTagTypeInTags
	} from './TrackContextMenu.svelte';
	import TrackRowPopovers, { type PlaylistsMenuAnchor, type RelocateMenuAnchor } from './TrackRowPopovers.svelte';
	import { trackCloudView } from './track-cloud-state';
	import { libraryRowHoverTitle, rowRendersUnavailable } from './browser-row-wire';
	import CloudStatusIcon from './CloudStatusIcon.svelte';
	import MinorIssueSquare from './MinorIssueSquare.svelte';
	import SortArrowIcon from './SortArrowIcon.svelte';
	import { minorIssuesFor } from '$lib/rb/track-minor-issues';
	import { PLAY_TRIANGLE_PATH } from '$lib/ui/icon-glyphs';
	import { isSetSkipShortcut, setTargetState } from '$lib/rb/set-target-state.svelte';
	import BpmDisputeMarker from '../BpmDisputeMarker.svelte';
	import BpmDisputeCard from '../BpmDisputeCard.svelte';
	import { putBpmPick } from '$lib/rb/api-rb';
	import type { BpmAlternative } from '$lib/rb/bpm-dispute';
	import { grossClashEdges } from '$lib/rb/set-clash-marker';
	import {
		nextTrackTableIndex,
		resolveActiveRowIndex,
		scrollTopToRevealRow,
		trackRowKey,
		trackTableKeyAction,
		trackTablePageSize,
		trackTableRangeSpan,
		type TrackTableNavKey
	} from './track-table-keyboard';
	import { dragEndIsCollapsingClick, markRowPress, type RowPressMark } from './row-click-intent';

	type DeckId = (typeof DECK_IDS)[number];

	const DECKS: DeckId[] = [1, 2, 3, 4];
	// Fixed row heights (virtualization window math requires constant height).
	// compact = current tight rows; cosy = taller + slightly roomier cell pad.
	const ROW_HEIGHT_COMPACT = 22;
	const ROW_HEIGHT_COSY = 30;
	const OVERSCAN = 10;

	// ----- AUTOPLAY-COL -----------------------------------------------------
	const AUTOPLAY_COL_COUNT = 21;
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
		/** Opened by Enter on a row: focus Yes so a second Enter confirms. */
		fromKeyboard: boolean;
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
			if (loadConfirm.fromKeyboard) {
				loadConfirmEl.querySelector<HTMLButtonElement>('.load-confirm-yes')?.focus();
			}
		})();
	});
	let trackContextMenu = $state<TrackContextMenu | null>(null);
	let playlistsMenu = $state<PlaylistsMenuAnchor | null>(null);
	let relocateMenu = $state<RelocateMenuAnchor | null>(null);

	// Pin a720a02ecf92: double-click, or right-click then Edit, opens a small
	// editor above the tempo or key cell. The value sticks for this session.
	type CellEditKind = 'bpm' | 'key';
	type CellEditTarget = { kind: CellEditKind; row: BrowserRow; anchor: HTMLElement };
	let cellEdit = $state<CellEditTarget | null>(null);

	// The editor is fetched on first use, so the /performance bundle does not carry it.
	let BpmKeyEditPopover = $state<Awaited<ReturnType<typeof loadBpmKeyEditPopover>>['default'] | null>(null);

	function showCellEdit(target: CellEditTarget): void {
		cellEdit = target;
		if (BpmKeyEditPopover !== null) return;
		void loadBpmKeyEditPopover().then(
			(mod) => {
				BpmKeyEditPopover = mod.default;
			},
			(error: unknown) => {
				if (cellEdit === target) cellEdit = null;
				console.error('[library] the tempo/key editor could not load', error);
			}
		);
	}

	function openCellEdit(event: MouseEvent, row: BrowserRow, kind: CellEditKind): void {
		event.preventDefault();
		event.stopPropagation();
		const anchor = event.currentTarget;
		if (!(anchor instanceof HTMLElement)) return;
		showCellEdit({ kind, row, anchor });
	}

	/** Right-click on a tempo or key cell: the row's whole menu with Edit on top, never
	 * Edit alone, so the cell does not hide Add to playlist / Remove from playlist. */
	function openCellEditMenu(event: MouseEvent, row: BrowserRow, kind: CellEditKind): void {
		const anchor = event.currentTarget;
		if (!(anchor instanceof HTMLElement)) return;
		cellEdit = null;
		trackContextMenu?.open(event, row, [
			{
				id: 'edit',
				label: 'Edit',
				testId: 'cell-value-edit',
				title: kind === 'bpm' ? 'Edit tempo' : 'Edit key',
				run: () => showCellEdit({ kind, row, anchor })
			}
		]);
	}

	/** A row as its K/B cells print it, session edits applied, for the column fit. */
	function editedMusicalRow(row: BrowserRow): BrowserRow {
		if (!hasBpmEdit(row.stable_id) && !hasKeyEdit(row.stable_id)) return row;
		return {
			...row,
			key: editedKey(row.stable_id, row.key),
			bpm: editedBpm(row.stable_id, row.bpm),
			...(hasKeyEdit(row.stable_id) ? { key_status: 'ok' as const } : {}),
			...(hasBpmEdit(row.stable_id) ? { bpm_status: 'ok' as const } : {})
		};
	}

	// LIBUX-56: double-click the Comment cell, or the row menu's Edit comment,
	// swaps the cell text for CommentCellEditor. Keyed by row order, since one
	// track can sit on two rows of a playlist.
	let commentEditOrder = $state<number | null>(null);

	function openCommentEdit(event: MouseEvent | null, row: BrowserRow): void {
		event?.preventDefault();
		event?.stopPropagation();
		if (oncomment === undefined) return;
		cellEdit = null;
		commentEditOrder = row.order;
	}

	async function saveComment(row: BrowserRow, next: string): Promise<void> {
		try {
			await oncomment?.(row, next);
		} finally {
			if (commentEditOrder === row.order) commentEditOrder = null;
		}
	}

	function cellEditInitial(target: CellEditTarget): string {
		if (target.kind === 'bpm') {
			const bpm = editedBpm(target.row.stable_id, target.row.bpm);
			return bpm === null ? '' : formatDraftBpm(bpm);
		}
		return formatKey(editedKey(target.row.stable_id, target.row.key), 'camelot') ?? '';
	}

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
	const masterBpm = $derived(masterDeck?.bpm ?? null);
	const masterStableId = $derived(masterDeck?.stable_id ?? null);
	const hoverDeckId = $derived(deckHoverLinkId(uiPrefs.deck_hover_link));
	const hoverStableId = $derived(
		hoverDeckId === null ? null : (deckStates[hoverDeckId].stable_id ?? null)
	);

	/** With the compatible filter on, a row matches under the active mode and
	 * may need a key shift (KEYSHIFT-01..03); otherwise the ring keeps today's
	 * +-1 family against the master. 24 keys at most, so memoized per key. */
	const keyMatchCache = $derived.by(() => {
		void keyCompatRef;
		void uiPrefs.next_only_filter;
		void uiPrefs.compatible_filter;
		return new Map<string | null, CompatibleKeyShift | null>();
	});

	function keyMatch(key: string | null): CompatibleKeyShift | null {
		const cached = keyMatchCache.get(key);
		if (cached !== undefined) return cached;
		let match: CompatibleKeyShift | null;
		if (uiPrefs.next_only_filter && compatibleReferenceKey !== null && compatibleKeyChecked(uiPrefs.compatible_filter)) {
			match = compatibleRowKeyShift(key, compatibleReferenceKey, uiPrefs.compatible_filter);
		} else {
			match = camelotKeysAreCompatible(key, keyCompatRef) && key !== null ? { shift: 0, key } : null;
		}
		keyMatchCache.set(key, match);
		return match;
	}

	function keyCompat(key: string | null): boolean {
		return keyMatch(key) !== null;
	}

	let keyHover = $state<{
		row: BrowserRow;
		anchor: { left: number; top: number; width: number; height: number };
	} | null>(null);
	// The hover card loads on the first key hover, so it stays out of the
	// library route's initial bundle (bundle budget, scripts/check-bundle-size.sh).
	let KeyHoverCard = $state<Component<{
		ownKey: string | null;
		shift: number;
		shiftedKey: string | null;
		note?: string | null;
		anchor: { left: number; top: number; width: number; height: number };
		onDismiss: () => void;
	}> | null>(null);
	let keyHoverCardLoad: Promise<void> | null = null;

	function _onKeyCellEnter(e: MouseEvent, row: BrowserRow): void {
		if (KeyHoverCard === null && keyHoverCardLoad === null) {
			keyHoverCardLoad = loadKeyHoverCard().then(
				(mod) => {
					KeyHoverCard = mod.default;
				},
				(err: unknown) => {
					// A failed chunk fetch must not disable the card for the session:
					// the next hover retries. The failure itself is shown, never only logged.
					keyHoverCardLoad = null;
					const detail = err instanceof Error ? err.message : String(err);
					onkeycardfailed(`Key card failed to load: ${detail}. Hover the key again to retry.`);
				}
			);
		}
		const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
		keyHover = {
			row,
			anchor: { left: rect.left, top: rect.top, width: rect.width, height: rect.height }
		};
	}

	// GRID-OCTAVE-02: the hover card on a disputed BPM's marker. It stays open
	// while the pointer crosses into it, so its one-click button is reachable.
	let bpmDispute = $state<{
		row: BrowserRow;
		anchor: { left: number; top: number; width: number; height: number };
	} | null>(null);
	let bpmDisputeCloseTimer: ReturnType<typeof setTimeout> | null = null;

	function cancelBpmDisputeClose(): void {
		if (bpmDisputeCloseTimer !== null) clearTimeout(bpmDisputeCloseTimer);
		bpmDisputeCloseTimer = null;
	}

	function scheduleBpmDisputeClose(): void {
		cancelBpmDisputeClose();
		bpmDisputeCloseTimer = setTimeout(() => (bpmDispute = null), 250);
	}

	function openBpmDispute(e: MouseEvent, row: BrowserRow): void {
		cancelBpmDisputeClose();
		const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
		bpmDispute = { row, anchor: { left: rect.left, top: rect.top, width: rect.width, height: rect.height } };
	}

	async function pickBpmAlternative(row: BrowserRow, alt: BpmAlternative): Promise<void> {
		// The server announces library.changed (tracks, beatgrid_edit): the
		// row and any deck holding the track re-read their tempo from it.
		await putBpmPick(row.stable_id, alt.source);
	}

	function keyCompatStyle(key: string | null): string | undefined {
		// A shifted match shows the key it plays in, so it takes that key's color.
		const match = keyMatch(key);
		const base = keyCellStyle(match !== null && match.shift !== 0 ? match.key : key);
		if (!keyCompat(key) || masterKeyColor === null) return base;
		const border = `--key-compat-color: ${masterKeyColor}`;
		return base === undefined ? border : `${base};${border}`;
	}

	function keyCellInert(row: BrowserRow): boolean {
		return (row.key === null || row.key === undefined) && (row.key_status === 'failed' || row.key_status === 'missing');
	}

	/** ONE notation for every row (ui-prefs key_notation), never the stored spelling. */
	function keyCellDisplay(row: BrowserRow) {
		return displayKey(row.key, uiPrefs.key_notation);
	}

	/** The row's canonical Camelot code ("8A"), for the key wheel e2e veto (#7599). */
	function rowCanonicalCamelotKey(row: BrowserRow): string | undefined {
		const shown = displayKey(row.key, 'camelot');
		return shown?.parsed === true ? shown.text : undefined;
	}

	// The key cell is a hover-card host (one explainer per control), so the
	// LIBUX-8682 clash reason rides the card note beside the KEY-CELL-01 reason.
	function keyHoverNote(row: BrowserRow): string | null {
		const parts = [keyCellInert(row) ? keyCellTitle(row) : null, clashEdges.get(row.stable_id) ?? null];
		const present = parts.filter((part): part is string => part !== null);
		return present.length === 0 ? null : present.join('\n');
	}

	function keyCellTitle(row: BrowserRow): string {
		if (row.key !== null && row.key !== undefined && row.key_source && KEY_SOURCE_HINT[row.key_source] && row.key_status !== 'ok') {
			return `${camelotKeyHoverLabel(row.key) ?? row.key} (${KEY_SOURCE_HINT[row.key_source]}; our analysis: ${row.key_reason ?? row.key_status})`;
		}
		if (row.key_status === 'failed') {
			const reason = row.key_reason ?? 'key analysis failed';
			// KEY-GUESS-01: the dimmed "8A?" cell explains itself on the key hover card (note line).
			if ((row.key === null || row.key === undefined) && row.key_guess) {
				return `Best guess ${row.key_guess} (ambiguous key, confidence ${Math.round((row.key_guess_confidence ?? 0) * 100)}%). ${reason}`;
			}
			return reason;
		}
		if (row.key_status === 'missing') {
			return row.key_reason ?? 'key not analyzed yet';
		}
		if (keyCellDisplay(row)?.parsed === false) {
			return `Unrecognised key notation, shown as stored: ${row.key}`;
		}
		return `${camelotKeyHoverLabel(row.key) ?? 'Key not analyzed'} Dynamic key, musical mode, and chord progression analysis: not analyzed.`;
	}

	function bpmCellHeat(bpm: number | null) {
		return classifyBpmHeat(bpm, masterBpm);
	}

	function bpmCellCompatibility(bpm: number | null) {
		return classifyBpmCompatibility(bpm, masterBpm);
	}

	function bpmCellStyle(bpm: number | null): string | undefined {
		const heat = bpmCellHeat(bpm);
		return heat === null ? undefined : `color:${heat.color}`;
	}

	function bpmCellInert(row: BrowserRow): boolean {
		return bpmCellText(row).kind !== 'value';
	}

	function bpmCellTitle(row: BrowserRow): string {
		if (row.bpm_status === 'failed') {
			return row.bpm_reason ?? 'bpm analysis failed';
		}
		if (row.bpm_status === 'missing') {
			return row.bpm_reason ?? 'bpm not analyzed yet';
		}
		if (row.bpm_status === 'too_short' || row.bpm_status === 'no_pulse') {
			return buildBpmCellTitle(row, masterBpm);
		}
		// buildBpmCellTitle carries method and confidence (LIBUX-34). The grid
		// sentence stays inline so the hover still names the stored verdict.
		const notSelected =
			row.bpm_status === 'available-not-selected'
				? ` ${row.bpm_reason ?? 'beatgrid analysis available but not selected'}.`
				: '';
		return `${buildBpmCellTitle(row, masterBpm)} ${bpmHeatLabel(bpmCellHeat(row.bpm), masterBpm) ?? 'BPM not analyzed'}${hasUsableBpm(row.bpm) ? ` Exact BPM: ${row.bpm.toFixed(1)}.` : ''}${notSelected} Dynamic tempo analysis: not analyzed. ${bpmGridHoverText(row, gridProvenanceFor(row.stable_id))}`;
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

	// GRIDFLAG-02 / QLTSCAN-03: the beatgrid verdicts and QLT letters on the rows
	// are stored server-side.
	// Ask once per page session for them to be brought up to date; the engine
	// skips every unchanged grid and announces any change on library.changed.
	// Library mode starts no worker (LIBWS-03), and the scan is one.
	$effect(() => {
		if (!libraryMode) ensureLibraryScans();
	});

	/** GRIDFLAG-04: hide or restore one row's beatgrid flag (a persisted user
	 * track field). The row is updated only after the engine confirmed it; a
	 * failure rejects, and the popover that asked reports it. */
	async function _setGridFlagDismissed(row: BrowserRow, dismissed: boolean): Promise<void> {
		const result = await setGridFlagDismissed(row.stable_id, dismissed);
		if (row.grid_quality) row.grid_quality = { ...row.grid_quality, dismissed: result.dismissed };
		if (row.etag !== '') row.etag = result.etag;
	}

	function _jobRowStyle(stableId: string): string | undefined {
		const job = jobProgress.activeFor(stableId);
		if (job === null) return undefined;
		return jobRowStyleVars(job);
	}

	let {
		provider,
		libraryMode = false,
		selectedIds,
		selectedOrders,
		loadedIds,
		playedIds = loadedIds,
		vocalsById,
		markerAnlzById,
		previewStripById,
		stripLoadingById,
		sortKey,
		sortDir,
		emptyMessage,
		onemptyretry = undefined,
		filterBypassNote = null,
		restoreKey,
		scrollTop,
		browseScroll = null,
		removable = false,
		readOnly = false,
		reorderable = false,
		reorderBlocked = null,
		onrestoreorder = undefined,
		onscrollcursor,
		onrenderedrowcapacity,
		onlibraryviewport,
		onsort,
		sectionHeaders = [],
		renamingSectionId = null,
		onsectionrenamestart,
		onsectionrename,
		onsectionrenamecancel,
		onsectionremove,
		onsectioninsert,
		onsectiontoggle,
		onsectiondragitems,
		oninsertsection,
		onmoveentries,
		onselectrow,
		onloadrow,
		onkeycardfailed,
		onpickdoubledeck,
		onloadconfirmcancelled,
		onpreviewseek,
		onrate,
		oncomment = undefined,
		onrefused,
		onrowvisible,
		onremoverow,
		onremoverows,
		removingRowKeys = new Set<string>(),
		restoringRowKeys = new Set<string>(),
		ongenrefilter,
		genreFilterUntil = 0,
		searchQuery = '',
		stackPlaylistId = null as string | null,
		findQuery = '',
		/** Suggest-next hover: temporarily highlight + scroll to this row. */
		suggestHoverId = null as string | null,
		pairedPartnerIds = new Set<string>() as ReadonlySet<string>,
		pairPartners,
		pairError,
		pairPulses = false,
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
		oneditrefresh = undefined,
		onaddtoplaylist = undefined
	}: {
		/** Read contract: { rows, total, truncated, fetchWindow } - see
		 * pane-contract.svelte.ts. */
		provider: RowProvider;
		selectedIds: string[];
		selectedOrders: number[];
		loadedIds: Set<string>;
		/** SESSION-PLAYED-01: loaded now or earlier this session; paints the green title. */
		playedIds?: ReadonlySet<string>;
		/** Vocals ALREADY known client-side (loaded decks / anlz cache) -
		 * v1 scope: strips never fetch /anlz themselves (see BrowserPanel). */
		vocalsById: Record<string, Vocals>;
		/** Strip marker ANLZ ALREADY in memory (loaded decks / anlz cache),
		 * resolved by BrowserPanel (LIBUX-12); absent = markerless strip. */
		markerAnlzById: Record<string, AnlzData>;
		/** Preview strip bytes ALREADY in memory (listing hydrate / anlz cache),
		 * resolved by BrowserPanel; absent = dash until warmed elsewhere. */
		previewStripById: Record<string, PreviewStripData | null>;
		/** True only while a warmed cache entry is still loading (never per-row fetch). */
		stripLoadingById: Record<string, boolean>;
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
		/** IOPIN-01 controller/keyboard selection scroll request. A revision
		 * makes repeated encoder steps observable even if they land on the same
		 * row at a list boundary. */
		browseScroll?: { order: number; direction: -1 | 1; revision: number } | null;
		/** add-remove-reorder-tracks: true when the pane is a real playlist
		 * (not All Tracks / blank) - shows the per-row remove control. */
		removable?: boolean;
		/** True when the pane is read-only (Spotify, a taglist, an autolist). */
		readOnly?: boolean;
		/** True when removable AND the pane is in its natural membership
		 * order (no client sort/search) - drag handles only render then,
		 * since row.order would otherwise not match the visual position. */
		reorderable?: boolean;
		/** W33: why an editable playlist cannot be drag-reordered right now
		 * (a sort or search is on), or null. Added to the order cell's title,
		 * and shown with a one-click restore when a row drag starts. */
		reorderBlocked?: string | null;
		onrestoreorder?: (() => void) | undefined;
		/** Reports the live table-wrap scrollTop back to the pane store. */
		onscrollcursor: (top: number) => void;
		/** Reports the number of actual rows the viewport can show after its
		 * header and current density are accounted for. */
		onrenderedrowcapacity?: (count: number) => void;
		/** Reports the space for song rows below the header and the row height;
		 * (0, 0) when the table unmounts. */
		onlibraryviewport?: (viewportPx: number, rowPx: number) => void;
		onsort: (key: SortKey) => void;
		/** Dividers to draw among `rows`, from arrangeSections. */
		sectionHeaders?: readonly SectionHeader[];
		renamingSectionId?: string | null;
		onsectionrenamestart?: ((sectionId: string) => void) | undefined;
		onsectionrename?: ((sectionId: string, title: string) => void) | undefined;
		onsectionrenamecancel?: (() => void) | undefined;
		onsectionremove?: ((sectionId: string) => void) | undefined;
		onsectioninsert?: ((header: SectionHeader) => void) | undefined;
		onsectiontoggle?: ((sectionId: string, collapsed: boolean) => void) | undefined;
		/** A section's tracks, hidden ones included, or null if one cannot move. */
		onsectiondragitems?: ((sectionId: string) => string[] | null) | undefined;
		oninsertsection?: ((row: BrowserRow) => void) | undefined;
		onmoveentries?: ((move: EntriesMove) => void) | undefined;
		/** `event` carries the click's (or arrow key's) Shift/Cmd/Ctrl state;
		 * keyboard moves reuse the same plain / Shift-range gestures. */
		onselectrow: (row: BrowserRow, event?: MouseEvent | KeyboardEvent) => void;
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
		onpreviewseek?: ((row: BrowserRow, ratio: number) => void) | undefined;
		onrate: (row: BrowserRow, next: number) => void;
		/** LIBUX-56: save the Comment cell editor's text; absent means the comment is read only. */
		oncomment?: ((row: BrowserRow, next: string) => Promise<void>) | undefined;
		onrowvisible: (row: BrowserRow) => void;
		/** Remove this row's membership position from the playlist. */
		onremoverow?: (row: BrowserRow) => void;
		/** LIBUX-8626: Delete/Backspace removes the whole selection in ONE write,
		 * resolving once the rows are gone, so focus can move to the row that
		 * took their place. */
		onremoverows?: (rows: BrowserRow[]) => Promise<void>;
		/** trackRowKey of rows mid red-collapse before they leave the list. */
		removingRowKeys?: ReadonlySet<string>;
		/** LIBUX-8676: trackRowKey of rows an undo just put back (green expand). */
		restoringRowKeys?: ReadonlySet<string>;
		/** A drag the table refused, with the reason. The table does not own a
		 * toast channel, so the panel says it (pins 8ba0b15d975b /
		 * 72be3e505510: a silent refusal reads as a broken feature). */
		onrefused?: (reason: string) => void;
		/** The open list, when it is a real playlist: a stack shows places in it. */
		stackPlaylistId?: string | null;
		onstemsdonext?: (stableIds: string[]) => void;
		onlyricsdonext?: (stableIds: string[]) => void;
		onopeneditmodal?: (kind: TrackEditModalKind) => void;
		/** Remove selected tracks from the library (files stay on disk). */
		onremovefromlibrary?: (stableIds: string[]) => void;
		/** FLOW-07: called after a relocate apply succeeds, so the pane can
		 * reload and pick up the row's fresh file_exists/broken state. */
		onrelocated?: (() => void) | undefined;
		oneditrefresh?: (() => void) | undefined;
		/** Open the add-to-playlist picker for the selected tracks. */
		onaddtoplaylist?: (stableIds: string[]) => void;
		/** Genre chip / post-filter gestures. */
		ongenrefilter?: (mode: 'strict' | 'loose' | 'clear' | 'undo', tag?: string) => void;
		/** Epoch ms until which library dbl/triple remap to clear/undo. */
		genreFilterUntil?: number;
		/** Live pane search (for same-tag toggle + active chip). */
		searchQuery?: string;
		/** In-place find highlight (>=3 chars); empty = off. */
		findQuery?: string;
		/** Suggest-next hover: temporarily highlight + scroll to this row. */
		suggestHoverId?: string | null;
		/** Purple pairing underline: partner ids of the current master. */
		pairedPartnerIds?: ReadonlySet<string>;
		/** PAIR-09: every paired track's partners (both directions), for the
		 * zigzag edge on every paired row and its crackle when a partner is loaded. */
		/** Required: an omitted map must not read as "no pairings". */
		pairPartners: ReadonlyMap<string, ReadonlySet<string>>;
		/** Non-null while the pairing list failed to load: edges are UNKNOWN,
		 * so the table says so instead of reading as "no pairings". */
		pairError: string | null;
		/** PAIR-09: loop the earlier pulses on live rows (library.pair_edge_pulses). */
		pairPulses?: boolean;
		/** Panel-owned status surface, pinned below the column headers. */
		bodyOverlay?: Snippet;
		/** When next-only filter is on, highlight keys against this ref (issue #3983). */
		compatibleReferenceKey?: string | null;
		/** LIBWS-03: mounted in the Library workspace, which has no decks. */
		libraryMode?: boolean;
		/** A key hover card that fails to load is reported here, never only logged. */
		/** Required: a failed key-card chunk load must reach the user (fail-fast). */
		onkeycardfailed: (message: string) => void;
	} = $props();

	const keyCompatRef = $derived(
		uiPrefs.next_only_filter && compatibleReferenceKey !== null
			? compatibleReferenceKey
			: masterKey
	);
	const masterKeyColor = $derived(camelotKeyColor(keyCompatRef));

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

	/** The Genre cell's raw text: the listing's choice, else rekordbox's unless the precedence hides it. */
	function genreText(row: BrowserRow): string {
		return listingGenre(row) ?? '';
	}

	function editTime(ms: number | null | undefined): string {
		if (ms == null) return '--:--.-';
		const seconds = Math.max(0, ms) / 1000;
		const minutes = Math.floor(seconds / 60);
		return `${String(minutes).padStart(2, '0')}:${(seconds % 60).toFixed(1).padStart(4, '0')}`;
	}

	function editFx(row: BrowserRow): string {
		return row.fx == null || Object.keys(row.fx).length === 0
			? '{}'
			: Object.keys(row.fx).sort().join(',');
	}

	async function editRequest(path: string, method: 'POST' | 'PATCH' | 'DELETE', body?: object): Promise<void> {
		const response = await fetch(path, {
			method,
			headers: { 'X-Undo-Session': 'webui-edits', ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
			...(body === undefined ? {} : { body: JSON.stringify(body) })
		});
		if (!response.ok) throw new Error(`${method} ${path}: ${response.status} ${await response.text()}`);
		oneditrefresh?.();
	}

	async function createVirtualEdit(row: BrowserRow): Promise<void> {
		const label = window.prompt('Edit label', `${row.title ?? 'Edit'} edit`);
		if (label === null) return;
		const inText = window.prompt('In point (milliseconds)', '0');
		if (inText === null) return;
		const outText = window.prompt('Out point (milliseconds)', String(row.duration_ms ?? ''));
		if (outText === null) return;
		const fxText = window.prompt('FX parameters (JSON object)', '{}');
		if (fxText === null) return;
		try {
			const fx = JSON.parse(fxText) as unknown;
			if (typeof fx !== 'object' || fx === null || Array.isArray(fx)) throw new Error('FX must be a JSON object');
			await editRequest('/api/v1/edits', 'POST', {
				source_track_id: row.stable_id,
				label,
				in_ms: Number(inText),
				out_ms: Number(outText),
				fx,
				...(stackPlaylistId !== null ? { playlist_id: stackPlaylistId } : {}),
				...(row.item_id !== null ? { after_item_id: row.item_id } : {})
			});
		} catch (error) {
			onrefused?.(error instanceof Error ? error.message : String(error));
		}
	}

	async function updateVirtualEdit(row: BrowserRow): Promise<void> {
		const editId = row.edit_id;
		if (!editId) return;
		const label = window.prompt('Edit label', row.edit_label ?? '');
		if (label === null) return;
		const inText = window.prompt('In point (milliseconds)', String(row.in_ms ?? 0));
		if (inText === null) return;
		const outText = window.prompt('Out point (milliseconds)', String(row.out_ms ?? ''));
		if (outText === null) return;
		const fxText = window.prompt('FX parameters (JSON object)', JSON.stringify(row.fx ?? {}));
		if (fxText === null) return;
		try {
			const fx = JSON.parse(fxText) as unknown;
			if (typeof fx !== 'object' || fx === null || Array.isArray(fx)) throw new Error('FX must be a JSON object');
			await editRequest(`/api/v1/edits/${encodeURIComponent(editId)}`, 'PATCH', {
				label, in_ms: Number(inText), out_ms: Number(outText), fx
			});
		} catch (error) {
			onrefused?.(error instanceof Error ? error.message : String(error));
		}
	}

	async function deleteVirtualEdit(row: BrowserRow): Promise<void> {
		const editId = row.edit_id;
		if (!editId || !window.confirm(`Delete virtual edit "${row.edit_label ?? row.title ?? ''}"?`)) return;
		try {
			await editRequest(`/api/v1/edits/${encodeURIComponent(editId)}`, 'DELETE');
		} catch (error) {
			onrefused?.(error instanceof Error ? error.message : String(error));
		}
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
		_noteRowPointerFocus(row, event);
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
		_requestLoadPlay(row, {
			shift: event.shiftKey,
			replace: event.metaKey || event.ctrlKey,
			timeStamp: event.timeStamp,
			x: event.clientX,
			y: event.clientY,
			fromKeyboard: false
		});
	}

	/** The double-click load+play gesture, shared by a row double-click and
	 * Enter on a focused row so both pick the same deck, honor the same
	 * confirm preference and own the same reservation. */
	function _requestLoadPlay(
		row: BrowserRow,
		gesture: {
			shift: boolean;
			replace: boolean;
			timeStamp: number;
			x: number;
			y: number;
			fromKeyboard: boolean;
		}
	): void {
		// LIBWS-04: Library mode has no deck to confirm; a double-click
		// previews the track from its start instead.
		if (libraryMode) {
			onpreviewseek?.(row, 0);
			return;
		}
		const picked = onpickdoubledeck?.(row, {
			shift: gesture.shift,
			replace: gesture.replace
		});
		const deck = picked?.deck ?? (gesture.shift ? null : 1);
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
					? { play: true, reservation, pressT0Ms: gesture.timeStamp }
					: { play: true, pressT0Ms: gesture.timeStamp }
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
			x: gesture.x,
			y: Math.max(8, gesture.y - 20),
			fromKeyboard: gesture.fromKeyboard
		};
	}

	function hl(text: string | null): Array<{ text: string; hit: boolean }> {
		return highlightSpans(text ?? '', findQuery);
	}

	const rows = $derived(provider.rows);
	const maxRowOrder = $derived(rows.reduce((max, row) => Math.max(max, row.order), 0));
	$effect(() => {
		const compact = compactUtilityWidths(maxRowOrder);
		void uiPrefs.ui_skin; // a skin switch changes the rendered font
		// K/B are measured in the font the table renders with, once it is in the DOM. Table
		// fonts are local system faces (app.css loads only the brand face), so nothing
		// arrives later to re-measure for.
		const musical = wrapEl === null ? null : fitMusicalColumns({
			rows: rows.map((row) => editedMusicalRow(row)),
			notation: uiPrefs.key_notation,
			font: renderedTableFont(wrapEl),
			padX: uiPrefs.library_density === 'cosy' ? 8 : 6,
			keyHeader: 'Key',
			bpmHeader: 'BPM',
			measure: canvasMeasureText
		});
		void wrapWidth;
		// Manual K/B drags stay authoritative; other utility widths reflow with data.
		const current = untrack(() => colWidths);
		colWidths = {
			...current,
			...compact,
			...(musical === null ? {} : autoMusicalWidths(current, musical, manuallyResizedColumns))
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
		if (isSetSkipShortcut(event) && setTargetState.playlistId !== null && event.target === event.currentTarget) {
			// LIBUX-8679: Cmd/Ctrl+Shift+X toggles "Skip in set" on the row (or its selection).
			event.preventDefault();
			event.stopPropagation();
			void setTargetState.toggleSkip(selectedOrders.includes(row.order) ? [...selectedIds] : [row.stable_id]);
			return;
		}
		const action = trackTableKeyAction(event);
		if (action.kind === 'move') {
			event.preventDefault();
			event.stopPropagation();
			_keyboardMove(action.nav, action.extend, event);
			return;
		}
		if (action.kind === 'load') {
			event.preventDefault();
			event.stopPropagation();
			if (genreWindowOpen()) return;
			const tr = event.currentTarget instanceof HTMLElement ? event.currentTarget : null;
			const rect = tr?.getBoundingClientRect();
			_requestLoadPlay(row, {
				shift: action.shift,
				replace: action.replace,
				timeStamp: event.timeStamp,
				x: rect !== undefined ? rect.left + Math.min(240, rect.width / 3) : 0,
				y: rect !== undefined ? rect.bottom + 20 : 0,
				fromKeyboard: true
			});
			return;
		}
		if (
			removable &&
			(event.key === 'Delete' || event.key === 'Backspace') &&
			(onremoverows || onremoverow)
		) {
			event.preventDefault();
			event.stopPropagation();
			const doomed = selectedOrderSet.has(row.order)
				? rows.filter((visible) => selectedOrderSet.has(visible.order))
				: [row];
			if (onremoverows) {
				void onremoverows(doomed).then(_focusRowAfterRemoval);
			} else {
				for (const visible of doomed) onremoverow?.(visible);
			}
			return;
		}
		if (event.key !== 'ContextMenu' && !(event.shiftKey && event.key === 'F10')) return;
		trackContextMenu?.openFromKeyboard(event, row);
	}

	// ----- keyboard navigation (track-table-keyboard.ts) --------------------
	// The active row is the one roving tabindex focuses; the anchor is the
	// fixed end a Shift-extended move grows from (a plain click or move sets
	// it, Shift never does). Both are (stable_id, order) keys because a
	// playlist can repeat a track. Selection itself stays in the pane model:
	// moves call onselectrow exactly like a plain or Shift click.
	let activeRowKey = $state<string | null>(null);
	let anchorRowKey: string | null = null;
	const activeRowIndex = $derived(
		resolveActiveRowIndex({ rows, activeKey: activeRowKey, selectedOrders })
	);

	function _noteRowPointerFocus(row: BrowserRow, event: MouseEvent): void {
		const key = trackRowKey(row);
		activeRowKey = key;
		if (!event.shiftKey || anchorRowKey === null) anchorRowKey = key;
	}

	function _keyboardMove(nav: TrackTableNavKey, extend: boolean, event: KeyboardEvent): void {
		const list = rows;
		const target = nextTrackTableIndex({
			current: activeRowIndex,
			nav,
			rowCount: list.length,
			pageSize: trackTablePageSize(renderedRowCapacity)
		});
		if (target < 0) return;
		const targetRow = list[target];
		const targetKey = trackRowKey(targetRow);
		if (extend) {
			if (anchorRowKey === null && activeRowIndex >= 0) {
				anchorRowKey = trackRowKey(list[activeRowIndex]);
			}
			const anchor =
				anchorRowKey === null ? -1 : list.findIndex((r) => trackRowKey(r) === anchorRowKey);
			const span = trackTableRangeSpan({ anchor, target, rowCount: list.length });
			if (span.from === span.to) {
				onselectrow(targetRow);
				anchorRowKey = targetKey;
			} else {
				// The pane's Shift-range spans from its last selected row, so
				// re-seat that on the fixed anchor first: the same plain-then-
				// Shift click pair a mouse user would make.
				onselectrow(list[anchor]);
				onselectrow(targetRow, event);
			}
		} else {
			onselectrow(targetRow);
			anchorRowKey = targetKey;
		}
		activeRowKey = targetKey;
		_revealAndFocusRow(target);
	}

	/** LIBUX-8626: the focused row left the DOM with the removal; hand roving
	 * focus to the newly selected row so the next Backspace keeps working. */
	async function _focusRowAfterRemoval(): Promise<void> {
		activeRowKey = null;
		await tick();
		if (activeRowIndex >= 0) _revealAndFocusRow(activeRowIndex);
	}

	/** Scroll the virtualized window so the row is mounted and fully in view,
	 * then move DOM focus to it (roving tabindex) once it has rendered. */
	function _revealAndFocusRow(index: number): void {
		const el = wrapEl;
		if (el === null) return;
		const next = scrollTopToRevealRow({
			rowIndex: slotOfRow(sectionHeaders, index),
			rowHeight,
			headerOffsetPx: TRACK_TABLE_THEAD_PX,
			viewportHeight,
			scrollTop: liveScrollTop
		});
		if (next !== liveScrollTop) {
			el.scrollTop = next;
			liveScrollTop = next;
			onscrollcursor(next);
		}
		void tick().then(() => {
			el.querySelector<HTMLElement>(`tr[data-row-index="${index}"]`)?.focus({
				preventScroll: true
			});
		});
	}

	// ----- AUTOPLAY-COL helpers ---------------------------------------------
	let hoveredApId = $state<string | null>(null);
	const autoPlayMode = $derived(describeAutoPlayMode(uiPrefs).mode);
	const showLyricsCol = $derived(uiPrefs.lyrics_library_col && uiPrefs.lyrics_global);
	/** R-TT-1: columns come from tag type rows, so a new type needs no code here. */
	const typeCols = $derived(tagTypeColumns());
	const colCount = $derived(
		(autoPlayMode === 'off' ? AUTOPLAY_COL_COUNT - 1 : AUTOPLAY_COL_COUNT) +
			(showLyricsCol ? 1 : 0) +
			typeCols.length -
			uiPrefs.library_hidden_columns.length
	);

	// ----- LIBUX-54 hidden columns (chooser lives in ColumnChooserMenu) -----
	const hiddenCols = $derived(new Set(uiPrefs.library_hidden_columns));
	const optionalCols = $derived(worksView.optionalColumns);
	const optionalOff = $derived(
		OPTIONAL_COLUMN_IDS.filter((col) => !optionalCols.includes(col))
	);
	/** colWidths with hidden columns at 0: what the table width and the
	 * MASTER fold badge are summed from. */
	const shownWidths = $derived(
		shownColumnWidths(colWidths, [...uiPrefs.library_hidden_columns, ...optionalOff])
	);
	const columnChooserPrefs = {
		state: uiPrefs,
		toggleColumn: toggleLibraryColumn,
		tagTypes: tagTypeChooserEntries,
		toggleTagTypeColumn,
		toggleTagTypeInTags,
		showAllColumns: showAllLibraryColumns,
		setKeyNotation,
		setDensity: setLibraryDensity,
		setSkin: setUiSkin
	};

	function _autoPlayRank(stableId: string): number | null {
		return autoPlayOrder.rankOf.get(stableId) ?? null;
	}

	const rowIndexOf = $derived.by(() => {
		const map = new Map<string, number>();
		for (let i = 0; i < rows.length; i++) map.set(rows[i].stable_id, slotOfRow(sectionHeaders, i));
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
	/** Hue and direction for the hop that leaves each ranked row. */
	const hopByFrom = $derived.by(() => {
		const map = new Map<string, { direction: -1 | 1; color: string }>();
		for (const hop of paintChainHops(autoPlayOrder.chain, rowIndexOf)) {
			map.set(hop.fromId, { direction: hop.direction, color: hopStroke(hop.hue) });
		}
		return map;
	});

	/** r3919185343: `table-layout: fixed` at `width: 100%` redistributes any
	 * extra space beyond the configured column total across the columns on a
	 * wide wrap, so the rendered title column drifts right of where
	 * masterFoldCenterPx (computed from the raw colWidths) puts the badge.
	 * Pinning the table to exactly this sum removes the extra space there is
	 * to redistribute - narrower than the wrap just leaves blank space to the
	 * right, same as any wrap wider than its content. */
	const tableWidthPx = $derived(
		Object.entries(shownWidths).reduce(
			(sum, [id, w]) => sum + (id === 'lyrics' && !showLyricsCol ? 0 : w),
			typeCols.reduce((sum, col) => sum + col.width, 0)
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
		if (el === null || anchor === null) return;
		const effectiveViewportHeight =
			viewportHeight > 0 ? viewportHeight : el.clientHeight;
		if (effectiveViewportHeight <= 0) return;
		const next = scrollTopForDeckLayoutAnchor({
			rowIndex: anchor.rowIndex,
			rowHeight,
			headerOffsetPx: TRACK_TABLE_THEAD_PX,
			viewportHeight: effectiveViewportHeight,
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
	/** PAIR-09: the PAIR arrow's own width, for the same clamp and for sitting
	 * beside MASTER when both point off the same edge. */
	let pairFoldWidth = $state(0);
	let lastAppliedBrowseScrollRevision: number | null = $state(null);
	let lastRestoredPaneKey: string | number | null = $state(null);
	$effect(() => {
		if (!shouldRestorePaneScroll(restoreKey, lastRestoredPaneKey)) return;
		const el = wrapEl;
		if (el !== null) {
			const restored = untrack(() => scrollTop);
			el.scrollTop = restored;
			liveScrollTop = restored;
		}
		lastRestoredPaneKey = restoreKey;
		activeRowKey = null;
		anchorRowKey = null;
	});
	// IOPIN-01 keep-in-view: apply only on new browseScroll.revision (LIBUX-2606).
	$effect(() => {
		const request = browseScroll;
		if (request === null || !shouldApplyBrowseScroll(request, lastAppliedBrowseScrollRevision)) return;
		const el = wrapEl;
		if (el === null) return;
		const target = untrack(() =>
			browseScrollTarget({ request, rows, viewportHeight, rowHeight, headerOffsetPx: TRACK_TABLE_THEAD_PX, slotOfRow: (index) => slotOfRow(sectionHeaders, index) })
		);
		if (target === null) return;
		el.scrollTop = target;
		liveScrollTop = target;
		onscrollcursor(target);
		lastAppliedBrowseScrollRevision = request.revision;
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
		viewportHeight = nextMeasuredViewportHeight(el.clientHeight, viewportHeight);
		wrapWidth = el.clientWidth;
		if (typeof ResizeObserver === 'undefined') return; // SSR guard
		const ro = new ResizeObserver((entries) => {
			for (const entry of entries) {
				viewportHeight = nextMeasuredViewportHeight(entry.contentRect.height, viewportHeight);
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

	const slotCount = $derived(slotCountOf(sectionHeaders, rows.length));
	const windowInfo = $derived(
		rowWindow(
			computeVirtualWindow({
				scrollTop: liveScrollTop,
				viewportHeight,
				rowHeight,
				rowCount: slotCount,
				overscan: OVERSCAN,
				headerOffsetPx: TRACK_TABLE_THEAD_PX
			}),
			sectionHeaders,
			rows.length,
			rowHeight
		)
	);
	const visibleRows = $derived(rows.slice(windowInfo.startIndex, windowInfo.endIndex));
	// LIBUX-8682: a gross key clash between on-screen neighbours (skips bridged) marks the lower row.
	const clashEdges = $derived(
		setTargetState.summary === null
			? new Map<string, string>()
			: grossClashEdges(rows.map((r) => r.stable_id), setTargetState.skipIds, setTargetState.summary.clashes)
	);
	const renderedRowCapacity = $derived(
		Math.min(
			rows.length,
			Math.max(0, Math.floor((viewportHeight - TRACK_TABLE_THEAD_PX) / rowHeight))
		)
	);

	$effect(() => {
		onrenderedrowcapacity?.(renderedRowCapacity);
	});

	// Space for songs, not how many are loaded. The thead sits inside the
	// same scroller the virtual window already subtracts.
	$effect(() => {
		onlibraryviewport?.(viewportHeight - TRACK_TABLE_THEAD_PX, rowHeight);
	});
	$effect(() => {
		return () => onlibraryviewport?.(0, 0);
	});

	// Strips for rows in view (+ one screen of margin) that were listed without
	// one: asked for in debounced batches, never per row (NATIVE-21).
	let filledStrips: Record<string, PreviewStripData | null> = $state({});
	// Row OBJECTS whose assets landed: a library refresh hands out new rows
	// without them, and those must be asked for again (not skipped by id).
	const rowAssetsSettled = new WeakSet<object>();
	const stripFiller = new PreviewStripFiller({
		// LIBM-172: one batch settles strip, vocals and cover verdict; lazy so it
		// stays off the /performance chunk.
		fetchBatch: async (ids) =>
			fetchRowAssetsLazily(ids, () => untrack(() => rows), rowAssetsSettled),
		onStrip: (id, wire) => {
			filledStrips[id] = decodePreviewStrip(wire.preview_b64, wire.preview_max);
		},
		onError: (error) => console.warn('[preview-strips] batch read failed; retrying with backoff', error),
		now: () => Date.now(),
		setTimer: (fn, ms) => setTimeout(fn, ms),
		clearTimer: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>)
	});
	$effect(() => () => stripFiller.dispose());
	$effect(() => {
		const missing = stripLessIdsNear(
			rows,
			windowInfo.startIndex,
			windowInfo.endIndex,
			renderedRowCapacity,
			(row) => rowAssetsSettled.has(row)
		);
		untrack(() => {
			stripFiller.reopen(missing);
			stripFiller.setVisible(missing);
		});
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
			rowIndex: slotOfRow(sectionHeaders, idx),
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
			rowIndex: masterIndex < 0 ? -1 : slotOfRow(sectionHeaders, masterIndex),
			rowHeight,
			scrollTop: liveScrollTop,
			viewportHeight,
			headerOffsetPx: TRACK_TABLE_THEAD_PX
		})
	);

	/** PAIR-09 fold cue: hidden while any of the master's partners is on screen,
	 * else pointing at the nearest off-screen partner, which a click jumps to. */
	const pairRowIndices = $derived(partnerIndices(rows, pairedPartnerIds));
	const pairTarget = $derived(
		nearestOffscreenPartner(
			pairRowIndices,
			(rowIndex) =>
				masterFoldVisibility({
					rowIndex,
					rowHeight,
					scrollTop: liveScrollTop,
					viewportHeight,
					headerOffsetPx: TRACK_TABLE_THEAD_PX
				}),
			(liveScrollTop + viewportHeight / 2) / Math.max(1, rowHeight)
		)
	);
	const pairIndex = $derived(pairTarget?.index ?? -1);
	const pairFold = $derived(pairTarget?.fold ?? null);
	const masterFoldSameEdge = $derived(masterFold !== null && masterFold === pairFold);
	/** MASTER badge centre. Beside the PAIR arrow it moves as one group with the arrow,
	 * so the arrow keeps its 6px gap to MASTER's right at the wrap's right edge (PAIR-09). */
	const masterFoldLeftPx = $derived.by(() => {
		const centre = masterFoldCenterPx(shownWidths, liveScrollLeft, wrapWidth, masterFoldBadgeWidth / 2);
		if (!masterFoldSameEdge || wrapWidth <= 0) return centre;
		return pairFoldGroupMasterCenterPx({
			masterCenterPx: centre,
			masterWidthPx: masterFoldBadgeWidth,
			pairWidthPx: pairFoldWidth,
			wrapWidthPx: wrapWidth
		});
	});
	const pairFoldLeftPx = $derived.by(() => {
		if (masterFoldSameEdge) {
			return pairFoldCenterPx({
				titleCenterPx: masterFoldLeftPx,
				masterOnSameEdge: true,
				masterWidthPx: masterFoldBadgeWidth,
				pairWidthPx: pairFoldWidth
			});
		}
		return masterFoldCenterPx(shownWidths, liveScrollLeft, wrapWidth, pairFoldWidth / 2);
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
		const target = scrollTopForRowIndex({
			rowIndex: slotOfRow(sectionHeaders, idx),
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
			rowIndex: slotOfRow(sectionHeaders, masterIndex),
			rowHeight,
			headerOffsetPx: TRACK_TABLE_THEAD_PX,
			offsetFromTopPx: viewportHeight * 0.35
		});
		wrapEl.scrollTop = target;
		liveScrollTop = target;
		onscrollcursor(target);
	}

	function jumpToPair(): void {
		if (wrapEl === null || pairIndex < 0) return;
		const target = scrollTopForRowIndex({
			rowIndex: pairIndex,
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
		// W33: while a sort or search blocks drag-reorder, the cell says why.
		return reorderBlocked === null
			? `Playlist position ${order}`
			: `Playlist position ${order}\n${reorderBlocked}`;
	}

	function timeCellTitle(ms: number | null, lengthFlag: string | null): string {
		if (ms === null) return 'Duration unknown';
		const base = `Duration ${_fmtTime(ms)} (mm:ss)`;
		return lengthFlag === null ? base : `${base}\n${lengthFlag}`;
	}

	function _fmtBpm(bpm: number | null): string {
		// Screenshot truncates to '132.' - one decimal is the sanctioned improvement.
		return bpm === null ? '' : String(Math.round(bpm));
	}

	// LIBM-181: a cover that fails to load is retried (2 s, 8 s, 30 s), never
	// recorded as "no artwork"; only the server's artwork_available says that.
	// The tick re-renders rows when a retry comes due or a resync clears ids.
	const coverRetry = artworkRetry();
	let coverRetryTick = $state(0);
	$effect(() => coverRetry.subscribe(() => (coverRetryTick += 1)));

	function _onArtworkLoad(event: Event, stableId: string): void {
		(event.currentTarget as HTMLImageElement).classList.add('art-loaded');
		coverRetry.noteLoad(stableId);
	}

	function _onArtworkError(stableId: string): void {
		coverRetry.noteError(stableId);
	}

	function _retrySrc(url: string, stableId: string): string {
		void coverRetryTick;
		return artworkRetrySrc(url, stableId, coverRetry);
	}

	// LIBM-172: cover images wait in the boot scheduler's deferred queue, behind
	// the boot library index, so ~30 artwork reads do not compete with it.
	let artworkReleased = $state(holdArtworkForBoot(() => (artworkReleased = true)));

	function _showArtworkImg(stableId: string, artworkAvailable: boolean | null): boolean {
		return (
			artworkReleased &&
			artworkAvailable === true &&
			coverRetryTick >= 0 &&
			artworkFetchAllowed(stableId, coverRetry)
		);
	}

	// ----------------------------------------- drag to move (native DnD)
	// A drag lifts a block of lines. While it hovers, the lines from the gap down
	// shift by the selected block height with transform only (LIBUX-53, #7605),
	// so no row re-lays-out on a frame. The gap stores the drop target computed
	// from the same slot, so preview and drop cannot diverge.
	let dragBlock = $state<{ item_ids: string[]; section_ids: string[] } | null>(null);
	let dropGap = $state<TrackDropGap | null>(null);
	/** Keep the last gap's policy while its transform animates closed. */
	let gapMotionRows = $state(1);
	let tbodyEl = $state<HTMLTableSectionElement | null>(null);
	const draggedItems = $derived(new Set(dragBlock?.item_ids ?? []));

	function _slotShift(slot: number): string | undefined {
		return dropGap === null ? undefined : trackDropGapTransform(slot, dropGap);
	}

	function _dragGapCount(block: { item_ids: string[]; section_ids: string[] }): number {
		// Section-title dragging remains a one-slot preview until #7603 defines
		// its block semantics. Track drags open one slot per selected membership.
		return block.section_ids.length > 0 ? 1 : block.item_ids.length;
	}

	function _rowBlock(row: BrowserRow): { item_ids: string[]; section_ids: string[] } | null {
		const picked = selectedOrderSet.has(row.order)
			? rows.filter((r) => selectedOrderSet.has(r.order))
			: [row];
		const itemIds = picked.map((r) => r.item_id);
		if (itemIds.includes(null)) return null;
		return { item_ids: itemIds as string[], section_ids: [] };
	}

	function _endEntriesDrag(): void {
		dragBlock = null;
		dropGap = null;
	}

	function onSectionDragStart(event: DragEvent, header: SectionHeader): void {
		const itemIds = onsectiondragitems?.(header.section.id) ?? null;
		if (!reorderable || itemIds === null) {
			event.preventDefault();
			return;
		}
		dragBlock = { item_ids: itemIds, section_ids: [header.section.id] };
		event.dataTransfer?.setData('text/plain', header.section.title);
		if (event.dataTransfer) event.dataTransfer.effectAllowed = 'move';
	}

	/** dragenter and dragover both accept a move (REORDER-ROW-01): WebKit (the
	 * packaged WKWebView) takes a target's accept from its dragenter whenever the
	 * pointer crosses into a new element, so a release right after a crossing,
	 * with no dragover yet, was refused when only dragover was cancelled. */
	function onBodyDragOver(event: DragEvent): void {
		if (dragBlock === null || tbodyEl === null) return;
		event.preventDefault();
		if (event.dataTransfer) event.dataTransfer.dropEffect = 'move';
		const bodyTop = tbodyEl.getBoundingClientRect().top;
		const draggedTrackCount = _dragGapCount(dragBlock);
		gapMotionRows = draggedTrackCount;
		dropGap = trackDropGapFromPointer({
			pointerY: event.clientY - bodyTop,
			rowHeight,
			slotCount,
			headers: sectionHeaders,
			rows,
			draggedTrackCount
		});
	}

	function onBodyDragLeave(event: DragEvent): void {
		// relatedTarget is null between cells on WebKit, so ask where the pointer went.
		const box = tbodyEl?.getBoundingClientRect();
		if (box === undefined) return;
		const inside =
			event.clientX >= box.left &&
			event.clientX <= box.right &&
			event.clientY >= box.top &&
			event.clientY <= box.bottom;
		if (!inside) dropGap = null;
	}

	function onBodyDrop(event: DragEvent): void {
		const block = dragBlock;
		const gap = dropGap;
		_endEntriesDrag();
		if (block === null || gap === null) return;
		event.preventDefault();
		onmoveentries?.({ ...block, ...gap.target });
	}

	async function toggleSection(header: SectionHeader): Promise<void> {
		const collapsing = !header.collapsed;
		onsectiontoggle?.(header.section.id, collapsing);
		await tick();
		if (tbodyEl !== null) {
			animateSectionToggle(tbodyEl, { header, collapsing, rowHeight, viewportHeight });
		}
	}

	// W33: a row drag on a sorted/searched playlist shows why reorder is off.
	// Stays up for a few seconds after the drag so its button can be clicked.
	const REORDER_HINT_LINGER_MS = 6000;
	let reorderHintShown = $state(false);
	let _reorderHintTimer: ReturnType<typeof setTimeout> | null = null;

	function _showReorderHint(): void {
		if (_reorderHintTimer !== null) clearTimeout(_reorderHintTimer);
		_reorderHintTimer = null;
		reorderHintShown = true;
	}

	function _lingerReorderHint(): void {
		if (!reorderHintShown) return;
		if (_reorderHintTimer !== null) clearTimeout(_reorderHintTimer);
		_reorderHintTimer = setTimeout(() => {
			_reorderHintTimer = null;
			reorderHintShown = false;
		}, REORDER_HINT_LINGER_MS);
	}

	function _restoreOrder(): void {
		// Fail fast: a blocker with no restore action is a broken integration, so
		// throw before the hint hides rather than dismiss it with the sort still on.
		if (!onrestoreorder) throw new Error('TrackTable: reorderBlocked is set but onrestoreorder is missing');
		if (_reorderHintTimer !== null) clearTimeout(_reorderHintTimer);
		_reorderHintTimer = null;
		reorderHintShown = false;
		onrestoreorder();
	}

	$effect(() => {
		if (reorderBlocked === null) reorderHintShown = false;
	});
	$effect(() => () => {
		if (_reorderHintTimer !== null) clearTimeout(_reorderHintTimer);
	});

	// ----- version stacks (R-VER, R-WORK-8): a plain drop never links ------------
	const stackList = $derived(onremoverow === undefined ? null : stackPlaylistId);
	let _dragTrackIds: string[] = [];
	let stackHintRowId = $state<string | null>(null);
	let stackOpenId = $state<string | null>(null);
	let worksModals = $state<ReturnType<typeof WorksModals> | null>(null);
	const stackOpenIndex = $derived(
		stackOpenId === null ? -1 : rows.findIndex((r) => r.stable_id === stackOpenId)
	);
	const viaWork = $derived(searchQuery === '' ? null : viaWorkHits(searchQuery));

	onMount(startWorksOverlay);
	// Only the mounted window: a whole collection would be a 50k id POST per scroll.
	$effect(() => {
		const ids = visibleRows.map((r) => r.stable_id);
		if (ids.length > 0) refreshSuggestionFlags(ids);
	});

	function indicatorFor(row: BrowserRow): string | null {
		return stackIndicator(
			worksView.overlay,
			row.stable_id,
			worksView.suggestionIds.has(row.stable_id)
		);
	}

	function shownTitle(row: BrowserRow): string {
		const title = row.is_edit && row.edit_label ? row.edit_label : (row.title ?? '');
		return worksView.classicalLayout
			? classicalTitle(worksView.overlay, row.stable_id, title)
			: title;
	}

	function shownArtist(row: BrowserRow): string {
		const artist = row.artist ?? '';
		return worksView.classicalLayout
			? classicalArtist(worksView.overlay, row.stable_id, artist)
			: artist;
	}

	function optionalCell(row: BrowserRow, col: OptionalColumn): string {
		const overlay = worksView.overlay;
		switch (col) {
			case 'work':
				return workCellText(overlay, row.stable_id);
			case 'composer':
				return composerOf(overlay, row.stable_id);
			case 'ensemble':
				return performerOf(overlay, row.stable_id);
			case 'label':
				return labelOf(overlay, row.stable_id);
			case 'year':
				return String(yearOf(overlay, row.stable_id) ?? '');
			default: {
				const _exhaustive: never = col;
				throw new Error(`Unhandled: ${String(_exhaustive)}`);
			}
		}
	}

	function _stackOnto(dragged: string[], row: BrowserRow): void {
		const action = shiftDropAction(
			dragged,
			row.stable_id,
			(id) => primaryWorkId(worksView.overlay, id)
		);
		if (action === 'nothing') return;
		if (action === 'already') {
			onrefused?.('already versions');
			return;
		}
		const stacked = [...new Set([...dragged, row.stable_id])];
		worksModals?.stackTracks(stacked, row.title ?? '').catch((exc: unknown) =>
			reportWorksFailure('link failed', exc)
		);
	}

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
			is_streaming: row.is_streaming ?? row.rb_meta?.is_streaming ?? false,
			offload: row.offload ?? null
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
		_dragTrackIds = [...ids];
		// The MIME is still set for cross-app interop; drop targets accept on
		// the in-app state because WKWebView hides it during dragover.
		event.dataTransfer?.setData(TRACK_STABLE_MIME, ids.join(','));
		// REORDER-ROW-01: in a playlist shown in membership order the row itself
		// is a move handle too (rekordbox behaviour), so the drag allows a move
		// onto the table body as well as the copy onto a deck or playlist.
		const block = reorderable ? _rowBlock(row) : null;
		dragBlock = block;
		if (event.dataTransfer) event.dataTransfer.effectAllowed = block === null ? 'copy' : 'copyMove';
		// Without an explicit drag image WebKit snapshots this <tr> plus every
		// composited layer overlapping it, so the whole browser panel appears
		// to come along for the ride.
		installTrackDragGhost(event, {
			title: row.title ?? row.stable_id,
			artist: row.artist ?? '',
			count: ids.length
		});
		// KEYSHIFT-03: each dragged row's displayed shift (its superscript) travels
		// with the drag, so a drop onto a deck loads it shifted like a double-click.
		// Every selected id gets an entry: a multi-row drop loads the first of them.
		if (reorderBlocked !== null) _showReorderHint();
		beginTrackDrag(ids, draggedDeckRows(ids, rows, (r) => keyMatch(r.key)?.shift ?? 0, row));
	}

	/** LIBUX-8676: the last plain press on a row (row-click-intent.ts). */
	let _rowPress: RowPressMark | null = null;

	function onRowDragEnd(event: DragEvent, row: BrowserRow): void {
		// dragend comes after any drop, so a drag that ended anywhere else must
		// not stay armed for the next drop on a row.
		_endEntriesDrag();
		_dragTrackIds = [];
		stackHintRowId = null;
		removeTrackDragGhost();
		endTrackDrag();
		_lingerReorderHint();
		// A press that WebKit turned into a drag but that barely moved and
		// dropped nowhere was a click: collapse the selection like one.
		const key = trackRowKey(row);
		if (
			dragEndIsCollapsingClick({
				mark: _rowPress,
				key,
				end: event,
				dropEffect: event.dataTransfer?.dropEffect,
				selectedCount: selectedOrderSet.size
			})
		) {
			_noteRowPointerFocus(row, event);
			onselectrow(row);
		}
		_rowPress = null;
	}


	function onGripDragStart(event: DragEvent, row: BrowserRow): void {
		event.stopPropagation();
		dragBlock = _rowBlock(row);
		event.dataTransfer?.setData('text/plain', String(row.order));
		if (event.dataTransfer) event.dataTransfer.effectAllowed = 'move';
	}

	/** Versions (#7901) gestures on a row: a stack-panel member dropped to copy
	 * and unstack, or a Shift drag that stacks onto this row. They own the drop,
	 * so the body's entries move never sees it. A plain move is the body's. */
	function onRowDragOver(event: DragEvent, row: BrowserRow): void {
		if (worksView.stackDrag !== null && stackList !== null) {
			event.preventDefault();
			event.stopPropagation();
			return;
		}
		if (event.shiftKey && _dragTrackIds.length > 0) {
			event.preventDefault();
			event.stopPropagation();
			dropGap = null;
			stackHintRowId = row.stable_id;
			return;
		}
		stackHintRowId = null;
	}

	function onRowDrop(event: DragEvent, row: BrowserRow): void {
		stackHintRowId = null;
		const stackDrag = worksView.stackDrag;
		if (stackDrag !== null && stackList !== null) {
			event.preventDefault();
			event.stopPropagation();
			_endEntriesDrag();
			worksView.stackDrag = null;
			worksView.modal = {
				kind: 'copy-unstack',
				member: stackDrag.member,
				workId: stackDrag.workId,
				playlistId: stackList,
				position: row.order
			};
			return;
		}
		if (event.shiftKey && _dragTrackIds.length > 0) {
			event.preventDefault();
			event.stopPropagation();
			_endEntriesDrag();
			const dragged = _dragTrackIds;
			_dragTrackIds = [];
			_stackOnto(dragged, row);
		}
	}
</script>

{#snippet sectionRow(header: SectionHeader)}
	<TrackSectionRow
		{header}
		{colCount}
		{rowHeight}
		editable={removable}
		movable={reorderable}
		renaming={renamingSectionId === header.section.id}
		dragged={dragBlock?.section_ids.includes(header.section.id) === true}
		transform={_slotShift(header.slot)}
		onstartrename={() => onsectionrenamestart?.(header.section.id)}
		onrename={(title) => onsectionrename?.(header.section.id, title)}
		oncancelrename={() => onsectionrenamecancel?.()}
		onremove={() => onsectionremove?.(header.section.id)}
		ontoggle={() => void toggleSection(header)}
		oninsertabove={() => onsectioninsert?.(header)}
		ondragstart={(event) => onSectionDragStart(event, header)}
		ondragend={_endEntriesDrag}
	/>
{/snippet}

{#snippet sortableTh(key: SortKey, label: string, col: ColId & LibraryColTipId)}
	<!-- svelte-ignore a11y_click_events_have_key_events -->
	<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
	<th
		class={`h-${key} sortable`}
		style={`width:${colWidths[col]}px`}
		use:columnExplainer={{ text: columnHeaderTitle(col, `Sort by ${label} (asc -> desc -> clear)`) }}
		onclick={(e) => {
			if ((e.target as HTMLElement).closest('.col-resize')) return;
			onsort(key);
		}}
	>
		<span class="th-label">
			<span>{label}</span>
			{#if sortKey === key}
				<SortArrowIcon asc={sortDir === 1} />
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
	{#if reorderHintShown && reorderBlocked !== null}
		<div class="reorder-hint" role="status" data-testid="reorder-blocked-hint">
			<span>{reorderBlocked}</span>
			<button type="button" onclick={_restoreOrder}>Restore playlist order</button>
		</div>
	{/if}
	{#if cellEdit !== null && BpmKeyEditPopover !== null}
		{@const target = cellEdit}
		{#key `${target.row.stable_id}:${target.kind}`}
			<BpmKeyEditPopover
				kind={target.kind}
				anchor={target.anchor}
				initial={cellEditInitial(target)}
				onclose={() => {
					if (cellEdit === target) cellEdit = null;
				}}
				oncommit={async (value) => {
					const decks = { ids: DECK_IDS, states: deckStates, pitchRanges };
					if (typeof value === 'number') await commitBpmEdit(target.row.stable_id, value, decks);
					else await commitKeyEdit(target.row.stable_id, value, decks);
					if (cellEdit === target) cellEdit = null;
				}}
			/>
		{/key}
	{/if}
	<TrackContextMenu
		{oninsertsection}
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
		{readOnly}
		onshowinplaylists={(row, x, y) => {
			playlistsMenu = { x, y, stableId: row.stable_id };
		}}
		onrelocate={(row, x, y) => {
			relocateMenu = { x, y, stableId: row.stable_id, title: row.title };
		}}
		oneditcomment={oncomment ? (row) => openCommentEdit(null, row) : undefined}
	/>
	<!-- TrackPlaylistsPopover and RelocatePopover, fetched by the pick that opens them. -->
	<TrackRowPopovers bind:playlistsMenu bind:relocateMenu {onrelocated} />
	{#if masterFold === 'above'}
		<button
			type="button"
			class="master-fold above"
			style={`left:${masterFoldLeftPx}px`}
			onclick={jumpToMaster}
			title="Master track is above - click to jump"
			bind:clientWidth={masterFoldBadgeWidth}
		>
			<SortArrowIcon asc={true} /> MASTER
		</button>
	{/if}
	{#if masterFold === 'below'}
		<button
			type="button"
			class="master-fold below"
			style={`left:${masterFoldLeftPx}px`}
			onclick={jumpToMaster}
			title="Master track is below - click to jump"
			bind:clientWidth={masterFoldBadgeWidth}
		>
			<SortArrowIcon asc={false} /> MASTER
		</button>
	{/if}
	{#if pairFold !== null}
		<!-- PAIR-09: the master's paired track is off screen. A bare purple
		     bodkin arrow (a cursor head with no tail), beside MASTER when both
		     point off the same edge. -->
		<button
			type="button"
			class="pair-fold {pairFold}"
			data-testid="pair-fold"
			style={`left:${pairFoldLeftPx}px`}
			onclick={jumpToPair}
			title={`Paired track is ${pairFold} - click to jump`}
			aria-label={`Paired track is ${pairFold} - jump to it`}
			bind:clientWidth={pairFoldWidth}
		>
			<svg viewBox="0 0 12 13" width="14" height="15" aria-hidden="true">
				<path d="M6 0.6 L11.4 12.4 L6 9.2 L0.6 12.4 Z" />
			</svg>
		</button>
	{/if}
	{#if pairError !== null}
		<!-- PAIR-09: a failed pairing load leaves the edges unknown. Say so for
		     as long as it lasts, rather than drawing the library as if it had
		     no pairings. -->
		<span class="pair-unknown" role="status" data-testid="pair-unknown" title={pairError}>
			Pairings unavailable
		</span>
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
		style:--tt-gap-ms={`${collapseDuration(gapMotionRows, false)}ms`}
		bind:this={wrapEl}
		onscroll={(e) => {
			const top = e.currentTarget.scrollTop;
			liveScrollTop = top;
			liveScrollLeft = e.currentTarget.scrollLeft;
			onscrollcursor(top);
		}}
	>
		<!-- Keyboard + screen readers: a grid of rows with roving tabindex (one
		     row is the Tab stop; arrows move it). Virtualized, so aria-rowcount /
		     aria-rowindex give the full list size and each row's place in it
		     (header row is 1). See track-table-keyboard.ts. -->
		<table
			data-testid="track-table"
			class={hiddenColumnClasses(uiPrefs.library_hidden_columns)}
			class:rb-pair-pulses={pairPulses}
			class:rb-multi-selected={selectedOrderSet.size > 1}
			style={`width:${tableWidthPx}px`}
			role="grid"
			aria-label="Tracks"
			aria-multiselectable="true"
			aria-rowcount={slotCount + 1}
		>
			<colgroup>
				<col style={`width:${colWidths.funnel}px`} />
				<col style={`width:${colWidths.err}px`} />
				<col style={`width:${colWidths.cloud}px`} />
				<col style={`width:${colWidths.order}px`} />
				{#if autoPlayMode !== 'off'}<col style={`width:${colWidths.autoplay}px`} />{/if}
				{#if !hiddenCols.has('preview')}<col style={`width:${colWidths.preview}px`} />{/if}
				{#if !hiddenCols.has('art')}<col style={`width:${colWidths.art}px`} />{/if}
				<col style={`width:${colWidths.title}px`} />
				{#if !hiddenCols.has('artist')}<col style={`width:${colWidths.artist}px`} />{/if}
				{#if !hiddenCols.has('key')}<col style={`width:${colWidths.key}px`} />{/if}
				{#if !hiddenCols.has('bpm')}<col style={`width:${colWidths.bpm}px`} />{/if}
				{#if !hiddenCols.has('tempostab')}<col style={`width:${colWidths.tempostab}px`} />{/if}
				{#if !hiddenCols.has('plays')}<col style={`width:${colWidths.plays}px`} />{/if}
				<col style={`width:${colWidths.last}px`} />
				{#if !hiddenCols.has('rating')}<col style={`width:${colWidths.rating}px`} />{/if}
				{#if !hiddenCols.has('comments')}<col style={`width:${colWidths.comments}px`} />{/if}
				{#if !hiddenCols.has('time')}<col style={`width:${colWidths.time}px`} />{/if}
				{#if !hiddenCols.has('quality')}<col style={`width:${colWidths.quality}px`} />{/if}
				{#if !hiddenCols.has('energy')}<col style={`width:${colWidths.energy}px`} />{/if}
				{#if !hiddenCols.has('genre')}<col style={`width:${colWidths.genre}px`} />{/if}
				{#if !hiddenCols.has('stems')}<col style={`width:${colWidths.stems}px`} />{/if}
				{#if !hiddenCols.has('tags')}<col style={`width:${colWidths.tags}px`} />{/if}
				{#each typeCols as col (col.id)}<col style={`width:${col.width}px`} />{/each}
				{#if showLyricsCol}
					<col style={`width:${colWidths.lyrics}px`} />
				{/if}
				{#each optionalCols as col (col)}<col style={`width:${colWidths[col]}px`} />{/each}
			</colgroup>
			<thead bind:clientHeight={theadHeightPx} oncontextmenu={(e) => trackContextMenu?.openColumnChooser(e, columnChooserPrefs)}>
				<tr aria-rowindex={1}>
					<th
						class="h-icon"
						style={`width:${colWidths.funnel}px`}
						use:columnExplainer={{ text: plannedTitle('track-table-filter') }}
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
					<!-- svelte-ignore a11y_click_events_have_key_events -->
					<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
					<th
						class="h-icon h-err"
						class:sorted={sortKey === 'grid'}
						style={`width:${colWidths.err}px`}
						use:columnExplainer={{
							text: 'Err - detected analysis data-quality issues, hover a square for detail. Orange: uneven beatgrid, Beat Sync may wander. Gray: variable tempo. Hollow: beatgrid not checked. Click to sort flagged beatgrids to the top (asc -> desc -> clear).'
						}}
						onclick={(e) => {
							if ((e.target as HTMLElement).closest('.col-resize')) return;
							onsort('grid');
						}}
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
								'Key - sort by key (asc -> desc -> clear)'
							)
						}}
						onclick={(e) => {
							if ((e.target as HTMLElement).closest('.col-resize')) return;
							onsort('key');
						}}
					>
						<span class="th-label">
							<!-- Always the column's name, never the master's value (the maintainer, Tue 27 Oct 2026: "clarity"). -->
							<span>Key</span>
							{#if sortKey === 'key'}
								<SortArrowIcon asc={sortDir === 1} />
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
								'BPM - sort by BPM (asc -> desc -> clear)'
							)
						}}
						onclick={(e) => {
							if ((e.target as HTMLElement).closest('.col-resize')) return;
							onsort('bpm');
						}}
					>
						<span class="th-label">
							<span>BPM</span>
							{#if sortKey === 'bpm'}
								<SortArrowIcon asc={sortDir === 1} />
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
					<!-- Issue #7879: the daemon's tempo-stability words; nothing computed here. -->
					<th
						class="h-tempostab"
						style={`width:${colWidths.tempostab}px`}
						title="BPM Stability - does the tempo hold, drift or change through the track (from the beatgrid scan)"
					>
						<span class="th-label"><span>BPM Stability</span></span>
						<!-- svelte-ignore a11y_no_static_element_interactions -->
						<span
							class="col-resize"
							onpointerdown={(e) => onColResizeStart(e, 'tempostab')}
							onpointermove={onColResizeMove}
							onpointerup={onColResizeEnd}
							onpointercancel={onColResizeEnd}
						></span>
					</th>
					<!-- svelte-ignore a11y_click_events_have_key_events -->
					<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
					<th
						class="h-plays"
						class:h-icon={true}
						class:sortable={true}
						style={`width:${colWidths.plays}px`}
						use:columnExplainer={{ text: columnHeaderTitle('plays', 'Sort by plays (asc -> desc -> clear)') }}
						onclick={(e) => {
							if ((e.target as HTMLElement).closest('.col-resize')) return;
							onsort('plays');
						}}
					>
						<span class="th-label"
							><svg class="plays-icon" aria-hidden="true" viewBox="0 0 14 14"
								><path d={PLAY_TRIANGLE_PATH} fill="currentColor" /></svg
							>{#if sortKey === 'plays'}<SortArrowIcon asc={sortDir === 1} />{/if}</span
						>
						<!-- svelte-ignore a11y_no_static_element_interactions -->
						<span
							class="col-resize"
							onpointerdown={(e) => onColResizeStart(e, 'plays')}
							onpointermove={onColResizeMove}
							onpointerup={onColResizeEnd}
							onpointercancel={onColResizeEnd}
						></span>
					</th>
					<th
						class="h-last"
						style={`width:${colWidths.last}px`}
						use:columnExplainer={{
							text: 'Last played, as days ago. Hover a cell for icons showing where the play happened: Rekordbox, a gig in Open DJ, Rekordbox USB, Serato, and any other source we know.'
						}}
					>
						<span class="th-label"><span>Last</span></span>
						<!-- svelte-ignore a11y_no_static_element_interactions -->
						<span
							class="col-resize"
							onpointerdown={(e) => onColResizeStart(e, 'last')}
							onpointermove={onColResizeMove}
							onpointerup={onColResizeEnd}
							onpointercancel={onColResizeEnd}
						></span>
					</th>
					{@render sortableTh('rating', 'Rating', 'rating')}
					{@render sortableTh('comments', 'Comments', 'comments')}
					{@render sortableTh('time', 'Time', 'time')}
					<th
						class="h-quality"
						style={`width:${colWidths.quality}px`}
						use:columnExplainer={{ text: "QLT - audio quality as a letter: A safe on any system, B good on a club system, C thin on a big rig, D rough. It starts from bitrate and container and drops when a scan finds a lossy source behind the file (a brick-wall cut-off) or damage. A hollow letter is not checked yet; a dot means the scan found an issue. Hover a badge for the measurements" }}
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
						<span class="th-label"><svg class="energy-icon" aria-hidden="true" viewBox="0 0 24 24"><path d="M13 2 3 14h7l-1 8 10-12h-7z" /></svg>{#if sortKey === 'energy'}<SortArrowIcon asc={sortDir === 1} />{/if}</span>
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
					<th class="h-tags" style={`width:${colWidths.tags}px`}>
						<span class="th-label"><span>Tags</span></span>
						<!-- svelte-ignore a11y_no_static_element_interactions -->
						<span
							class="col-resize"
							onpointerdown={(e) => onColResizeStart(e, 'tags')}
							onpointermove={onColResizeMove}
							onpointerup={onColResizeEnd}
							onpointercancel={onColResizeEnd}
						></span>
					</th>
					{#each typeCols as col (col.id)}
						<th class="h-tagtype" style={`width:${col.width}px`}>
							<span class="th-label"><span>{col.label}</span></span>
						</th>
					{/each}
					{#if showLyricsCol}
						{@render sortableTh('lyrics', 'Lyrics', 'lyrics')}
					{/if}
					{#each optionalCols as col (col)}
						<th
							class={`h-${col} sortable`}
							style={`width:${colWidths[col]}px`}
							data-testid={`column-${col}`}
							onclick={() => onsort(col)}
						>
							<span class="th-label"><span>{OPTIONAL_COLUMN_LABELS[col]}</span></span>
						</th>
					{/each}
				</tr>
			</thead>
			<!-- svelte-ignore a11y_no_static_element_interactions -->
			<tbody
				bind:this={tbodyEl}
				ondragenter={onBodyDragOver}
				ondragover={onBodyDragOver}
				ondragleave={onBodyDragLeave}
				ondrop={onBodyDrop}
			>
				{#if windowInfo.topPad > 0}
					<tr class="tt-spacer" style={`height:${windowInfo.topPad}px`} aria-hidden="true">
						<td colspan={colCount}></td>
					</tr>
				{/if}
				{#each visibleRows as row, i (row.edit_id ?? row.item_id ?? `${row.stable_id}:${row.order}`)}
					{@const cloudView = trackCloudView({
						fileExists: row.file_exists === true,
						isStreaming: row.is_streaming ?? row.rb_meta?.is_streaming ?? false,
						hasRemoteCopy: row.has_remote_copy === true,
						spotifyPending: row.spotify_pending === true,
						provider: row.streaming_provider,
						folderPath: row.rb_meta?.folder_path ?? null,
						filePath: row.file_path ?? row.rb_meta?.folder_path ?? null,
						fileAvailability: row.file_availability,
						transfer:
							row.cloud_transfer === null || row.cloud_transfer === undefined
								? null
								: {
										direction: row.cloud_transfer.direction,
										bytesTransferred: row.cloud_transfer.bytes_transferred,
										bytesTotal: row.cloud_transfer.bytes_total
									},
						offload: row.offload ?? null
					})}
					{@const minorIssues = minorIssuesFor(row)}
					{@const rowIndex = windowInfo.startIndex + i}
					{@const rowSlot = slotOfRow(sectionHeaders, rowIndex)}
					{@const cellBpm = editedBpm(row.stable_id, row.bpm)}
					{@const cellKey = editedKey(row.stable_id, row.key)}
					{@const bpmEdited = hasBpmEdit(row.stable_id)}
					{@const keyEdited = hasKeyEdit(row.stable_id)}
					{#each sectionHeaders.filter((h) => h.beforeIndex === rowIndex) as header (header.section.id)}
						{@render sectionRow(header)}
					{/each}
					<!-- key: playlists CAN repeat a track, so a playlist row keys on its membership item_id, which a move keeps (REORDER-ROW-02: a drop moves DOM nodes instead of remounting the list); other panes fall back to stable_id:order -->
					<!-- svelte-ignore a11y_click_events_have_key_events -->
					<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
					<!-- Svelte reuses this keyed node across pane switches; observeRow.update() rebinds the WeakMap + observer. Pane identity stays out of the each-key so rows are not remounted and focus is not dropped. -->
					<tr
						use:observeRow={row}
						data-testid="track-row"
						data-canonical-camelot-key={rowCanonicalCamelotKey(row)}
						data-stable-id={row.stable_id}
						data-row-index={rowIndex}
						aria-rowindex={rowSlot + 2}
						aria-selected={selectedOrderSet.has(row.order)}
						tabindex={rowIndex === activeRowIndex ||
						((activeRowIndex < windowInfo.startIndex ||
							activeRowIndex >= windowInfo.endIndex) &&
							i === 0)
							? 0
							: -1}
						draggable="true"
						class:rb-row-selected={selectedOrderSet.has(row.order)}
						class:rb-row-removing={removingRowKeys.has(trackRowKey(row))}
						class:rb-row-restoring={restoringRowKeys.has(trackRowKey(row))}
						class:rb-row-first={windowInfo.topPad === 0 && i === 0}
						class:dblclick-guard-active={dblclickGuardRowIds.has(row.stable_id)}
						class:corridor-grace-active={corridorGraceRowIds.has(row.stable_id)}
						class:rb-row-menu={quickDrawUi.menuHighlightStableId === row.stable_id}
						class:rb-row-mix-compatible={keyCompat(cellKey) &&
							bpmCellCompatibility(cellBpm)?.compatible === true}
						class:rb-row-key-compat={keyCompat(cellKey)}
						class:rb-row-paired={isPairedRow(pairedPartnerIds, row.stable_id)}
						class:rb-row-pair={pairRowState(pairPartners, loadedIds, row.stable_id) !== 'none'}
						class:rb-row-pair-live={pairRowState(pairPartners, loadedIds, row.stable_id) === 'live'}
						class:rb-row-spotify-pending={row.spotify_pending === true ||
							row.stable_id.startsWith('spotify-pending:')}
						class:loaded={loadedIds.has(row.stable_id)}
						class:rb-row-master={masterOrder !== null && row.order === masterOrder}
						class:rb-row-deck-hover={hoverStableId !== null &&
							row.stable_id === hoverStableId}
						class:rb-row-suggest-hover={suggestHoverId !== null &&
							row.stable_id === suggestHoverId}
						class:rb-row-find={findQuery !== '' && rowMatchesFind(row, findQuery)}
						class:broken={rowRendersUnavailable(row)}
						class:rb-row-offloaded={cloudView.kind === 'offloaded'}
						class:rb-row-availability-pending={row.file_availability ===
							'AVAILABILITY_PENDING'}
						title={libraryRowHoverTitle(row)}
						class:rb-row-job={jobProgress.activeFor(row.stable_id) !== null}
						class:rb-row-set-skip={setTargetState.skipIds.has(row.stable_id)}
						style={_jobRowStyle(row.stable_id)}
						style:transform={_slotShift(rowSlot)}
						class:tt-dragged={row.item_id !== null && draggedItems.has(row.item_id)}
						onclick={(event) => onRowPointer(event, row)}
						ondblclick={(e) => onRowDblClick(e, row)}
						oncontextmenu={(e) => trackContextMenu?.open(e, row)}
						onkeydown={(e) => onTrackKeydown(e, row)}
						ondragstart={(e) => onRowDragStart(e, row)}
						onpointerdown={(e) => (_rowPress = markRowPress(e, trackRowKey(row)))}
						ondragend={(e) => onRowDragEnd(e, row)}
						ondragover={(e) => onRowDragOver(e, row)}
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
								>&gt;</span>
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
							<AnalysisDotsPopover
								issues={_issuesFor(row)}
								mode="issues"
								stableId={row.stable_id}
								title={errColumnTitle(row)}
								gridFlag={gridFlagFor(row)}
								sourceFlags={sourceFileFlagsFor(row)}
								ongridflagdismiss={(dismissed) => _setGridFlagDismissed(row, dismissed)}
							/>
						</td>
						<!-- CloudSync presence, local availability, and transfer bytes are
						     separate backend facts; this cell never guesses a percentage. -->
						<td class="c-cloud">
							{#if cloudView.showIcon}
								<span class="cloud-state-wrap" data-cloud-state={cloudView.kind}>
									<CloudStatusIcon view={cloudView} />
									<MinorIssueSquare issues={minorIssues} />
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
									{@const hop = hopByFrom.get(row.stable_id)}
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
											arrowDirection={hop?.direction ?? 0}
											arrowColor={hop?.color ?? null}
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
								strip={row.strip ?? previewStripById[row.stable_id] ?? filledStrips[row.stable_id] ?? null}
								stripLoading={stripLoadingById[row.stable_id] ?? false}
								vocals={vocalsById[row.stable_id] ?? null}
								markerAnlz={markerAnlzById[row.stable_id] ?? null}
								duration_ms={row.duration_ms}
								tempo={uiPrefs.show_tempo_lane ? (row.tempo_curve ?? null) : null}
								tempoTitle={row.grid_quality?.tempo?.title ?? null}
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
								{#if _showArtworkImg(row.stable_id, row.artwork_available)}
									<img
										src={_retrySrc(artworkUrl(row.stable_id, 's'), row.stable_id)}
										alt=""
										loading="lazy"
										data-expected-resource-error="artwork-404"
										onload={(e) => _onArtworkLoad(e, row.stable_id)}
										onerror={() => _onArtworkError(row.stable_id)}
									/>
								{/if}
							{/if}
						</td>
						<td class="c-title" class:rb-row-loaded={playedIds.has(row.stable_id)} onpointerleave={(e) => _onDeckTriggerPointerLeave(e, row)}>
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
							<!-- LIBUX-57: the full title shows on hover only when it is cut off. -->
							<span class="title-line"><span class="title-text" data-custom-tip="" use:truncationTip={shownTitle(row)}
								>{#each hl(shownTitle(row)) as part, i (i)}{#if part.hit}<mark
											class="find-hit">{part.text}</mark
										>{:else}{part.text}{/if}{/each}</span
							>
							{#if row.is_edit}
								<span class="edit-badge" data-testid="edit-badge" title={row.edit_label ?? 'Virtual edit'}>
									{editTime(row.in_ms)}-{editTime(row.out_ms)} / {editFx(row)}
								</span>
								<button class="edit-action" title="Edit virtual edit" onclick={(e) => { e.stopPropagation(); void updateVirtualEdit(row); }}>E</button>
								<button class="edit-action" title="Delete virtual edit" onclick={(e) => { e.stopPropagation(); void deleteVirtualEdit(row); }}>x</button>
							{:else}
								<button class="edit-action create-edit" title="Create virtual edit" onclick={(e) => { e.stopPropagation(); void createVirtualEdit(row); }}>cut</button>
							{/if}
							{#if indicatorFor(row) !== null}
								<button
									type="button"
									class="stack-indicator"
									data-testid="stack-indicator"
									aria-expanded={stackOpenId === row.stable_id}
									aria-label="Versions"
									onclick={(e) => {
										e.stopPropagation();
										stackOpenId = stackOpenId === row.stable_id ? null : row.stable_id;
									}}
									ondblclick={(e) => e.stopPropagation()}
								>{indicatorFor(row)}</button>
							{/if}
							{#if viaWork !== null && viaWork.has(row.stable_id)}
								<span class="filed-under" data-testid="via-work">via work</span>
							{:else if !optionalCols.includes('work')}
								<span class="filed-under">{filedUnderText(worksView.overlay, row.stable_id)}</span>
							{/if}
							</span>
						</td>
						<td class="c-artist" class:rb-row-loaded={playedIds.has(row.stable_id)} title={row.artist ?? ''}>
							{#each hl(shownArtist(row)) as part, i (i)}
								{#if part.hit}<mark class="find-hit">{part.text}</mark>{:else}{part.text}{/if}
							{/each}
						</td>
						<td
							class="c-key"
							class:set-clash-cell={clashEdges.has(row.stable_id)}
							class:key-compat={keyCompat(cellKey)}
							class:key-inert={keyCellInert(row) && !keyEdited}
							class:key-unparsed={displayKey(cellKey, uiPrefs.key_notation)?.parsed === false}
							style={keyCompatStyle(cellKey)}
							aria-label={`${keyCellTitle(row)} Double-click, or right-click and choose Edit, to change it.`}
							data-custom-tip=""
							onmouseenter={(e) => _onKeyCellEnter(e, row)}
							onmouseleave={() => (keyHover = null)}
							ondblclick={(e) => openCellEdit(e, row, 'key')}
							oncontextmenu={(e) => openCellEditMenu(e, row, 'key')}
						>
							{#if clashEdges.has(row.stable_id)}<span class="set-clash-edge" aria-hidden="true"></span>{/if}
							{#if !keyEdited && (row.key === null || row.key === undefined) && row.key_status === 'failed' && row.key_guess}
								<!-- KEY-GUESS-01: the analyzer's best candidate, dimmed, never a confirmed key. -->
								<span class="key-guess">{displayKey(row.key_guess, uiPrefs.key_notation)?.text ?? row.key_guess}?</span>
							{:else if !keyEdited && (row.key === null || row.key === undefined) && (row.key_status === 'failed' || row.key_status === 'missing')}
								<!-- KEY-CELL-01: no status word in the cell; the reason is on the hover and in the Err column. -->
								<span class="key-status" aria-hidden="true"></span>
							{:else}
								{#snippet keyCharacters(text: string)}
									{#each text as character}<span class:camelot-suffix={uiPrefs.key_notation === 'camelot' && (character === 'A' || character === 'B')}>{character}</span>{/each}
								{/snippet}
								{@const match = keyMatch(cellKey)}
								{#if match !== null && match.shift !== 0}
									<!-- KEYSHIFT-03: the key it will play in (in the chosen notation), plus the load shift. -->
									{@render keyCharacters(shiftedKeyText(match.key, uiPrefs.key_notation))}<sup class="key-shift-sup">{keyShiftSuperscript(match.shift)}</sup>
								{:else}
									{#each keyCellSpans(cellKey, uiPrefs.key_notation, findQuery) as part, i (i)}
										{#if part.hit}<mark class="find-hit">{@render keyCharacters(part.text)}</mark>{:else}{@render keyCharacters(part.text)}{/if}
									{/each}
								{/if}
							{/if}
						</td>
						<td
							class="c-bpm"
							class:set-clash-cell={clashEdges.has(row.stable_id)}
							class:bpm-sweet={bpmCellHeat(cellBpm)?.lane === 'sweet'}
							class:bpm-half={bpmCellHeat(cellBpm)?.lane === 'half'}
							class:bpm-compatible={bpmCellCompatibility(cellBpm)?.compatible === true}
							class:bpm-warn={bpmCellCompatibility(cellBpm)?.severity === 'warn'}
							class:bpm-danger={bpmCellCompatibility(cellBpm)?.severity === 'danger'}
							class:bpm-critical={bpmCellCompatibility(cellBpm)?.severity === 'critical'}
							class:bpm-far={bpmCellHeat(cellBpm)?.lane === 'far'}
							class:bpm-inert={bpmCellInert(row) && !bpmEdited}
							style={bpmCellStyle(cellBpm)}
							title={row.bpm_disputed ? undefined : `${bpmCellTitle(row)} Double-click, or right-click and choose Edit, to change it.`}
							onpointerenter={() => requestGridProvenance(row.stable_id)}
							ondblclick={(e) => openCellEdit(e, row, 'bpm')}
							oncontextmenu={(e) => openCellEditMenu(e, row, 'bpm')}
						>
							{#if clashEdges.has(row.stable_id)}<span class="set-clash-edge" aria-hidden="true" title={clashEdges.get(row.stable_id)}></span>{/if}
							{#if bpmEdited}
								{bpmCellText({ bpm: cellBpm, bpm_status: 'ok' }).text}
							{:else if bpmCellText(row).kind === 'value'}
								{bpmCellText(row).text}{#if row.bpm_disputed}<!-- svelte-ignore a11y_no_static_element_interactions --><span
										class="bpm-dispute"
										data-custom-tip=""
										data-testid="bpm-dispute-marker"
										role="img"
										aria-label="tempo disputed with Mixed In Key"
										onmouseenter={(e) => openBpmDispute(e, row)}
										onmouseleave={scheduleBpmDisputeClose}
									><BpmDisputeMarker /></span>{/if}
							{:else}
								<!-- KEY-CELL-01: no status word in the cell; the reason is on the hover and in the Err column. -->
								<span class="bpm-status" title={bpmCellTitle(row)} aria-hidden="true"></span>
							{/if}
						</td>
						<td class="c-tempostab" title={bpmStabilityTitle(row.grid_quality)}><BpmStabilityCell grid={row.grid_quality} /></td>
						<td
							class="c-plays"
							title="play count: rekordbox plays plus Open DJ plays heard for 60 s or more"
						>{row.play_count > 0 ? String(row.play_count) : ''}</td>
						<td class="c-last">
							<LastPlayedCell
								stableId={row.stable_id}
								playCount={row.play_count}
								wire={row.plays ?? null}
							/>
						</td>
						<!-- The cell hands its own width to CSS so the stars can
						     tighten then shrink to fit it (pin 8f60606750c6). -->
						<td class="c-rating" style={`--rating-w:${colWidths.rating}px`}>
							<RatingStars rating={row.rating} onrate={(n) => onrate(row, n)} />
						</td>
						<td
							class="c-comments"
							class:c-comments-editing={commentEditOrder === row.order}
							title={row.comments ?? ''}
							ondblclick={(e) => openCommentEdit(e, row)}
						>
							{#if commentEditOrder === row.order}
								<CommentCellEditor
									initial={row.comments ?? ''}
									oncommit={(next) => saveComment(row, next)}
									oncancel={() => {
										if (commentEditOrder === row.order) commentEditOrder = null;
									}}
								/>
							{:else}
								{#each hl(row.comments) as part, i (i)}
									{#if part.hit}<mark class="find-hit">{part.text}</mark>{:else}{part.text}{/if}
								{/each}
							{/if}
						</td>
						<td class="c-time" title={timeCellTitle(row.duration_ms, lengthFlagText(row))}>{_fmtTime(row.duration_ms)}</td>
						<td class="c-quality">
							<QualityBadge
								quality={row.quality}
								trackKey={row.stable_id}
								compact
								showContainer={false}
								loadHover={row.quality?.grade?.letter ? () => audioQualityHover(row.stable_id, JSON.stringify(row.quality?.grade)) : undefined}
							/>
						</td>
						<td class="c-energy" class:energy-unset={row.energy === null} title={row.energy_reason}>
							{row.energy ?? ''}
						</td>
						<!-- LIBUX-36: a missing genre is a blank cell; why it is missing
						     (STANDALONE-05) is the hover title, never cell text. -->
						<!-- GENRE-04: the hover names the provider the shown genre came from. -->
						<td
							class="c-genre"
							title={genreCellTitle(genreText(row), row.genre_source, row.genre_reason)}
							data-genre-source={row.genre_source ?? undefined}
						>
							{#if splitGenreTags(genreText(row)).length > 0}
								{#each splitGenreTags(genreText(row)) as tag, i (tag + String(i))}
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
							{:else if row.genre_guess}
								<span
									class="genre-guess"
									title={`JEV guess, ${Math.round(row.genre_guess.confidence * 100)}% sure; not a file tag`}
									>{row.genre_guess.family}?</span
								>
							{/if}
						</td>
						<td class="c-stems">
							<StemTags stems={row.stems} />
							<VocalAnalyzeButton stableId={row.stable_id} stems={row.stems} />
						</td>
						<td class="c-tags">
							<span class="tags-cell-text">{formatTagsCell(tagsCellFor(row.stable_id))}</span>
						</td>
						{#each typeCols as col (col.id)}
							<td class="c-tagtype">
								<TagCell stableId={row.stable_id} {selectedIds} typeId={col.id} />
							</td>
						{/each}
						{#if showLyricsCol}
							<LyricColumn {row} />
						{/if}
						{#each optionalCols as col (col)}
							<td class={`c-optional c-${col}`} title={optionalCell(row, col)}>{optionalCell(row, col)}</td>
						{/each}
					</tr>
				{/each}
				{#if windowInfo.endIndex === rows.length}
					{#each sectionHeaders.filter((h) => h.beforeIndex === rows.length) as header (header.section.id)}
						{@render sectionRow(header)}
					{/each}
				{/if}
				{#if windowInfo.bottomPad > 0}
					<tr class="tt-spacer" style={`height:${windowInfo.bottomPad}px`} aria-hidden="true">
						<td colspan={colCount}></td>
					</tr>
				{/if}
			</tbody>
		</table>
		{#if stackOpenId !== null && stackOpenIndex >= 0}
			<VersionStackPanel
				stableId={stackOpenId}
				playlistId={stackList}
				rowPosition={rows[stackOpenIndex]?.order}
				top={TRACK_TABLE_THEAD_PX + (stackOpenIndex + 1) * rowHeight}
				onclose={() => (stackOpenId = null)}
			/>
		{/if}
		<WorksModals bind:this={worksModals} />
		{#if rows.length === 0 && emptyMessage !== null}
			<div class="empty">
				{emptyMessage}{#if onemptyretry !== undefined}
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
				{#each apCurveSegments as seg (`${seg.from.stable_id}-${seg.to.stable_id}`)}
					{@const color = hopByFrom.get(seg.from.stable_id)?.color ?? 'var(--rb-accent)'}
					{@const head = arrowHead(seg.from.y, seg.to.y, apCurveX)}
					<path
						d={segmentPath(seg, apCurveX, head.baseY)}
						class="ap-curve-seg"
						class:skips={seg.skips}
						fill="none"
						style={`stroke:${color}`}
					/>
					{#if head.points !== ''}
						<polygon points={head.points} class="ap-curve-head" style={`fill:${color}`} />
					{/if}
					<circle
						cx={apCurveX}
						cy={seg.from.y}
						r={hoveredApId === seg.from.stable_id ? 3.5 : 2}
						class="ap-curve-node"
						class:hot={hoveredApId === seg.from.stable_id}
						style={`fill:${color}`}
					/>
					<circle
						cx={apCurveX}
						cy={seg.to.y}
						r={hoveredApId === seg.to.stable_id ? 3.5 : 2}
						class="ap-curve-node"
						class:hot={hoveredApId === seg.to.stable_id}
						style={`fill:${hopByFrom.get(seg.to.stable_id)?.color ?? color}`}
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

{#if keyHover !== null && KeyHoverCard !== null}
	{@const hoverMatch = keyMatch(keyHover.row.key)}
	<KeyHoverCard
		ownKey={keyHover.row.key}
		shift={hoverMatch?.shift ?? 0}
		shiftedKey={hoverMatch?.key ?? null}
		note={keyHoverNote(keyHover.row)}
		anchor={keyHover.anchor}
		onDismiss={() => (keyHover = null)}
	/>
{/if}

{#if bpmDispute !== null}
	{@const disputed = bpmDispute.row}
	<BpmDisputeCard
		alternatives={disputed.bpm_alternatives ?? []}
		shownBpm={disputed.bpm}
		confidence={disputed.bpm_confidence}
		note={`${bpmCellTitle(disputed)} Double-click, or right-click and choose Edit, to change it.`}
		anchor={bpmDispute.anchor}
		onpick={(alt) => pickBpmAlternative(disputed, alt)}
		onerror={onkeycardfailed}
		onenter={cancelBpmDisputeClose}
		onleave={scheduleBpmDisputeClose}
		onDismiss={() => (bpmDispute = null)}
	/>
{/if}

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
	.title-line {
		display: flex;
		align-items: baseline;
		min-width: 0;
	}
	.title-line .title-text {
		flex: 0 1 auto;
		min-width: 0;
	}
	.edit-badge {
		flex: none;
		margin-left: 5px;
		padding: 0 4px;
		border: 1px solid #2f9d62;
		border-radius: 3px;
		color: #62d993;
		font-size: 9px;
		line-height: 14px;
		white-space: nowrap;
	}
	.edit-action {
		flex: none;
		margin-left: 2px;
		padding: 0 2px;
		border: 0;
		background: transparent;
		color: #62d993;
		cursor: pointer;
		font: inherit;
	}
	.create-edit { opacity: 0; }
	.c-title:hover .create-edit,
	.create-edit:focus-visible { opacity: 1; }
	.stack-indicator {
		flex: none;
		background: none;
		border: 0;
		padding: 0 3px;
		margin: 0;
		color: inherit;
		font: inherit;
		opacity: 0.55;
		cursor: pointer;
	}
	.stack-indicator:hover,
	.stack-indicator:focus-visible {
		opacity: 1;
	}
	.filed-under {
		flex: 1 1 0;
		min-width: 0;
		overflow: hidden;
		white-space: nowrap;
		text-overflow: ellipsis;
		margin-left: 6px;
		opacity: 0.45;
		font-size: 0.9em;
	}
	.c-optional {
		overflow: hidden;
		white-space: nowrap;
		text-overflow: ellipsis;
	}

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
		stroke-width: 2;
		opacity: 0.92;
	}
	.ap-curve-seg.skips {
		stroke-dasharray: 3 3;
		opacity: 0.72;
	}
	.ap-curve-node {
		opacity: 0.9;
	}
	.ap-curve-node.hot {
		opacity: 1;
	}
	.ap-curve-head {
		stroke: var(--rb-bg);
		stroke-width: 0.75;
		stroke-linejoin: round;
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
		cursor: pointer;
	}
	.h-err.sorted {
		color: var(--rb-accent, #3d7dd9);
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

	tbody tr.tt-dragged {
		opacity: 0.4;
	}
	/* The animated gap of a drag (#7605). Transform only, and it is the row's one
	 * transition, so nothing re-lays-out while the pointer moves. */
	tbody tr[data-testid='track-row'] {
		transition: transform var(--tt-gap-ms) ease-out;
	}
	@media (prefers-reduced-motion: reduce) {
		.table-wrap {
			--tt-gap-ms: 0ms !important;
		}
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
		z-index: 4;
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
	/* Square, the size and corner of an AnalysisDots grid cell it sits on
	   (LIBM-177): a round dot read as foreign to the grid. */
	.audio-cache-dot {
		position: absolute;
		top: 4px;
		left: 3px;
		z-index: 2;
		width: 4px;
		height: 4px;
		border-radius: 0.5px;
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
	/* Subtle TL-to-BR white sweep (same 135deg idea as Deck.svelte
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
	/* LIBUX-8626: a removed row shows red and folds into its neighbour over
	 * ROW_REMOVE_ANIMATION_MS (pane-row-removal.ts) before the splice. */
	tbody tr.rb-row-removing {
		pointer-events: none;
	}
	tbody tr.rb-row-removing > td {
		animation: rb-row-remove 200ms ease-in forwards;
		transform-origin: top;
	}
	/* LIBUX-8676: the reverse for an undo - the row unfolds green over
	 * ROW_RESTORE_ANIMATION_MS and settles into its normal (selected) look. */
	tbody tr.rb-row-restoring > td {
		animation: rb-row-restore 200ms ease-out;
		transform-origin: top;
	}
	@keyframes rb-row-restore {
		0% {
			background-color: var(--rb-green);
			opacity: 0;
			transform: scaleY(0);
		}
		60% {
			background-color: var(--rb-green);
			opacity: 1;
			transform: scaleY(1);
		}
	}
	/* LIBUX-8676 (the maintainer, Wed 4 Nov 2026): several tracks cannot load into one
	 * deck, so the 1/2/3/4 load box never appears while more than one is
	 * selected. visibility, not display: the box keeps its layout (the
	 * single-select reveal stays an opacity fade), and a hidden box's buttons
	 * take neither the pointer nor Tab focus. */
	table.rb-multi-selected .deck-btns {
		visibility: hidden;
	}
	@keyframes rb-row-remove {
		0% {
			background-color: var(--rb-red);
		}
		100% {
			background-color: var(--rb-red);
			opacity: 0;
			transform: scaleY(0);
		}
	}
	/* Keyboard focus (roving tabindex, track-table-keyboard.ts): the active
	 * row a screen reader announces and arrows move from. */
	tbody tr:focus-visible {
		outline: 1px solid var(--rb-accent);
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
		--rb-row-master-edge:
			inset 5px 0 0 var(--rb-master),
			inset -1px 0 0 color-mix(in srgb, var(--rb-master) 55%, transparent);
		background: color-mix(in srgb, var(--rb-master) 28%, transparent);
		box-shadow: var(--rb-row-master-edge);
		outline: 1px solid color-mix(in srgb, var(--rb-master) 70%, transparent);
		outline-offset: -1px;
	}
	tbody tr.rb-row-master:hover {
		background: color-mix(in srgb, var(--rb-master) 36%, var(--rb-panel-raised));
	}
	tbody tr.rb-row-master .c-title,
	tbody tr.rb-row-master .c-artist {
		color: var(--rb-master-text);
		font-weight: 700;
	}
	/* Hovered deck's library track (non-master): light pulse to help find it. */
	tbody tr.rb-row-suggest-hover {
		outline: 1px solid #ff4da6;
		background: color-mix(in srgb, #ff4da6 16%, transparent) !important;
	}
	/* DECKUX-5917: a hovered deck's library row gets the same static left-edge tick
	 * as the deck and waveform row. The !important beats the loaded edge; a master
	 * row keeps its gold edge under the tick (--rb-row-master-edge). This rule
	 * follows tbody tr.rb-row-job, which zeroes the shadow with !important at the
	 * same specificity, so the later rule wins. */
	tbody tr.rb-row-deck-hover {
		box-shadow: var(--rb-deck-link-tick), var(--rb-row-master-edge, inset 0 0 0 transparent) !important;
	}
	/* IOPIN-11 + LIBM-186: a row is marked only when both Camelot key and the
	 * closest raw/half/double BPM relationship are compatible. LIBM-186 (the maintainer,
	 * Tue 3 Nov 2026: "less garish ... a 70-80% height highlight on just the LHS
	 * border") replaced the full-row green wash with a thin, vertically centred
	 * bar on the row's left edge. Every row state that owns the left edge or the
	 * row fill (selected, menu, loaded, master, deck hover, job, Spotify pending)
	 * keeps its own look, so the bar never sits on top of one. Skin hook:
	 * --rb-mix-compat-bar (theme.css); the default mixes the skin's green with
	 * its dim text so it stays muted on dark and light skins alike.
	 * LIBM-8699: the bar rides the FIRST CELL's ::before (every tbody td is
	 * position: relative), never the row's own ::before. Chrome and WebKit lay
	 * a <tr>'s ::before out as a leading table cell even when it is absolutely
	 * positioned, so every compatible row rendered one column right: the order
	 * number under Preview, the strip under artwork, the cover stretched over
	 * the title (the PAIR-09 bug, 9f1ba66f9e, back through this rule). */
	tbody tr.rb-row-mix-compatible:not(.rb-row-selected):not(.rb-row-menu):not(.loaded):not(.rb-row-master):not(
			.rb-row-spotify-pending
		):not(.rb-row-deck-hover):not(.rb-row-job) > td.c-funnel::before {
		content: '';
		position: absolute;
		left: 0;
		top: 12.5%;
		width: 3px;
		height: 75%;
		z-index: 3;
		border-radius: 0 1.5px 1.5px 0;
		background: var(--rb-mix-compat-bar, color-mix(in srgb, var(--rb-green) 55%, var(--rb-text-dim)));
		pointer-events: none;
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
		border: 1px solid var(--rb-master);
		border-radius: 3px;
		background: var(--rb-master);
		color: var(--rb-master-ink);
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
	/* LIBUX-35: every cell clips to its own column. A column may only set
	 * `overflow: visible` when an inner element clips its text instead
	 * (.c-title -> .title-text); tests/unit/track-table-cell-clip.test.mjs
	 * holds that list. `:global(td)` because a cell can be rendered by a
	 * child component (LyricColumn's td never matched a scoped `td`, so it
	 * did not clip either); `:where()` keeps the rule at a bare td's
	 * specificity so every `.c-*` column rule below still overrides it. */
	:where(tbody > tr) > :global(td) {
		padding: 0 var(--tt-td-pad-x);
		border-bottom: none;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		vertical-align: middle;
	}
	/* position without a z-index on purpose: `z-index: 0` here made every
	 * cell its own stacking context (#4009), which trapped a cell's
	 * `position: fixed` popovers (the analysis-dots hover tile, z-index 9600)
	 * at that cell's level, so every LATER row's cells painted over them and
	 * took their pointer events. The row separator (`tbody tr::after`,
	 * z-index 1) still paints above z-index:auto cells. */
	tbody td {
		position: relative;
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
	.c-bpm {
		cursor: pointer;
	}
	.h-order .th-label {
		justify-content: center;
		padding: 0 2px;
	}
	.h-plays .th-label {
		justify-content: center;
	}
	/* The header glyph has a viewBox and no intrinsic size, so without an
	 * explicit box it stretches to fill its flex label. */
	.plays-icon {
		flex: 0 0 auto;
		width: 10px;
		height: 10px;
	}
	.c-plays {
		font-size: 10px;
	}
	.c-last {
		font-size: 10px;
		color: var(--rb-text-dim);
		padding-left: 4px;
		padding-right: 4px;
	}
	.h-last .th-label {
		font-size: 9px;
		letter-spacing: 0.02em;
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
	 * STAR_ADV (1.2em) is the filled star glyph's advance, which is wider than 1em; using
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
	.reorder-hint {
		position: absolute;
		top: 24px;
		left: 50%;
		transform: translateX(-50%);
		z-index: 20;
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 4px 8px;
		border: 1px solid var(--rb-border, rgba(255, 255, 255, 0.18));
		border-radius: 3px;
		background: var(--rb-panel-raised);
		color: var(--rb-text);
		font-size: 11px;
		white-space: nowrap;
		box-shadow: 0 2px 8px rgba(0, 0, 0, 0.45);
	}
	.reorder-hint button {
		padding: 1px 6px;
		border: 1px solid var(--rb-accent);
		border-radius: 2px;
		background: none;
		color: var(--rb-text);
		font: inherit;
		cursor: pointer;
	}
	.c-key {
		color: var(--rb-text-dim);
		box-sizing: border-box;
		cursor: pointer;
	}
	/* Key and BPM are auto-fit to their widest text in the skin font
	 * (musical-column-fit.ts), so a narrower column is a user drag: clip it,
	 * never trade two characters for an ellipsis (Gothic showed "8..."). */
	.c-key,
	.c-bpm {
		text-overflow: clip;
	}
	.camelot-suffix { font-size: 0.67em; }
	.key-shift-sup {
		margin-left: 1px;
		font-size: 0.68em;
		font-weight: 600;
		line-height: 1;
		color: var(--rb-orange, #e8a13a);
	}
	.c-key.key-compat {
		border-radius: 2px;
		padding-left: 4px;
		padding-right: 4px;
	}
	/* Compatible-key ring as an overlay ABOVE the row separator
	   (tbody tr::after, z-index 4), so all four sides show; an inset
	   box-shadow on the td itself lost its bottom edge under it. The ring is
	   a real 1px border (pixel-snapped on every side, unlike an inset shadow
	   on the half-pixel row edges the collapsed header border leaves) and
	   stops 1px short of the cell bottom, so its bottom edge sits above the
	   row separator instead of on it, and two compatible rows in a row show
	   two rings with the separator between them rather than one doubled
	   line (LIBM-176). A skin may set --rb-key-compat-border (mono-dev:
	   white) over the master key hue. */
	.c-key.key-compat::after {
		content: '';
		position: absolute;
		inset: 0 0 1px 0;
		z-index: 5;
		box-sizing: border-box;
		border: 1px solid var(--rb-key-compat-border, var(--key-compat-color));
		border-radius: 2px;
		pointer-events: none;
	}
	.c-key.key-inert {
		color: var(--rb-text-dim);
		font-size: 0.85em;
		text-transform: lowercase;
	}
	/* A stored key in no notation we parse: shown as stored, never guessed,
	 * and marked so it reads as a data problem rather than a key. */
	.c-key.key-unparsed {
		text-decoration: underline dotted;
		font-style: italic;
	}
	/* KEY-GUESS-01: an ambiguous key's best candidate reads as a guess, not a key. */
	.key-guess {
		opacity: 0.55;
		font-style: italic;
	}
	.key-status,
	.bpm-status {
		/* Parent cells are --rb-text-dim. That gray, times 0.85 opacity, missed
		   WCAG AA on the tinted key/BPM cells (contrast audit, 3.28:1 to 4.33:1).
		   The status word uses primary text so "missing" stays readable. */
		color: var(--rb-text);
		opacity: 1;
	}
	/* Existing heat washes remain secondary. IOPIN-11's border is the
	 * actionable master-relative verdict and uses the nearest raw/half/double
	 * relationship from bpm-heat.ts. */
	.c-bpm.bpm-sweet {
		border-radius: 2px;
		background: color-mix(in srgb, var(--rb-green) 18%, transparent);
	}
	.c-bpm.bpm-half {
		border-radius: 2px;
		background: color-mix(in srgb, #a855f7 12%, transparent);
	}
	.c-bpm.bpm-compatible { box-shadow: inset 0 0 0 1px var(--rb-green); }
	/* Skin hook: Gothic (mono-dev) drops the red ring (--rb-bpm-mismatch-ring,
	 * theme.css); the inline heat colour already dims an off-tempo number. */
	.c-bpm.bpm-warn { box-shadow: inset 0 0 0 1px var(--rb-bpm-mismatch-ring, #d45a4f); }
	.c-bpm.bpm-danger { box-shadow: inset 0 0 0 1px var(--rb-bpm-mismatch-ring, #e14238); }
	.c-bpm.bpm-critical { box-shadow: inset 0 0 0 2px var(--rb-bpm-mismatch-ring, #ff2f25); }
	/* PAIR-09 (the maintainer, Thu 8 Oct 2026): a paired row gets a purple zigzag ring,
	 * "as if being microwaved". the maintainer picked the Cmd K bar's "G . Comet" edge
	 * (Cmd K thread, round 3, same day), so both surfaces draw the same ring:
	 * --rb-pair mixed 62% with --rb-text-dim at 40% opacity, drawn inside the
	 * row on a rect inset 1px, with jags every 5px alternating in and out
	 * (10px period, about 0.8px peak to peak). Nothing draws outside the row,
	 * so stacked pairs never overlap. The ring rides the row's ::after: a fill
	 * cut to the zigzag by four SVG mask tiles, one per side, which replaces
	 * the separator line. Not ::before: Chrome lays a row's ::before out ahead
	 * of its cells and shifts every column one to the right.
	 *
	 * The comet: one pulse runs the whole way round once, clockwise from the
	 * top left, in 0.85 s, fading in and back out along the lap (peak 95%)
	 * with a fading trail behind it, then the ring stays still. It plays when
	 * the row first draws as paired (scrolled into view, or the pairing just
	 * landed) and once more each time the pointer enters the row. Four
	 * gradient layers, one per side, carry the head; registered --pz-*
	 * properties move it, so the animation is finite and costs nothing once
	 * it ends. The master's partners (rb-row-paired) draw a slightly stronger
	 * ring (52%) instead of a cell wash: a 7% purple wash pulled the muted
	 * time column on a loaded light-theme row under AA (4.26:1), while the
	 * ring paints only the row's edges.
	 *
	 * The looping in-row pulses this replaced stay behind the
	 * library.pair_edge_pulses flag (default off), on .rb-pair-pulses. */
	@property --pz-e {
		syntax: '<percentage>';
		inherits: false;
		initial-value: 0%;
	}
	@property --pz-t {
		syntax: '<percentage>';
		inherits: false;
		initial-value: -50%;
	}
	@property --pz-r {
		syntax: '<percentage>';
		inherits: false;
		initial-value: -50%;
	}
	@property --pz-b {
		syntax: '<percentage>';
		inherits: false;
		initial-value: -50%;
	}
	@property --pz-l {
		syntax: '<percentage>';
		inherits: false;
		initial-value: -50%;
	}
	tbody tr.rb-row-pair {
		--pz-ink: color-mix(in srgb, var(--rb-pair) 62%, var(--rb-text-dim));
		--pz-base: color-mix(in srgb, var(--pz-ink) 40%, transparent);
		--pz-hot: color-mix(in srgb, var(--pz-ink) 85%, transparent);
		--pz-h: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 10 3' preserveAspectRatio='none'%3E%3Cpolyline points='0,1.9 5,1.1 10,1.9' fill='none' stroke='%23000' stroke-width='1' vector-effect='non-scaling-stroke'/%3E%3C/svg%3E");
		--pz-v: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 3 10' preserveAspectRatio='none'%3E%3Cpolyline points='1.9,0 1.1,5 1.9,10' fill='none' stroke='%23000' stroke-width='1' vector-effect='non-scaling-stroke'/%3E%3C/svg%3E");
	}
	tbody tr.rb-row-pair::after {
		top: 1px;
		bottom: 1px;
		left: 1px;
		right: 1px;
		height: auto;
		background-color: var(--pz-base);
		mask-image: var(--pz-h), var(--pz-h), var(--pz-v), var(--pz-v);
		mask-repeat: repeat-x, repeat-x, repeat-y, repeat-y;
		mask-size: 10px 3px, 10px 3px, 3px 10px, 3px 10px;
		mask-position: left 0 top 0, left 0 bottom 0, left 0 top 0, right 0 top 0;
		/* The comet head, one layer per side; --pz-e is its fade envelope. */
		--pz-head: color-mix(in srgb, var(--pz-ink) calc(var(--pz-e) * 0.95), transparent);
		--pz-tail: color-mix(in srgb, var(--pz-ink) calc(var(--pz-e) * 0.35), transparent);
		background-image:
			linear-gradient(90deg, transparent calc(var(--pz-t) - 40%), var(--pz-tail) calc(var(--pz-t) - 14%), var(--pz-head) var(--pz-t), transparent var(--pz-t)),
			linear-gradient(180deg, transparent calc(var(--pz-r) - 40%), var(--pz-tail) calc(var(--pz-r) - 14%), var(--pz-head) var(--pz-r), transparent var(--pz-r)),
			linear-gradient(270deg, transparent calc(var(--pz-b) - 40%), var(--pz-tail) calc(var(--pz-b) - 14%), var(--pz-head) var(--pz-b), transparent var(--pz-b)),
			linear-gradient(0deg, transparent calc(var(--pz-l) - 40%), var(--pz-tail) calc(var(--pz-l) - 14%), var(--pz-head) var(--pz-l), transparent var(--pz-l));
		background-size: 100% 50%, 50% 100%, 100% 50%, 50% 100%;
		background-repeat: no-repeat;
		background-position: left top, right top, left bottom, left top;
		animation: pair-comet 0.85s linear;
	}
	/* Entering the row adds a second, identical run; the first one keeps its
	 * place in the list, so leaving the row does not replay it. */
	tbody tr.rb-row-pair:hover::after {
		animation: pair-comet 0.85s linear, pair-comet-again 0.85s linear;
	}
	tbody tr.rb-row-pair.rb-row-paired::after {
		--pz-base: color-mix(in srgb, var(--pz-ink) 52%, transparent);
	}
	/* A wide row is mostly its two long sides, so the head spends 48% of the
	 * lap on each and 2% on each end. Each side's head runs on past its end
	 * so the trail drains out, and sin(pi p) is the fade envelope. */
	@keyframes pair-comet {
		0% { --pz-t: 0%; --pz-r: -50%; --pz-b: -50%; --pz-l: -50%; --pz-e: 0%; }
		25% { --pz-e: 71%; }
		48% { --pz-t: 100%; --pz-r: 0%; }
		50% { --pz-r: 100%; --pz-b: 0%; --pz-e: 100%; }
		62% { --pz-t: 145%; --pz-r: 145%; }
		75% { --pz-e: 71%; }
		98% { --pz-b: 100%; --pz-l: 0%; }
		100% { --pz-t: 145%; --pz-r: 145%; --pz-b: 104%; --pz-l: 100%; --pz-e: 0%; }
	}
	@keyframes pair-comet-again {
		0% { --pz-t: 0%; --pz-r: -50%; --pz-b: -50%; --pz-l: -50%; --pz-e: 0%; }
		25% { --pz-e: 71%; }
		48% { --pz-t: 100%; --pz-r: 0%; }
		50% { --pz-r: 100%; --pz-b: 0%; --pz-e: 100%; }
		62% { --pz-t: 145%; --pz-r: 145%; }
		75% { --pz-e: 71%; }
		98% { --pz-b: 100%; --pz-l: 0%; }
		100% { --pz-t: 145%; --pz-r: 145%; --pz-b: 104%; --pz-l: 100%; --pz-e: 0%; }
	}
	/* library.pair_edge_pulses: the earlier looping pulses on live rows (a
	 * partner on a deck), three 85% pulses about 80px long, 443px apart,
	 * lapping clockwise at 260px/s while the jag depth swells a little. */
	.rb-pair-pulses tbody tr.rb-row-pair-live::after {
		/* One pulse per 443px on each side, moving with the clockwise lap. */
		background-image:
			linear-gradient(90deg, transparent 363px, var(--pz-hot) 363px 443px),
			linear-gradient(270deg, transparent 363px, var(--pz-hot) 363px 443px),
			linear-gradient(180deg, transparent 363px, var(--pz-hot) 363px 443px),
			linear-gradient(0deg, transparent 363px, var(--pz-hot) 363px 443px);
		background-size: 443px 50%, 443px 50%, 50% 443px, 50% 443px;
		background-repeat: repeat-x, repeat-x, repeat-y, repeat-y;
		background-position: left 0 top 0, left 0 bottom 0, left 0 top 0, right 0 top 0;
		animation: pair-pulse 1.7s linear infinite;
	}
	@keyframes pair-pulse {
		0% { background-position: left 0 top 0, left 0 bottom 0, left 0 top 0, right 0 top 0; mask-size: 10px 3px, 10px 3px, 3px 10px, 3px 10px; }
		50% { mask-size: 10px 3.6px, 10px 3.6px, 3.6px 10px, 3.6px 10px; }
		100% { background-position: left 443px top 0, left -443px bottom 0, left 0 top -443px, right 0 top 443px; mask-size: 10px 3px, 10px 3px, 3px 10px, 3px 10px; }
	}
	@media (prefers-reduced-motion: reduce) {
		tbody tr.rb-row-pair::after,
		tbody tr.rb-row-pair:hover::after,
		.rb-pair-pulses tbody tr.rb-row-pair-live::after {
			animation: none;
			background-image: none;
		}
	}
	/* PAIR-09: the PAIR fold arrow, the purple sibling of .master-fold. */
	.pair-fold {
		position: absolute;
		transform: translateX(-50%);
		z-index: 4;
		display: inline-flex;
		align-items: center;
		padding: 2px;
		border: 0;
		background: transparent;
		color: var(--rb-pair);
		cursor: pointer;
		filter: drop-shadow(0 2px 4px rgba(0, 0, 0, 0.55));
		pointer-events: auto;
	}
	.pair-fold svg path {
		fill: currentColor;
		stroke: color-mix(in srgb, var(--rb-bg) 70%, transparent);
		stroke-width: 0.8;
		stroke-linejoin: round;
	}
	.pair-fold.above {
		top: 22px;
	}
	.pair-fold.below {
		bottom: 4px;
	}
	.pair-fold.below svg {
		transform: rotate(180deg);
	}
	.pair-fold:hover svg {
		translate: 0 -1px;
	}
	.pair-fold.below:hover svg {
		translate: 0 1px;
	}
	.pair-unknown {
		position: absolute;
		right: 12px;
		bottom: 4px;
		z-index: 4;
		padding: 1px 6px;
		border: 1px dashed var(--rb-pair);
		border-radius: 3px;
		background: var(--rb-bg);
		color: var(--rb-text);
		font-size: 11px;
		pointer-events: auto;
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
		/* Grows to hold the minor-issue square beside the cloud (CHROME-03). */
		min-width: 18px;
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
	/* OFFLOAD: an offloaded row is muted, never struck or red (spec e8da14b8 veto 2). */
	tbody tr.rb-row-offloaded td {
		opacity: 0.6;
	}
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
	/* Inherits the td nowrap + ellipsis: a wrapping guess grows the
	 * fixed-height row (22.5px -> 25px), which the virtualization math and
	 * right-click anchored popovers both assume never happens. */
	.genre-guess {
		color: var(--text-muted, #8b949e);
		font-style: italic;
	}
	.genre-tag.active {
		color: var(--rb-text);
		text-decoration: underline;
		text-underline-offset: 2px;
	}
	/* LIBUX-8679: a skipped backup row only dims; it stays in place, playable and loadable. */
	/* LIBUX-8682: gross key clash, a thin jagged red line on the border above the row, Key and BPM cells only. */
	td.set-clash-cell {
		position: relative;
	}
	.set-clash-edge {
		position: absolute;
		top: -2px;
		left: 0;
		right: 0;
		height: 4px;
		background:
			linear-gradient(135deg, transparent 50%, var(--rb-red, #d9434e) 50%) 0 0 / 4px 4px repeat-x,
			linear-gradient(45deg, var(--rb-red, #d9434e) 50%, transparent 50%) 0 0 / 4px 4px repeat-x;
		opacity: 0.8;
		pointer-events: auto;
		z-index: 1;
	}
	tbody tr.rb-row-set-skip {
		opacity: 0.45;
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
		width: 100%;
		height: 100%;
		object-fit: cover;
		object-position: center 66.67%;
		display: block;
	}
	.c-art:has(img.art-loaded) .art-slate {
		display: none;
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

	/* LIBUX-54: a hidden column drops its header and every cell; its <col> is
	 * left out of the colgroup, so fixed layout gives its width to nobody. */
	table.hide-preview :is(th.h-preview, td.c-preview) {
		display: none;
	}
	table.hide-art :is(th.h-art, td.c-art) {
		display: none;
	}
	table.hide-artist :is(th.h-artist, td.c-artist) {
		display: none;
	}
	table.hide-key :is(th.h-key, td.c-key) {
		display: none;
	}
	table.hide-bpm :is(th.h-bpm, td.c-bpm) {
		display: none;
	}
	table.hide-tempostab :is(th.h-tempostab, td.c-tempostab) {
		display: none;
	}
	/* Issue #7879: the cell's text styles live in BpmStabilityCell.svelte. */
	.c-tempostab {
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	table.hide-plays :is(th.h-plays, td.c-plays) {
		display: none;
	}
	table.hide-rating :is(th.h-rating, td.c-rating) {
		display: none;
	}
	table.hide-comments :is(th.h-comments, td.c-comments) {
		display: none;
	}
	table.hide-time :is(th.h-time, td.c-time) {
		display: none;
	}
	table.hide-quality :is(th.h-quality, td.c-quality) {
		display: none;
	}
	table.hide-energy :is(th.h-energy, td.c-energy) {
		display: none;
	}
	table.hide-genre :is(th.h-genre, td.c-genre) {
		display: none;
	}
	.tags-cell-text {
		display: block;
		font-style: italic;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	table.hide-tags :is(th.h-tags, td.c-tags) {
		display: none;
	}
	table.hide-stems :is(th.h-stems, td.c-stems) {
		display: none;
	}
</style>
