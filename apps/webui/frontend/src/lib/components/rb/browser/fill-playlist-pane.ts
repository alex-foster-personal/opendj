/**
 * Playlist progressive fill (PERF-UI-05, issue #3530).
 *
 * Page 1 publishes and clears the blocking spinner; remaining offset pages
 * append only while the load seq is current.
 *
 * LIBM-134: page 1 stays small (first paint, budget L3) and every later page
 * is PLAYLIST_FILL_PAGE rows, so a 10k-member playlist fills in ~21 requests
 * rather than ~335. Requests stay sequential: the agentbox3 bench on Thu 1 Oct
 * 2026 measured 2 in flight no faster and 3-4 slower (the engine is GIL-bound).
 */

import {
	appendRows,
	finishFill,
	publishFirstPage,
	type ProgressiveLoadPane
} from './pane-progressive-load';

/** First-page size for playlist switch paint (visible rows ~15-25). */
export const PLAYLIST_FIRST_PAGE = 30;

/** Every page after the first. Must not exceed the route's `limit` cap
 * (`le=500` on `GET /api/v1/playlists/{id}/tracks`); a pytest pins both. */
export const PLAYLIST_FILL_PAGE = 500;

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
	fetchPage: (
		offset: number,
		limit: number
	) => Promise<{ page: PlaylistTracksPage<T>; etag: string }>;
	mapRow: (item: T, order: number) => Row;
	progressTotal: number | null;
	onFirstPaint?: (decomposition: { fetchMs: number; paintMs: number }) => void;
	onComplete?: (info: { fetchMs: number; rows: number }) => void;
	onFillError?: (error: string) => void;
}): Promise<void> {
	const { pane, seq, fetchPage, mapRow, progressTotal } = opts;
	const startedAt = performance.now();
	let painted = false;
	let offset = 0;
	let total: number | null = progressTotal;
	let finishedClean = false;

	try {
		while (pane.isCurrentLoad(seq)) {
			const limit = painted ? PLAYLIST_FILL_PAGE : PLAYLIST_FIRST_PAGE;
			const fetchStartedAt = performance.now();
			const { page, etag } = await fetchPage(offset, limit);
			const fetchMs = performance.now() - fetchStartedAt;
			if (!pane.isCurrentLoad(seq)) return;
			total = page.total;
			const rows = page.tracks.map((item, i) => mapRow(item, offset + i + 1));
			if (!painted) {
				const paintStartedAt = performance.now();
				if (!publishFirstPage(pane, seq, rows)) return;
				pane.etag = etag;
				painted = true;
				pane.updateLoadProgress(seq, rows.length, total);
				const paintMs = performance.now() - paintStartedAt;
				opts.onFirstPaint?.({ fetchMs, paintMs });
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
