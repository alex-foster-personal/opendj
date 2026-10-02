/**
 * Keyboard navigation for the library track list (TrackTable).
 *
 * Pure decisions only, so TrackTable.svelte (a hotspot file) keeps just the
 * wiring: which key does what, where the active row moves to, which rows a
 * Shift-extended move should select, how far a page is, and whether the
 * table should leave the key alone because the user is typing.
 *
 * Selection itself is NOT modelled here. The table drives the existing pane
 * selection model (pane-row-selection.ts applySelect, via onselectrow) with
 * the same plain / Shift gestures a mouse click uses; this module only says
 * which rows those gestures target.
 */

import { isTextEntryTarget } from '$lib/keyboard/text-entry-target';

export type TrackTableNavKey = 'up' | 'down' | 'home' | 'end' | 'pageup' | 'pagedown';

export type TrackTableKeyAction =
	| { kind: 'move'; nav: TrackTableNavKey; extend: boolean }
	| { kind: 'load'; shift: boolean; replace: boolean }
	| { kind: 'none' };

export interface TrackTableKeyEvent {
	key: string;
	shiftKey: boolean;
	metaKey: boolean;
	ctrlKey: boolean;
	altKey: boolean;
	target: EventTarget | null;
	currentTarget: EventTarget | null;
	isComposing?: boolean;
}

const NAV_KEYS: Record<string, TrackTableNavKey> = {
	ArrowUp: 'up',
	ArrowDown: 'down',
	Home: 'home',
	End: 'end',
	PageUp: 'pageup',
	PageDown: 'pagedown'
};

/**
 * Map a keydown on a track row to a table action.
 *
 * Only a key aimed at the ROW ITSELF counts: a key that bubbles up from a
 * control inside the row (a deck button, the rating stars, the preview
 * strip, an inline edit field) belongs to that control, so Enter on a
 * "Load onto deck" button does not also run the row's own load. Keys while
 * typing in a text field are always left alone. Alt chords are left alone
 * so OS/browser bindings keep working.
 */
export function trackTableKeyAction(e: TrackTableKeyEvent): TrackTableKeyAction {
	if (e.isComposing === true) return { kind: 'none' };
	if (isTextEntryTarget(e.target)) return { kind: 'none' };
	if (e.target !== e.currentTarget) return { kind: 'none' };
	if (e.altKey) return { kind: 'none' };
	if (e.key === 'Enter') {
		return { kind: 'load', shift: e.shiftKey, replace: e.metaKey || e.ctrlKey };
	}
	const nav = NAV_KEYS[e.key];
	if (nav === undefined) return { kind: 'none' };
	// Cmd/Ctrl+arrow is left for the platform (and any future chord), so the
	// table only claims the plain and Shift-extended forms.
	if (e.metaKey || e.ctrlKey) return { kind: 'none' };
	return { kind: 'move', nav, extend: e.shiftKey };
}

/** Rows one PageUp/PageDown moves: the rows the viewport actually shows,
 * never less than one (an unmeasured or tiny viewport still moves). */
export function trackTablePageSize(viewportRowCapacity: number): number {
	if (!Number.isFinite(viewportRowCapacity)) return 1;
	return Math.max(1, Math.floor(viewportRowCapacity));
}

/**
 * Index the active row moves to. `current` -1 means no active row yet: the
 * first move lands on the first row (Down/Home/PageDown) or the last
 * (Up/End/PageUp), so the very first key press always selects something.
 * Clamps at both ends; returns -1 only for an empty list.
 */
export function nextTrackTableIndex(params: {
	current: number;
	nav: TrackTableNavKey;
	rowCount: number;
	pageSize: number;
}): number {
	const { nav, rowCount } = params;
	if (rowCount <= 0) return -1;
	const last = rowCount - 1;
	const page = trackTablePageSize(params.pageSize);
	const current = params.current >= 0 && params.current <= last ? params.current : -1;
	if (nav === 'home') return 0;
	if (nav === 'end') return last;
	if (current === -1) {
		return nav === 'down' || nav === 'pagedown' ? 0 : last;
	}
	const delta =
		nav === 'down' ? 1 : nav === 'up' ? -1 : nav === 'pagedown' ? page : -page;
	return Math.min(last, Math.max(0, current + delta));
}

/** Inclusive index span a Shift-extended move selects: from the fixed
 * anchor to the new active row, in on-screen order. With no usable anchor
 * the span is just the target row (a plain move). */
export function trackTableRangeSpan(params: {
	anchor: number;
	target: number;
	rowCount: number;
}): { from: number; to: number } {
	const { anchor, target, rowCount } = params;
	if (anchor < 0 || anchor >= rowCount) return { from: target, to: target };
	return { from: Math.min(anchor, target), to: Math.max(anchor, target) };
}

/**
 * Scroll position that brings row `rowIndex` fully into the visible band of
 * a virtualized table with a sticky header, moving as little as possible:
 * unchanged when the row is already fully visible, aligned to the top edge
 * when it is above, to the bottom edge when it is below.
 */
export function scrollTopToRevealRow(params: {
	rowIndex: number;
	rowHeight: number;
	headerOffsetPx: number;
	viewportHeight: number;
	scrollTop: number;
}): number {
	const { rowIndex, rowHeight, headerOffsetPx, viewportHeight, scrollTop } = params;
	if (rowIndex < 0 || rowHeight <= 0 || viewportHeight <= 0) return Math.max(0, scrollTop);
	// Row tops in scroll-content coordinates: tbody starts below the thead.
	const rowTop = headerOffsetPx + rowIndex * rowHeight;
	const rowBottom = rowTop + rowHeight;
	const visibleTop = scrollTop + headerOffsetPx;
	const visibleBottom = scrollTop + viewportHeight;
	if (rowTop < visibleTop) return Math.max(0, rowTop - headerOffsetPx);
	if (rowBottom > visibleBottom) return Math.max(0, rowBottom - viewportHeight);
	return Math.max(0, scrollTop);
}

/** Stable identity of a row within a pane: playlists can repeat a track, so
 * stable_id alone is not unique, but (stable_id, order) is. */
export function trackRowKey(row: { stable_id: string; order: number }): string {
	return `${row.stable_id}:${row.order}`;
}

/**
 * Index of the active (focused) row: the row the keyboard last moved to or
 * the mouse last clicked, when it is still in the list; otherwise the most
 * recently selected row, so focus resumes where the selection is after a
 * pane switch or a reload. -1 when neither is present.
 */
export function resolveActiveRowIndex(params: {
	rows: ReadonlyArray<{ stable_id: string; order: number }>;
	activeKey: string | null;
	selectedOrders: readonly number[];
}): number {
	const { rows, activeKey, selectedOrders } = params;
	if (activeKey !== null) {
		const idx = rows.findIndex((r) => trackRowKey(r) === activeKey);
		if (idx >= 0) return idx;
	}
	if (selectedOrders.length > 0) {
		const lead = selectedOrders[selectedOrders.length - 1];
		return rows.findIndex((r) => r.order === lead);
	}
	return -1;
}
