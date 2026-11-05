/**
 * LIBUX-8626: removing playlist rows in place, without reloading the pane.
 *
 * A reload (`_loadPane` -> `beginLoad`) empties the rows, the selection and
 * the scroll position, so the list flashed and the user lost their place. A
 * Backspace delete instead flashes the doomed rows red for
 * ROW_REMOVE_ANIMATION_MS, then splices them out here and moves the selection
 * to the row that took their place.
 */

/** How long a removed row shows red and collapses before it leaves the list
 * (LIBUX-8626 shipped 100 ms; the maintainer, Wed 4 Nov 2026: "could be 200ms"). */
export const ROW_REMOVE_ANIMATION_MS = 200;

/** How long an undo-restored row shows green while it expands back in. */
export const ROW_RESTORE_ANIMATION_MS = 200;

/** A membership slot: the identity `trackRowKey` and the selection use. */
export interface RowSlot {
	stable_id: string;
	order: number;
}

/**
 * `rows` (membership order) minus the removed slots, renumbered 1..n so each
 * `order` is the slot a reload would assign. Untouched rows keep identity.
 */
export function withoutRemovedRows<R extends RowSlot>(
	rows: readonly R[],
	removedOrders: ReadonlySet<number>
): R[] {
	const kept = rows.filter((row) => !removedOrders.has(row.order));
	return kept.map((row, index) => (row.order === index + 1 ? row : { ...row, order: index + 1 }));
}

/**
 * Where the selection lands after removing `removedOrders`: the first
 * surviving row below the lowest removed row in on-screen order, or, when the
 * removal reached the bottom, the nearest surviving row above the highest
 * one. Returned with its post-removal `order`; null when nothing survives.
 */
export function selectionAfterRemoval(
	visible: readonly RowSlot[],
	removedOrders: ReadonlySet<number>
): RowSlot | null {
	const removedIndexes = visible.flatMap((row, index) => (removedOrders.has(row.order) ? [index] : []));
	if (removedIndexes.length === 0) return null;
	const first = removedIndexes[0];
	const last = removedIndexes[removedIndexes.length - 1];
	const survives = (row: RowSlot): boolean => !removedOrders.has(row.order);
	const pick = visible.slice(last + 1).find(survives) ?? visible.slice(0, first).reverse().find(survives);
	if (pick === undefined) return null;
	let shift = 0;
	for (const order of removedOrders) if (order < pick.order) shift += 1;
	return { stable_id: pick.stable_id, order: pick.order - shift };
}

/** The PaneStore fields an in-place removal reads and writes. */
export interface RemovalPane<R extends RowSlot> {
	playlist_id: string | null;
	rows: R[];
	etag: string;
	selected_id: string | null;
	selected_ids: string[];
	selected_order: number | null;
	selected_orders: number[];
	select(stable_id: string, extend?: boolean, range?: boolean, ordered_ids?: readonly string[], order?: number): void;
}

function _wait(ms: number): Promise<void> {
	return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Remove `rows` from the pane's playlist without reloading it: mark them for
 * the red collapse, send the write, and once BOTH the write and the animation
 * finished splice them out, take the fresh membership etag and select the
 * row that took their place. Scroll position is untouched because the pane is
 * never reset. A failed write unmarks the rows and rethrows; a pane switched
 * to another playlist meanwhile is left alone ('superseded').
 */
export async function removeRowsInPlace<R extends RowSlot & { item_id?: string | null }>(opts: {
	pane: RemovalPane<R>;
	rows: readonly R[];
	visible: readonly RowSlot[];
	remove: (playlistId: string, itemIds: string[]) => Promise<{ etag: string }>;
	markRemoving: (keys: ReadonlySet<string>) => void;
	rowKey: (row: RowSlot) => string;
	wait?: (ms: number) => Promise<void>;
}): Promise<RowRemoval<R> | 'superseded'> {
	const { pane, rows, visible, remove, markRemoving, rowKey } = opts;
	const playlistId = pane.playlist_id;
	if (playlistId === null) throw new Error('remove rows: the pane shows no playlist');
	const itemIds = rows.map((row) => row.item_id);
	if (itemIds.some((id) => !id)) throw new Error('playlist row has no membership id - reload and try again');
	const removedOrders = new Set(rows.map((row) => row.order));
	const next = selectionAfterRemoval(visible, removedOrders);
	markRemoving(new Set(rows.map(rowKey)));
	let etag: string;
	try {
		[{ etag }] = await Promise.all([
			remove(playlistId, itemIds as string[]),
			(opts.wait ?? _wait)(ROW_REMOVE_ANIMATION_MS)
		]);
	} finally {
		markRemoving(new Set());
	}
	if (pane.playlist_id !== playlistId) return 'superseded';
	const removedRows = pane.rows.filter((row) => removedOrders.has(row.order));
	pane.rows = withoutRemovedRows(pane.rows, removedOrders);
	pane.etag = etag;
	if (next !== null) {
		pane.select(next.stable_id, false, false, [], next.order);
	} else {
		pane.selected_id = null;
		pane.selected_ids = [];
		pane.selected_order = null;
		pane.selected_orders = [];
	}
	return { playlist_id: playlistId, rows: removedRows, etag_after: etag };
}

// ---------------------------------------------------------------- undo restore
// LIBUX-8676 (the maintainer, Wed 4 Nov 2026): "Undo for that should be almost instant
// and have the reverse animation with green." The server's undo of a removal
// un-tombstones the SAME membership rows at their old order keys, so the
// client can put the rows it removed back where they were before the write
// returns, instead of waiting for a full pane reload.

/** One in-place removal, remembered so its undo can restore it in place. */
export interface RowRemoval<R extends RowSlot> {
	playlist_id: string;
	/** The removed rows with their PRE-removal `order`, membership order. */
	rows: R[];
	/** Membership etag right after the removal: the pane must still hold it. */
	etag_after: string;
}

/** The fields of a playlist history entry an undo restore matches on. */
export interface RemovalHistoryEntry {
	op: string;
	playlist_id: string;
	stable_ids?: string[];
}

/**
 * `rows` (post-removal membership order) with `restored` put back at their
 * pre-removal `order`, renumbered 1..n. The inverse of `withoutRemovedRows`
 * for the same removal; untouched rows keep identity where the slot matches.
 */
export function withRestoredRows<R extends RowSlot>(rows: readonly R[], restored: readonly R[]): R[] {
	const byOrder = new Map(restored.map((row) => [row.order, row]));
	const total = rows.length + restored.length;
	const out: R[] = [];
	let next = 0;
	for (let order = 1; order <= total; order += 1) {
		const back = byOrder.get(order);
		const row = back ?? rows[next++];
		if (row === undefined) throw new Error(`restore rows: no row for slot ${order}`);
		out.push(row.order === order ? row : { ...row, order });
	}
	return out;
}

function _sameIdSet(left: readonly string[], right: readonly string[]): boolean {
	const a = new Set(left);
	const b = new Set(right);
	return a.size === b.size && [...a].every((id) => b.has(id));
}

/**
 * The remembered removal the next undo reverses, or null when it cannot be
 * restored in place: the history entry is not this pane's removal, or the
 * pane changed since (another playlist, a newer edit, a reload).
 */
export function restorableRemoval<R extends RowSlot>(
	removals: readonly RowRemoval<R>[],
	entry: RemovalHistoryEntry,
	pane: { playlist_id: string | null; etag: string }
): RowRemoval<R> | null {
	const last = removals.at(-1);
	if (last === undefined || entry.op !== 'memberships') return null;
	if (last.playlist_id !== entry.playlist_id || pane.playlist_id !== last.playlist_id) return null;
	if (pane.etag !== last.etag_after) return null;
	const ids = last.rows.map((row) => row.stable_id);
	if (entry.stable_ids === undefined || !_sameIdSet(ids, entry.stable_ids)) return null;
	return last;
}

/** An optimistic restore already on screen, waiting on the undo write. */
export interface PendingRestore {
	/** The undo landed on this entry: keep the rows, take its etag. */
	commit(etag: string | undefined): void;
	/** The undo failed or reversed something else: take the rows back out. */
	revert(): void;
}

/**
 * Put `removal.rows` back into the pane NOW, flash them green for
 * ROW_RESTORE_ANIMATION_MS and select them. Returns the commit/revert pair
 * the undo write settles. A revert after the pane moved on leaves it alone.
 */
export function restoreRowsInPlace<R extends RowSlot>(opts: {
	pane: RemovalPane<R>;
	removal: RowRemoval<R>;
	markRestoring: (keys: ReadonlySet<string>) => void;
	rowKey: (row: RowSlot) => string;
	wait?: (ms: number) => Promise<void>;
}): PendingRestore {
	const { pane, removal, markRestoring, rowKey } = opts;
	const before = pane.rows;
	const beforeSelection = {
		selected_id: pane.selected_id,
		selected_ids: pane.selected_ids,
		selected_order: pane.selected_order,
		selected_orders: pane.selected_orders
	};
	const restored = withRestoredRows(before, removal.rows);
	pane.rows = restored;
	markRestoring(new Set(removal.rows.map(rowKey)));
	const [first, ...rest] = removal.rows;
	if (first !== undefined) {
		pane.select(first.stable_id, false, false, [], first.order);
		for (const row of rest) pane.select(row.stable_id, true, false, [], row.order);
	}
	void (opts.wait ?? _wait)(ROW_RESTORE_ANIMATION_MS).then(() => markRestoring(new Set()));
	return {
		commit(etag) {
			if (etag !== undefined && pane.rows === restored) pane.etag = etag;
		},
		revert() {
			markRestoring(new Set());
			if (pane.rows !== restored) return;
			pane.rows = before;
			Object.assign(pane, beforeSelection);
		}
	};
}
