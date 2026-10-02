/**
 * Library edit shortcuts: Cmd/Ctrl+A, C, X and V on the track list
 * (pin ce142ae7e22f in #3988, duplicated as 0b1e12cc01d0 in #3989).
 *
 * Select all marks every row the user can see (post filter and sort).
 * Copy and cut stash the selected tracks in an in-app clipboard; paste
 * appends them to the playlist open in the active pane, and a cut paste
 * moves them out of their source playlist through the same atomic
 * transfer endpoint a drag-move uses. The clipboard holds track ids, not
 * text, so it lives in this module rather than on the OS clipboard.
 *
 * Everything here is pure; BrowserPanel owns the key listener and the
 * network calls.
 */

import { isTextEntryTarget } from '$lib/keyboard/text-entry-target';
import type { RowRef, SelectionPane } from './pane-row-selection';

export type LibraryEditShortcut = 'select_all' | 'copy' | 'cut' | 'paste';

export type ShortcutKeyEvent = {
	key: string;
	metaKey: boolean;
	ctrlKey: boolean;
	altKey: boolean;
	shiftKey: boolean;
	target: EventTarget | null;
};

const SHORTCUT_KEYS: Record<string, LibraryEditShortcut> = {
	a: 'select_all',
	c: 'copy',
	x: 'cut',
	v: 'paste'
};

/** The library shortcut this key press asks for, or null when it is not one
 * or belongs to a text field (where Cmd+A/C/V must keep editing text). */
export function libraryEditShortcut(e: ShortcutKeyEvent): LibraryEditShortcut | null {
	if (!(e.metaKey || e.ctrlKey) || e.altKey || e.shiftKey) return null;
	if (isTextEntryTarget(e.target)) return null;
	return SHORTCUT_KEYS[e.key.toLowerCase()] ?? null;
}

/** Select every rendered row, keeping the anchor when it is still on screen
 * so a following shift-click extends from where the user last clicked.
 * Returns the number of tracks now selected. */
export function selectAllRows(pane: SelectionPane, rows: readonly RowRef[]): number {
	if (rows.length === 0) return 0;
	const orders: number[] = [];
	const seenOrders = new Set<number>();
	const ids: string[] = [];
	const seenIds = new Set<string>();
	for (const row of rows) {
		if (!seenOrders.has(row.order)) {
			seenOrders.add(row.order);
			orders.push(row.order);
		}
		if (!seenIds.has(row.stable_id)) {
			seenIds.add(row.stable_id);
			ids.push(row.stable_id);
		}
	}
	const anchorOnScreen =
		pane.selected_id !== null &&
		rows.some(
			(r) =>
				r.stable_id === pane.selected_id &&
				(pane.selected_order === null || r.order === pane.selected_order)
		);
	if (!anchorOnScreen) {
		pane.selected_id = rows[0].stable_id;
		pane.selected_order = rows[0].order;
	}
	pane.selected_orders = orders;
	pane.selected_ids = ids;
	return ids.length;
}

/** Selected track ids in the order they appear on screen, so a paste keeps
 * the visible running order rather than the click order. Falls back to
 * `selected_ids` when no positional selection exists. */
export function selectedIdsInViewOrder(
	rows: readonly RowRef[],
	selectedOrders: readonly number[],
	selectedIds: readonly string[]
): string[] {
	if (selectedOrders.length === 0) return [...new Set(selectedIds)];
	const wanted = new Set(selectedOrders);
	const seen = new Set<string>();
	const out: string[] = [];
	for (const row of rows) {
		if (wanted.has(row.order) && !seen.has(row.stable_id)) {
			seen.add(row.stable_id);
			out.push(row.stable_id);
		}
	}
	// A selected row filtered off screen since it was picked still counts:
	// the user selected it and nothing deselected it.
	for (const id of selectedIds) {
		if (!seen.has(id)) {
			seen.add(id);
			out.push(id);
		}
	}
	return out;
}

export type TrackClipboard = {
	stable_ids: string[];
	mode: 'copy' | 'cut';
	/** Playlist the tracks were taken from; null for All Tracks, search
	 * results and other non-playlist sources (a cut from those copies). */
	source_playlist_id: string | null;
	source_title: string;
};

export type PasteTargetPane = {
	playlist_id: string | null;
	kind: string | null;
	whole_collection: boolean;
};

/** Why the active pane cannot take a paste, or null when it can. */
export function pasteBlockReason(
	target: PasteTargetPane,
	source: 'collection' | 'spotify',
	clip: TrackClipboard | null
): string | null {
	if (clip === null || clip.stable_ids.length === 0) {
		return 'nothing to paste - select tracks and press Cmd+C first';
	}
	if (
		source !== 'collection' ||
		target.kind !== 'playlist' ||
		target.playlist_id === null ||
		target.playlist_id === 'all'
	) {
		return 'open a playlist to paste tracks into';
	}
	if (target.whole_collection) {
		return 'turn off whole-collection search to paste into this playlist';
	}
	return null;
}

/** Split the clipboard into tracks to add and tracks the target already
 * holds, so a repeated Cmd+V does not stack duplicates. */
export function partitionPaste(
	clipIds: readonly string[],
	targetIds: readonly string[]
): { add: string[]; already: number } {
	const present = new Set(targetIds);
	const add: string[] = [];
	let already = 0;
	for (const id of clipIds) {
		if (present.has(id)) already += 1;
		else {
			add.push(id);
			present.add(id);
		}
	}
	return { add, already };
}

/** Membership slots of the rows a paste just appended: the LAST row of each
 * pasted track, since the transfer appends to the end of the playlist. Used
 * to select and reveal what landed, which in a long playlist is otherwise
 * below the fold. */
export function pastedRowOrders(rows: readonly RowRef[], pastedIds: readonly string[]): number[] {
	const wanted = new Set(pastedIds);
	const lastOrder = new Map<string, number>();
	for (const row of rows) {
		if (wanted.has(row.stable_id)) lastOrder.set(row.stable_id, row.order);
	}
	return [...lastOrder.values()].sort((a, b) => a - b);
}

/** Select exactly the given rows, anchored on the first. */
export function selectRowOrders(pane: SelectionPane, orders: readonly number[]): void {
	const wanted = new Set(orders);
	const picked = pane.rows.filter((r) => wanted.has(r.order));
	if (picked.length === 0) return;
	pane.selected_orders = picked.map((r) => r.order);
	pane.selected_ids = [...new Set(picked.map((r) => r.stable_id))];
	pane.selected_id = picked[0].stable_id;
	pane.selected_order = picked[0].order;
}

export function clipboardToastMessage(count: number, mode: 'copy' | 'cut'): string {
	const noun = count === 1 ? '1 track' : `${count} tracks`;
	return mode === 'cut'
		? `Cut ${noun} - open a playlist and press Cmd+V to move them`
		: `Copied ${noun} - open a playlist and press Cmd+V to paste`;
}

export function pasteToastMessage(
	added: number,
	already: number,
	playlistName: string,
	moved: boolean
): string {
	const verb = moved ? 'Moved' : 'Pasted';
	const noun = added === 1 ? '1 track' : `${added} tracks`;
	const head = added === 0 ? `Nothing new to paste into "${playlistName}"` : `${verb} ${noun} into "${playlistName}"`;
	if (already === 0) return head;
	return `${head} (${already} already there)`;
}

// In-app clipboard. One per window: copy in one pane, paste in another.
let clipboard: TrackClipboard | null = null;

export function getTrackClipboard(): TrackClipboard | null {
	return clipboard;
}

export function setTrackClipboard(next: TrackClipboard | null): void {
	clipboard = next;
}
