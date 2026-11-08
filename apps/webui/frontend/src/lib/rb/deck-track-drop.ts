import type { DeckId } from './deck-slots';
import { noteRecentDeck } from '$lib/rb/recent-deck';
import { trackDragRefusal, type DraggableRow } from './track-drag-refusal';

type DeckLoadCommand =
	| { type: 'unload'; deck: DeckId }
	| {
			type: 'load';
			deck: DeckId;
			stable_id: string;
			key_shift?: number;
			edit_id?: string;
			in_ms?: number;
			out_ms?: number;
	  };

/** A dragged row's refusal flags plus the key shift its row displayed
 * (KEYSHIFT-03: a row the compatible filter matched after a shift loads
 * shifted, whether it is double-clicked or dragged). */
export type DraggedDeckRow = DraggableRow & {
	key_shift?: number;
	edit_id?: string;
	in_ms?: number;
	out_ms?: number;
};

export async function applyDeckTrackDrop(args: {
	deckId: DeckId;
	occupied: boolean;
	stableId: string;
	row: DraggedDeckRow | null;
	dispatch: (command: DeckLoadCommand) => Promise<unknown>;
	toast: (message: string, kind: 'error') => void;
}): Promise<void> {
	if (args.row !== null) {
		const why = trackDragRefusal(args.row);
		if (why !== null) {
			args.toast(why, 'error');
			return;
		}
	}
	try {
		noteRecentDeck(args.deckId);
		if (args.occupied) {
			await args.dispatch({ type: 'unload', deck: args.deckId });
		}
		const shift = args.row?.key_shift ?? 0;
		const row = args.row;
		await args.dispatch({
			type: 'load',
			deck: args.deckId,
			stable_id: args.stableId,
			...(shift === 0 ? {} : { key_shift: shift }),
			...(row?.edit_id ? { edit_id: row.edit_id } : {}),
			...(row?.in_ms != null ? { in_ms: row.in_ms } : {}),
			...(row?.out_ms != null ? { out_ms: row.out_ms } : {})
		});
	} catch (error: unknown) {
		const message = error instanceof Error ? error.message : String(error);
		args.toast(`drop load failed: ${message}`, 'error');
	}
}

/** The fields of a library row the drag metadata is built from. */
export interface DragSourceRow {
	stable_id: string;
	file_exists: boolean | null;
	file_availability?: DraggableRow['file_availability'];
	is_streaming?: boolean | null;
	rb_meta?: { is_streaming?: boolean | null } | null;
	offload?: { state: string } | null | undefined;
	edit_id?: string | null;
	in_ms?: number | null;
	out_ms?: number | null;
}

/** Drag metadata for EVERY dragged id, not just the row under the pointer: a
 * multi-row drag drops its first selected id on the deck, which must still get
 * its own refusal flags and displayed key shift (review on PR #6574). An id
 * whose row is not loaded in the table gets no entry, as before. */
export function draggedDeckRows<R extends DragSourceRow>(
	ids: readonly string[],
	rows: readonly R[],
	shiftFor: (row: R) => number,
	primary?: R | null
): Record<string, DraggedDeckRow> {
	const byId = new Map(rows.map((r) => [r.stable_id, r] as const));
	const out: Record<string, DraggedDeckRow> = {};
	for (const id of ids) {
		const row = byId.get(id);
		if (row === undefined) continue;
		const primaryRow =
			primary !== undefined && primary !== null && primary.stable_id === id ? primary : null;
		const editSource = primaryRow ?? row;
		const eid = editSource.edit_id;
		const inMs = editSource.in_ms;
		const outMs = editSource.out_ms;
		out[id] = {
			file_exists: row.file_exists,
			...(row.file_availability === undefined ? {} : { file_availability: row.file_availability }),
			is_streaming: row.is_streaming ?? row.rb_meta?.is_streaming ?? false,
			...(row.offload === undefined || row.offload === null
				? {}
				: { offload: { state: row.offload.state } }),
			key_shift: shiftFor(row),
			...(eid ? { edit_id: eid } : {}),
			...(inMs != null ? { in_ms: inMs } : {}),
			...(outMs != null ? { out_ms: outMs } : {})
		};
	}
	return out;
}
