import {
	dropTargetAt,
	gapFromPointer,
	type DropTarget,
	type SectionHeader
} from '$lib/library/playlist-sections';

export interface TrackDropGap {
	slot: number;
	count: number;
	offsetPx: number;
	target: DropTarget;
}

/** One pointer calculation owns both the visible gap and the emitted target. */
export function trackDropGapFromPointer(params: {
	pointerY: number;
	rowHeight: number;
	slotCount: number;
	headers: readonly SectionHeader[];
	rows: readonly { item_id: string | null }[];
	draggedTrackCount: number;
}): TrackDropGap {
	const { pointerY, rowHeight, slotCount, headers, rows, draggedTrackCount } = params;
	if (!Number.isSafeInteger(draggedTrackCount) || draggedTrackCount <= 0) {
		throw new RangeError(
			`trackDropGapFromPointer: draggedTrackCount must be a positive integer, got ${String(draggedTrackCount)}`
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

/** Virtualized rows move with a transform only; their layout geometry stays fixed. */
export function trackDropGapTransform(renderedSlot: number, gap: TrackDropGap): string | undefined {
	return renderedSlot < gap.slot ? undefined : `translateY(${gap.offsetPx}px)`;
}
