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

/** Playlists whose rows are held for an instant switch back (LIBM-171). */
export const PLAYLIST_ROW_CACHE_LIMIT = 24;

const playlistRowCache = new Map<string, { rows: unknown[]; etag: string }>();

/** Forget every held playlist: BrowserPanel calls this on each library change,
 * so a held copy is only ever the rows as of the last change seen. */
export function clearPlaylistRowCache(): void {
	playlistRowCache.clear();
}

function _holdPlaylistRows(key: string, rows: unknown[], etag: string): void {
	playlistRowCache.delete(key);
	playlistRowCache.set(key, { rows, etag });
	while (playlistRowCache.size > PLAYLIST_ROW_CACHE_LIMIT) {
		const oldest = playlistRowCache.keys().next().value;
		if (oldest === undefined) break;
		playlistRowCache.delete(oldest);
	}
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
	/** LIBM-171: hold the filled rows under this key; a later fill with the same
	 * key paints them at once and only refetches when the membership ETag moved. */
	cacheKey?: string;
	onFirstPaint?: (decomposition: { fetchMs: number; paintMs: number }) => void;
	onComplete?: (info: { fetchMs: number; rows: number }) => void;
	onFillError?: (error: string) => void;
}): Promise<void> {
	const { pane, seq, fetchPage, mapRow, progressTotal, cacheKey } = opts;
	const startedAt = performance.now();
	let painted = false;
	let offset = 0;
	let total: number | null = progressTotal;
	let finishedClean = false;
	const filled: Row[] = [];
	const held = cacheKey === undefined ? undefined : playlistRowCache.get(cacheKey);
	let prefetched: { page: PlaylistTracksPage<T>; etag: string; fetchMs: number } | null = null;

	try {
		if (held !== undefined && cacheKey !== undefined) {
			const paintStartedAt = performance.now();
			if (!publishFirstPage(pane, seq, held.rows as Row[])) return;
			pane.etag = held.etag;
			opts.onFirstPaint?.({ fetchMs: 0, paintMs: performance.now() - paintStartedAt });
			_holdPlaylistRows(cacheKey, held.rows, held.etag);
			const fetchStartedAt = performance.now();
			const first = await fetchPage(0, PLAYLIST_FIRST_PAGE);
			if (!pane.isCurrentLoad(seq)) return;
			if (first.etag === held.etag && first.page.total === held.rows.length) {
				if (finishFill(pane, seq, false)) {
					opts.onComplete?.({ fetchMs: performance.now() - startedAt, rows: held.rows.length });
				}
				return;
			}
			prefetched = { ...first, fetchMs: performance.now() - fetchStartedAt };
		}
		while (pane.isCurrentLoad(seq)) {
			const limit = painted ? PLAYLIST_FILL_PAGE : PLAYLIST_FIRST_PAGE;
			const fetchStartedAt = performance.now();
			const { page, etag } = prefetched ?? (await fetchPage(offset, limit));
			const fetchMs = prefetched?.fetchMs ?? performance.now() - fetchStartedAt;
			prefetched = null;
			if (!pane.isCurrentLoad(seq)) return;
			total = page.total;
			const rows = page.tracks.map((item, i) => mapRow(item, offset + i + 1));
			filled.push(...rows);
			if (!painted) {
				const paintStartedAt = performance.now();
				if (!publishFirstPage(pane, seq, rows)) return;
				pane.etag = etag;
				painted = true;
				pane.updateLoadProgress(seq, rows.length, total);
				if (held === undefined) {
					opts.onFirstPaint?.({ fetchMs, paintMs: performance.now() - paintStartedAt });
				}
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
		if (!painted && held === undefined) throw exc;
		if (!pane.isCurrentLoad(seq)) return;
		opts.onFillError?.(String(exc));
		finishFill(pane, seq, true);
		return;
	}
	if (finishedClean) {
		if (cacheKey !== undefined) _holdPlaylistRows(cacheKey, filled, pane.etag);
		opts.onComplete?.({
			fetchMs: performance.now() - startedAt,
			rows: pane.rows.length
		});
	}
}
