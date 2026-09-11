/**
 * Positional row selection for playlist panes (issue #2075).
 *
 * stable_id is track identity (batch edit, anlz, drag MIME); order is the
 * 1-based membership slot unique within a pane. Highlight and click selection
 * key on order so duplicate playlist entries select independently.
 */

export type RowRef = { stable_id: string; order: number };

export type SelectionPane = {
	rows: ReadonlyArray<RowRef>;
	selected_id: string | null;
	selected_ids: string[];
	selected_order: number | null;
	selected_orders: number[];
};

function resolveOrder(pane: SelectionPane, stable_id: string, order?: number): number | null {
	if (order !== undefined) return order;
	const row = pane.rows.find((r) => r.stable_id === stable_id);
	return row?.order ?? null;
}

function findInOrdered(
	ordered: readonly RowRef[],
	stable_id: string,
	order: number | null
): number {
	if (order !== null) {
		return ordered.findIndex((r) => r.stable_id === stable_id && r.order === order);
	}
	return ordered.findIndex((r) => r.stable_id === stable_id);
}

function uniqueStableIds(slice: readonly RowRef[]): string[] {
	const seen = new Set<string>();
	const ids: string[] = [];
	for (const row of slice) {
		if (!seen.has(row.stable_id)) {
			seen.add(row.stable_id);
			ids.push(row.stable_id);
		}
	}
	return ids;
}

function rebuildSelectedIds(pane: SelectionPane): string[] {
	const seen = new Set<string>();
	const ids: string[] = [];
	for (const ord of pane.selected_orders) {
		const row = pane.rows.find((r) => r.order === ord);
		if (row !== undefined && !seen.has(row.stable_id)) {
			seen.add(row.stable_id);
			ids.push(row.stable_id);
		}
	}
	return ids;
}

export function applySelect(
	pane: SelectionPane,
	stable_id: string,
	extend: boolean,
	range = false,
	ordered: readonly RowRef[] = [],
	order?: number
): void {
	const resolvedOrder = resolveOrder(pane, stable_id, order);

	if (range) {
		const anchorId = pane.selected_id;
		const from =
			anchorId === null ? -1 : findInOrdered(ordered, anchorId, pane.selected_order);
		const to = findInOrdered(ordered, stable_id, resolvedOrder);
		if (from !== -1 && to !== -1) {
			const lo = Math.min(from, to);
			const hi = Math.max(from, to);
			const slice = ordered.slice(lo, hi + 1);
			pane.selected_orders = slice.map((r) => r.order);
			pane.selected_ids = uniqueStableIds(slice);
			pane.selected_id = stable_id;
			pane.selected_order = resolvedOrder;
			return;
		}
	}

	pane.selected_id = stable_id;

	if (extend) {
		if (resolvedOrder !== null) {
			const idx = pane.selected_orders.indexOf(resolvedOrder);
			if (idx !== -1) {
				pane.selected_orders = pane.selected_orders.filter((o) => o !== resolvedOrder);
			} else {
				pane.selected_orders = [...pane.selected_orders, resolvedOrder];
			}
			pane.selected_order = resolvedOrder;
			pane.selected_ids =
				pane.rows.length > 0 ? rebuildSelectedIds(pane) : pane.selected_ids;
			if (pane.rows.length === 0) {
				pane.selected_ids = pane.selected_ids.includes(stable_id)
					? pane.selected_ids.filter((id) => id !== stable_id)
					: [...pane.selected_ids, stable_id];
			}
			return;
		}
		pane.selected_ids = pane.selected_ids.includes(stable_id)
			? pane.selected_ids.filter((id) => id !== stable_id)
			: [...pane.selected_ids, stable_id];
		pane.selected_order = null;
		pane.selected_orders = [];
		return;
	}

	pane.selected_ids = [stable_id];
	pane.selected_order = resolvedOrder;
	pane.selected_orders = resolvedOrder === null ? [] : [resolvedOrder];
}

export function clearSelection(
	pane: Pick<SelectionPane, 'selected_id' | 'selected_ids' | 'selected_order' | 'selected_orders'>
): void {
	pane.selected_id = null;
	pane.selected_ids = [];
	pane.selected_order = null;
	pane.selected_orders = [];
}

export function pruneSelection(pane: SelectionPane, rows: readonly RowRef[]): void {
	const present = new Set(rows.map((r) => `${r.stable_id}:${r.order}`));
	pane.selected_orders = pane.selected_orders.filter((ord) => {
		const row = pane.rows.find((r) => r.order === ord);
		return row !== undefined && present.has(`${row.stable_id}:${row.order}`);
	});
	pane.selected_ids = rebuildSelectedIds(pane);
	if (pane.selected_id !== null && !pane.selected_ids.includes(pane.selected_id)) {
		pane.selected_id = null;
		pane.selected_order = null;
	}
}

export function isSelectedRow(row: RowRef, selectedOrders: readonly number[]): boolean {
	return selectedOrders.includes(row.order);
}

export function masterHighlightOrder(
	rows: readonly RowRef[],
	masterStableId: string | null
): number | null {
	if (masterStableId === null) return null;
	return rows.find((r) => r.stable_id === masterStableId)?.order ?? null;
}
