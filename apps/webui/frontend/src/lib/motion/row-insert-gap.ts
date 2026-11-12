/**
 * Transform-only insertion gap for virtualized track-table reorder (#7605).
 * Shares the 100 ms / 200-row / reduced-motion snap policy with collapse.ts
 * (LIBUX-53). Mounted rows move with translateY only; layout height never changes.
 */
import {
	dropTargetAt,
	gapFromPointer,
	type DropTarget,
	type SectionHeader
} from '$lib/library/playlist-sections';

import { COLLAPSE_MS, COLLAPSE_SNAP_ROWS, collapseDuration } from './collapse';

export { COLLAPSE_MS, COLLAPSE_SNAP_ROWS, collapseDuration };

/** Same snap threshold and duration as library collapse blocks. */
export const rowInsertGapDuration = collapseDuration;

export interface RowInsertGap {
	slot: number;
	count: number;
	offsetPx: number;
	target: DropTarget;
}

/** Legacy name kept for TrackTable and track-table-support re-exports. */
export type TrackDropGap = RowInsertGap;

/** One pointer calculation owns both the visible gap slot and the drop target. */
export function rowInsertGapFromPointer(params: {
	pointerY: number;
	rowHeight: number;
	slotCount: number;
	headers: readonly SectionHeader[];
	rows: readonly { item_id: string | null }[];
	draggedTrackCount: number;
}): RowInsertGap {
	const { pointerY, rowHeight, slotCount, headers, rows, draggedTrackCount } = params;
	if (!Number.isSafeInteger(draggedTrackCount) || draggedTrackCount <= 0) {
		throw new RangeError(
			`rowInsertGapFromPointer: draggedTrackCount must be a positive integer, got ${String(draggedTrackCount)}`
		);
	}
	const slot = gapFromPointer(pointerY, rowHeight, slotCount);
	return {
		slot,
		count: draggedTrackCount,
		offsetPx: draggedTrackCount * rowHeight,
		target: dropTargetAt(slot, headers, rows)
	};
}

/** Inline transform for a rendered slot; undefined above the gap. */
export function rowInsertGapTransform(renderedSlot: number, gap: RowInsertGap): string | undefined {
	return renderedSlot < gap.slot ? undefined : `translateY(${gap.offsetPx}px)`;
}

export const trackDropGapFromPointer = rowInsertGapFromPointer;
export const trackDropGapTransform = rowInsertGapTransform;
