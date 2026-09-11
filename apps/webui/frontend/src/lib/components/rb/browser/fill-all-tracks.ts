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
