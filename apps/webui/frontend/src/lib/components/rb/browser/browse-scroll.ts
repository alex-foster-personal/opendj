/**
 * TrackTable browse-scroll session helpers (issue #2606, LIBUX-2606).
 * Pure functions: when to apply keep-in-view, pane restore, and viewport measure.
 */

import { scrollTopForRowIndex, TRACK_TABLE_THEAD_PX } from './virtual-window';

export type BrowseScrollRequest = {
	order: number;
	direction: -1 | 1;
	revision: number;
};

export function shouldApplyBrowseScroll(
	request: BrowseScrollRequest | null,
	lastAppliedRevision: number | null
): boolean {
	if (request === null) return false;
	if (lastAppliedRevision === null) return true;
	return request.revision !== lastAppliedRevision;
}

export function browseScrollTarget(params: {
	request: BrowseScrollRequest;
	rows: ReadonlyArray<{ order: number }>;
	viewportHeight: number;
	rowHeight: number;
	headerOffsetPx?: number;
	slotOfRow: (index: number) => number;
}): number | null {
	const {
		request,
		rows,
		viewportHeight,
		rowHeight,
		headerOffsetPx = TRACK_TABLE_THEAD_PX,
		slotOfRow
	} = params;
	const index = rows.findIndex((row) => row.order === request.order);
	if (index < 0) return null;
	const offset = request.direction > 0 ? viewportHeight * 0.35 : viewportHeight * 0.65;
	return scrollTopForRowIndex({
		rowIndex: slotOfRow(index),
		rowHeight,
		headerOffsetPx,
		offsetFromTopPx: offset
	});
}

export function shouldRestorePaneScroll(
	restoreKey: string | number,
	lastRestoredKey: string | number | null
): boolean {
	return restoreKey !== lastRestoredKey;
}

export function nextMeasuredViewportHeight(measured: number, previous: number): number {
	if (measured > 0) return measured;
	if (previous > 0 && measured <= 0) return previous;
	return measured;
}
