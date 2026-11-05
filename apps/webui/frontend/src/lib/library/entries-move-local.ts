/**
 * REORDER-ROW-02: a drag-move lands in the table at once, with no whole-list
 * repaint, and a 3 px line shows where it will land before the drop.
 *
 * `moveEntriesLocally` is the client mirror of the server's entries:move
 * (apps/shared/state/library_sections.py display_tokens + move_tokens +
 * section_positions): lift tracks and dividers out as one block in display
 * order and land them above the named line, a target inside the block meaning
 * the next line below it. Applied to the pane before the request, it moves
 * the same row objects (keyed by item_id in the table) so no row remounts; the
 * server's answer is then read back quietly and a refusal reverts.
 *
 * `dropLineTop` places the insertion line in the middle of the gap the drag
 * opens (rows at or below the gap slide down one slot), from the same slot
 * numbers `gapFromPointer` and `dropTargetAt` use, so the line, the gap and
 * the landing place can never disagree, at a section boundary included.
 */
import type { DropTarget, PlaylistSection } from './playlist-sections';

/** Thickness of the insertion line, px. */
export const DROP_LINE_PX = 3;

export interface EntriesBlockMove extends DropTarget {
	item_ids: string[];
	section_ids: string[];
}

type Token = { kind: 'track' | 'section'; id: string };

const _tokenKey = (token: Token): string => `${token.kind}:${token.id}`;

function _displayTokens(itemIds: readonly string[], sections: readonly PlaylistSection[]): Token[] {
	const above = new Map<number, string[]>();
	const ordered = sections
		.map((section, index) => ({ section, index }))
		.sort((a, b) => a.section.before_position - b.section.before_position || a.index - b.index);
	for (const { section } of ordered) {
		const at = Math.min(section.before_position, itemIds.length);
		above.set(at, [...(above.get(at) ?? []), section.id]);
	}
	const tokens: Token[] = [];
	itemIds.forEach((itemId, index) => {
		for (const id of above.get(index) ?? []) tokens.push({ kind: 'section', id });
		tokens.push({ kind: 'track', id: itemId });
	});
	for (const id of above.get(itemIds.length) ?? []) tokens.push({ kind: 'section', id });
	return tokens;
}

/**
 * The rows (membership order) and dividers after `move`, as the server will
 * write them. Rows keep their objects when their slot is unchanged; a moved
 * row is a shallow copy carrying its new `order`. `orderMap` maps each row's
 * old order to its new one. Null when a row has no membership id or a named
 * line is not in the list.
 */
export function moveEntriesLocally<Row extends { order: number; item_id: string | null }>(
	rows: readonly Row[],
	sections: readonly PlaylistSection[],
	move: EntriesBlockMove
): { rows: Row[]; sections: PlaylistSection[]; orderMap: Map<number, number> } | null {
	const byItem = new Map<string, Row>();
	for (const row of rows) {
		if (row.item_id === null) return null;
		byItem.set(row.item_id, row);
	}
	const tokens = _displayTokens([...byItem.keys()], sections);
	const keys = tokens.map(_tokenKey);
	const moved = new Set([
		...move.item_ids.map((id) => `track:${id}`),
		...move.section_ids.map((id) => `section:${id}`)
	]);
	for (const key of moved) if (!keys.includes(key)) return null;
	let before: string | null =
		move.before_item_id !== undefined
			? `track:${move.before_item_id}`
			: move.before_section_id !== undefined
				? `section:${move.before_section_id}`
				: null;
	if (before !== null && !keys.includes(before)) return null;
	if (before !== null && moved.has(before)) {
		before = keys.slice(keys.indexOf(before)).find((key) => !moved.has(key)) ?? null;
	}
	const block = tokens.filter((token) => moved.has(_tokenKey(token)));
	const rest = tokens.filter((token) => !moved.has(_tokenKey(token)));
	const at = before === null ? rest.length : rest.findIndex((token) => _tokenKey(token) === before);
	const arranged = [...rest.slice(0, at), ...block, ...rest.slice(at)];

	const slots = rows.map((row) => row.order);
	const orderMap = new Map<number, number>();
	const positions = new Map<string, number>();
	const nextRows: Row[] = [];
	for (const token of arranged) {
		if (token.kind === 'section') {
			positions.set(token.id, nextRows.length);
			continue;
		}
		const row = byItem.get(token.id)!;
		const slot = slots[nextRows.length];
		orderMap.set(row.order, slot);
		nextRows.push(row.order === slot ? row : { ...row, order: slot });
	}
	const nextSections = sections
		.map((section) => {
			const position = positions.get(section.id)!;
			return position === section.before_position ? section : { ...section, before_position: position };
		})
		.sort((a, b) => a.before_position - b.before_position);
	return { rows: nextRows, sections: nextSections, orderMap };
}

/** The pane state an optimistic move touches. */
export interface MovablePane<Row extends { order: number; item_id: string | null }> {
	rows: Row[];
	selected_orders: number[];
	selected_order: number | null;
}

/**
 * Apply `move` to the pane and its dividers now. The selection (multi and
 * anchor) follows its rows. Returns a revert restoring the exact prior rows,
 * dividers and selection, or null when the move cannot be mirrored locally
 * (the caller then just waits for the server).
 */
export function applyOptimisticEntriesMove<Row extends { order: number; item_id: string | null }>(
	pane: MovablePane<Row>,
	sections: readonly PlaylistSection[],
	setSections: (next: readonly PlaylistSection[]) => void,
	move: EntriesBlockMove
): { revert: () => void } | null {
	const result = moveEntriesLocally(pane.rows, sections, move);
	if (result === null) return null;
	const prior = {
		rows: pane.rows,
		selected_orders: pane.selected_orders,
		selected_order: pane.selected_order
	};
	const remap = (order: number): number => result.orderMap.get(order) ?? order;
	pane.rows = result.rows;
	pane.selected_orders = pane.selected_orders.map(remap);
	pane.selected_order = pane.selected_order === null ? null : remap(pane.selected_order);
	if (sections.length > 0) setSections(result.sections);
	return {
		revert: () => {
			pane.rows = prior.rows;
			pane.selected_orders = prior.selected_orders;
			pane.selected_order = prior.selected_order;
			if (sections.length > 0) setSections(sections);
		}
	};
}

/**
 * The insertion line's top, px, in the coordinates `bodyTop` is given in
 * (the tbody's top inside the scroll container). Gap g opens above slot g
 * and the slots below it slide down one row, so the line sits centred in
 * that open row: inside a section when the gap is below its divider, above
 * the divider when the gap is above it.
 */
export function dropLineTop(gap: number, rowHeight: number, bodyTop: number): number {
	return bodyTop + gap * rowHeight + rowHeight / 2 - DROP_LINE_PX / 2;
}
