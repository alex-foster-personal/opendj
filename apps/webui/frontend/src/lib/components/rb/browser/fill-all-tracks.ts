/**
 * All Tracks progressive fill (issue #318).
 *
 * Page 1 publishes and clears the blocking spinner; remaining cursor
 * pages append only while the load seq is current. Pagination bugs
 * after first paint toast and mark truncated without hiding the table.
 * A throw before first paint is rethrown so the caller can failLoad.
 */

import {
	appendRows,
	finishFill,
	publishFirstPage,
	type ProgressiveLoadPane
} from './pane-progressive-load';
import { forEachCursorPage, type CursorPage } from './virtual-window';

export interface FillAllTracksPane<Row = unknown> extends ProgressiveLoadPane<Row> {
	updateLoadProgress(seq: number, loaded: number, total: number | null): boolean;
}

export async function fillAllTracksPane<T, Row>(opts: {
	pane: FillAllTracksPane<Row>;
	seq: number;
	fetchPage: (cursor: string | undefined) => Promise<CursorPage<T>>;
	mapRow: (item: T, order: number) => Row;
	progressTotal: number | null;
	onFirstPaint?: () => void;
	onComplete?: (info: { fetchMs: number; rows: number }) => void;
	onFillError?: (error: string) => void;
}): Promise<void> {
	const { pane, seq, fetchPage, mapRow, progressTotal } = opts;
	const startedAt = performance.now();
	let painted = false;
	let finishedClean = false;
	try {
		await forEachCursorPage(fetchPage, {
			shouldContinue: () => pane.isCurrentLoad(seq),
			onPage: (info) => {
				if (!pane.isCurrentLoad(seq)) return;
				if (!painted) {
					const rows = info.items.map((item, i) => mapRow(item, i + 1));
					if (!publishFirstPage(pane, seq, rows)) return;
					painted = true;
					pane.updateLoadProgress(seq, info.loaded, progressTotal);
					opts.onFirstPaint?.();
				} else {
					const extra = info.items.map((item, i) =>
						mapRow(item, pane.rows.length + i + 1)
					);
					appendRows(pane, seq, extra);
					pane.updateLoadProgress(seq, info.loaded, progressTotal);
				}
				if (info.done) {
					finishedClean = finishFill(pane, seq, false);
				}
			}
		});
	} catch (exc) {
		if (!painted) throw exc;
		if (!pane.isCurrentLoad(seq)) return;
		opts.onFillError?.(String(exc));
		finishFill(pane, seq, true);
		return;
	}
	if (finishedClean) {
		opts.onComplete?.({
			fetchMs: performance.now() - startedAt,
			rows: pane.rows.length
		});
	}
}

const mappedIndexRows = new WeakMap<readonly unknown[], unknown[]>();

/** The index's rows, mapped once per index (LIBM-171). Mapped rows are never
 * mutated (a pane's $state proxies writes), so every pane may share them. */
export function rowsForIndex<T, Row>(
	items: readonly T[],
	mapRow: (item: T, order: number) => Row
): Row[] {
	const cached = mappedIndexRows.get(items) as Row[] | undefined;
	if (cached !== undefined) return cached;
	const rows = items.map((item, i) => mapRow(item, i + 1));
	mappedIndexRows.set(items, rows);
	return rows;
}

/**
 * All Tracks from the browser's library index (LIBM-171): the industry shape,
 * one local index resolved per view, never a re-walk per switch.
 *
 * A held index paints at once with no request. A stale one (the library has
 * changed since) paints at once too, then the current index replaces it. With
 * no index yet, the boot first page paints while the whole index arrives in
 * ONE request. A failure before any paint is rethrown for the caller's failLoad.
 */
export async function fillAllTracksFromIndex<T, Row>(opts: {
	pane: FillAllTracksPane<Row>;
	seq: number;
	held: { items: readonly T[]; current: boolean } | null;
	loadIndex: () => Promise<readonly T[]>;
	firstPage: () => Promise<CursorPage<T>>;
	mapRow: (item: T, order: number) => Row;
	progressTotal: number | null;
	onFirstPaint?: () => void;
	onComplete?: (info: { fetchMs: number; rows: number }) => void;
	onFillError?: (error: string) => void;
}): Promise<void> {
	const { pane, seq, held, mapRow } = opts;
	const startedAt = performance.now();
	let painted = false;
	const paint = (rows: Row[]): boolean => {
		if (!publishFirstPage(pane, seq, rows)) return false;
		painted = true;
		pane.updateLoadProgress(seq, rows.length, opts.progressTotal);
		opts.onFirstPaint?.();
		return true;
	};
	if (held !== null) {
		if (!paint(rowsForIndex(held.items, mapRow))) return;
		if (held.current) {
			if (finishFill(pane, seq, false)) {
				opts.onComplete?.({ fetchMs: performance.now() - startedAt, rows: pane.rows.length });
			}
			return;
		}
	}
	const index = opts.loadIndex();
	try {
		if (!painted) {
			const first = await Promise.race([
				index.then(() => null),
				opts.firstPage().catch(() => null)
			]);
			if (first !== null && !painted && !paint(first.items.map((item, i) => mapRow(item, i + 1)))) return;
		}
		const items = await index;
		if (!pane.isCurrentLoad(seq)) return;
		const rows = rowsForIndex(items, mapRow);
		if (painted) pane.rows = rows;
		else if (!paint(rows)) return;
		if (finishFill(pane, seq, false)) {
			opts.onComplete?.({ fetchMs: performance.now() - startedAt, rows: rows.length });
		}
	} catch (exc) {
		if (!painted) throw exc;
		if (!pane.isCurrentLoad(seq)) return;
		opts.onFillError?.(String(exc));
		finishFill(pane, seq, true);
	}
}
