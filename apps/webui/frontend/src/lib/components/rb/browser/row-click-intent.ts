/**
 * LIBUX-8676: a click on a multi-selected row that the WebView turned into a
 * drag still counts as a click.
 *
 * the maintainer, Wed 4 Nov 2026: "when selecting multiple tracks, clicking once without
 * dragging at all doesn't deselect them". Rows are `draggable`, and WebKit
 * starts a drag session after about 3 px of travel; a trackpad press moves
 * that much, so the press became a zero-distance drag of the whole selection
 * and no `click` (the event that collapses the selection) ever fired.
 * File managers treat a press-release that barely moved and dropped nowhere
 * as a click: on a multi-selection, that collapses it to the pressed row.
 */

/** Travel, in CSS px, under which a press-release is a click, not a drag. */
export const ROW_CLICK_SLOP_PX = 8;

export interface RowPressMark {
	key: string;
	x: number;
	y: number;
}

interface PressEvent {
	button: number;
	clientX: number;
	clientY: number;
	shiftKey: boolean;
	metaKey: boolean;
	ctrlKey: boolean;
	altKey: boolean;
}

/** The press a later dragend is measured against; null for modified or
 * non-primary presses, which never collapse a selection. */
export function markRowPress(event: PressEvent, key: string): RowPressMark | null {
	if (event.button !== 0) return null;
	if (event.shiftKey || event.metaKey || event.ctrlKey || event.altKey) return null;
	return { key, x: event.clientX, y: event.clientY };
}

/**
 * True when a drag that just ended was really a click that should collapse a
 * multi-selection onto `key`: same row as the press, more than one row
 * selected, the drop landed nowhere, and the pointer stayed within the slop.
 */
export function dragEndIsCollapsingClick(opts: {
	mark: RowPressMark | null;
	key: string;
	end: { clientX: number; clientY: number };
	dropEffect: string | undefined;
	selectedCount: number;
}): boolean {
	const { mark, key, end, dropEffect, selectedCount } = opts;
	if (mark === null || mark.key !== key || selectedCount < 2) return false;
	if (dropEffect !== undefined && dropEffect !== 'none') return false;
	return Math.hypot(end.clientX - mark.x, end.clientY - mark.y) <= ROW_CLICK_SLOP_PX;
}
