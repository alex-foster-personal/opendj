export {
	analysisIssuesFor,
	bpmGridHoverText,
	errColumnTitle,
	gridFlagFor,
	lengthFlagText,
	sourceFileFlagsFor
} from '$lib/rb/analysis-issues';
export { gridProvenanceFor, requestGridProvenance } from '$lib/rb/grid-provenance.svelte';
export { camelotKeyColor, camelotKeyHoverLabel } from '$lib/rb/camelot-color';
export { displayKey, formatKey } from '$lib/rb/key-notation';
export { canvasMeasureText, fitMusicalColumns, renderedTableFont } from '$lib/rb/musical-column-fit';
export { bpmCellText, hasUsableBpm } from '$lib/rb/bpm-cell-text';
export { genreCellTitle, listingGenre } from '$lib/rb/genre-source';
export { columnHeaderTitle, type LibraryColTipId } from '$lib/rb/column-tips';
export { masterFoldCenterPx } from '$lib/rb/master-fold-anchor';
export { highlightSpans, keyCellSpans, rowMatchesFind } from '$lib/rb/find-highlight';
export { arrowHead, buildCurveSegments, hopStroke, paintChainHops, segmentPath } from '$lib/rb/autoplay-curve';
export { describeAutoPlayMode } from '$lib/rb/autoplay-mode';
export { installTrackDragGhost, removeTrackDragGhost } from '$lib/rb/drag-ghost';
export { trackDragRefusal } from '$lib/rb/track-drag-refusal';
export { draggedDeckRows } from '$lib/rb/deck-track-drop';
export {
	dropTargetAt,
	gapFromPointer,
	rowWindow,
	slotCountOf,
	slotOfRow,
	type SectionHeader
} from '$lib/library/playlist-sections';
export type { EntriesMove } from '$lib/library/playlist-sections-store.svelte';

export {
	type TrackDropGap,
	trackDropGapFromPointer,
	trackDropGapTransform
} from './track-table-drop-gap';

export { animateSectionToggle } from '$lib/library/section-motion';
export {
	bpmHeatLabel,
	classifyBpmHeat,
	genreHoverColor
} from './track-table-colors';
export { columnExplainer } from './column-explainer-placement';
export { trackEditMenuItems, type TrackEditModalKind } from './track-edit-menu';
export { addToPlaylistMenuItem } from './add-to-playlist-menu';
export { removeFromLibraryMenuItem } from './track-library-menu';
export { showInPlaylistsMenuItem } from './track-playlists-menu';
export { default as LastPlayedCell } from './LastPlayedCell.svelte';
export const loadBpmKeyEditPopover = () => import('./BpmKeyEditPopover.svelte');
export { default as ContextMenu } from '../ContextMenu.svelte';
export { formatDraftBpm } from './bpm-key-cell-edit';
export {
	commitBpmEdit,
	commitKeyEdit,
	editedBpm,
	editedKey,
	hasBpmEdit,
	hasKeyEdit
} from './bpm-key-overrides.svelte';
export { compatibleKeyChecked, compatibleRowKeyShift } from '$lib/rb/next-only-filter';
export { keyShiftSuperscript, shiftedKeyText } from '$lib/rb/compatible-filter-copy';
export type { CompatibleKeyShift } from '$lib/player/key/camelot';
export { loadKeyHoverCard } from './key-hover-card-load';
export {
	OPTIONAL_COLUMNS,
	OPTIONAL_COLUMN_LABELS,
	classicalArtist,
	classicalTitle,
	composerOf,
	filedUnderText,
	labelOf,
	performerOf,
	primaryWorkId,
	stackIndicator,
	workCellText,
	yearOf,
	type OptionalColumn
} from '$lib/rb/library-works';
export {
	refreshSuggestionFlags,
	reportWorksFailure,
	startWorksOverlay,
	viaWorkHits,
	worksView
} from '$lib/rb/works-overlay.svelte';
export { shiftDropAction } from './works-drop';
export { default as VersionStackPanel } from './VersionStackPanel.svelte';
export { default as WorksModals } from './WorksModals.svelte';

export function formatTagsCell(tags: readonly string[]): string {
	return tags.map((tag) => `#${tag}`).join(' · ');
}
