/**
 * Playlist progressive fill (PERF-UI-05, issue #3530).
 *
 * Page 1 publishes and clears the blocking spinner; remaining offset pages
 * append only while the load seq is current.
 */

import {
	appendRows,
	finishFill,
	publishFirstPage,
	type ProgressiveLoadPane
} from './pane-progressive-load';

/** First-page size for playlist switch paint (visible rows ~15-25). */
export const PLAYLIST_FIRST_PAGE = 30;

export interface PlaylistTracksPage<T> {
	tracks: T[];
	total: number;
	next_offset: number | null;
}

export interface FillPlaylistPane<Row = unknown> extends ProgressiveLoadPane<Row> {
	updateLoadProgress(seq: number, loaded: number, total: number | null): boolean;
}

export async function fillPlaylistPane<T, Row>(opts: {
	pane: FillPlaylistPane<Row>;
	seq: number;
	pageSize?: number;
	fetchPage: (offset: number) => Promise<{ page: PlaylistTracksPage<T>; etag: string }>;
	mapRow: (item: T, order: number) => Row;
	progressTotal: number | null;
	onFirstPaint?: () => void;
	onComplete?: (info: { fetchMs: number; rows: number }) => void;
	onFillError?: (error: string) => void;
}): Promise<void> {
	const { pane, seq, fetchPage, mapRow, progressTotal } = opts;
	const pageSize = opts.pageSize ?? PLAYLIST_FIRST_PAGE;
	const startedAt = performance.now();
	let painted = false;
	let offset = 0;
	let total: number | null = progressTotal;
	let finishedClean = false;

	try {
		while (pane.isCurrentLoad(seq)) {
			const { page, etag } = await fetchPage(offset);
			if (!pane.isCurrentLoad(seq)) return;
			total = page.total;
			const rows = page.tracks.map((item, i) => mapRow(item, offset + i + 1));
			if (!painted) {
				if (!publishFirstPage(pane, seq, rows)) return;
				pane.etag = etag;
				painted = true;
				pane.updateLoadProgress(seq, rows.length, total);
				opts.onFirstPaint?.();
			} else {
				appendRows(pane, seq, rows);
				pane.updateLoadProgress(seq, pane.rows.length, total);
			}
			if (page.next_offset === null || page.tracks.length === 0) {
				finishedClean = finishFill(pane, seq, false);
				break;
			}
			offset = page.next_offset;
			if (offset >= page.total) {
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
