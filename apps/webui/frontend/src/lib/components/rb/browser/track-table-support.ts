export { analysisIssuesFor, bpmGridHoverText, errColumnTitle, gridFlagFor } from '$lib/rb/analysis-issues';
export { gridProvenanceFor, requestGridProvenance } from '$lib/rb/grid-provenance.svelte';
export { camelotKeyColor, camelotKeyHoverLabel } from '$lib/rb/camelot-color';
export { displayKey } from '$lib/rb/key-notation';
export { canvasMeasureText, fitMusicalColumns, renderedTableFont } from '$lib/rb/musical-column-fit';
export { bpmCellText, hasUsableBpm } from '$lib/rb/bpm-cell-text';
export { genreCellTitle, listingGenre } from '$lib/rb/genre-source';
export { columnHeaderTitle, type LibraryColTipId } from '$lib/rb/column-tips';
export { masterFoldCenterPx } from '$lib/rb/master-fold-anchor';
export { highlightSpans, keyCellSpans, rowMatchesFind } from '$lib/rb/find-highlight';
export { buildCurveSegments, segmentPath } from '$lib/rb/autoplay-curve';
export { describeAutoPlayMode } from '$lib/rb/autoplay-mode';
export { installTrackDragGhost, removeTrackDragGhost } from '$lib/rb/drag-ghost';
export { trackDragRefusal } from '$lib/rb/track-drag-refusal';
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
export { compatibleKeyChecked, compatibleRowKeyShift } from '$lib/rb/next-only-filter';
export { keyShiftSuperscript, shiftedKeyText } from '$lib/rb/compatible-filter-copy';
export type { CompatibleKeyShift } from '$lib/player/key/camelot';
export { loadKeyHoverCard } from './key-hover-card-load';
