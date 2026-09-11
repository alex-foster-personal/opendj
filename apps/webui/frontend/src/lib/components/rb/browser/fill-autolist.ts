/**
 * Autolist progressive fill (issue #2066).
 */
import { hasAutolistSelection, type AutolistSelection } from '$lib/smartlists/autolist-rule';
import type { AutolistQueryResult } from '$lib/rb/api-autolists';
import {
	appendRows,
	finishFill,
	publishFirstPage,
	type ProgressiveLoadPane
} from './pane-progressive-load';

export interface FillAutolistPane<Row = unknown> extends ProgressiveLoadPane<Row> {
	updateLoadProgress(seq: number, loaded: number, total: number | null): boolean;
}

export async function fillAutolistPane<T, Row>(opts: {
	pane: FillAutolistPane<Row>;
	seq: number;
	selection: AutolistSelection;
	pageSize: number;
	fetchPage: (offset: number, limit: number) => Promise<AutolistQueryResult>;
	mapRow: (item: T, order: number) => Row;
	onFirstPaint?: () => void;
	onComplete?: (info: { fetchMs: number; rows: number }) => void;
	onFillError?: (error: string) => void;
}): Promise<void> {
	const { pane, seq, selection, pageSize, fetchPage, mapRow } = opts;
	const startedAt = performance.now();

	if (!hasAutolistSelection(selection)) {
		if (publishFirstPage(pane, seq, [])) {
			finishFill(pane, seq, false);
		}
		return;
	}

	let painted = false;
	let offset = 0;
	let total: number | null = null;
	let finishedClean = false;

	try {
		while (pane.isCurrentLoad(seq)) {
			const page = await fetchPage(offset, pageSize);
			if (!pane.isCurrentLoad(seq)) return;
			total = page.total;
			const rows = page.tracks.map((item, i) =>
				mapRow(item as T, offset + i + 1)
			);
			if (!painted) {
				if (!publishFirstPage(pane, seq, rows)) return;
				painted = true;
				pane.updateLoadProgress(seq, rows.length, total);
				opts.onFirstPaint?.();
			} else {
				appendRows(pane, seq, rows);
				pane.updateLoadProgress(seq, pane.rows.length, total);
			}
			offset += page.tracks.length;
			if (offset >= page.total || page.tracks.length === 0) {
				finishedClean = finishFill(pane, seq, false);
				break;
			}
			// Yield after first page so main thread stays responsive.
			await Promise.resolve();
		}
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
