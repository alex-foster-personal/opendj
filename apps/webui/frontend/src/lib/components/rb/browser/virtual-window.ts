/**
 * Track-list virtualization (browser-surface unit, beyond-500-cap lane).
 *
 * Two independent concerns, both pure/testable against fixtures:
 * - computeVirtualWindow: row-window math for TrackTable's DOM
 *   virtualization (which row indices to actually mount, given scroll
 *   position + row height + an overscan buffer).
 * - forEachCursorPage / fetchAllPages: a generic cursor-following fetch
 *   loop so the 500-row server page cap no longer becomes a client-side
 *   truncation - callers supply the page fetcher, this walks next_cursor
 *   to exhaustion (or until shouldContinue says stop).
 */

// ------------------------------------------------------------ row window

/** Sticky TrackTable thead height. Must match `thead th { height: 20px }`. */
export const TRACK_TABLE_THEAD_PX = 20;

export interface VirtualWindow {
	/** First row index to render (inclusive). */
	startIndex: number;
	/** Last row index to render (exclusive). */
	endIndex: number;
	/** Spacer height (px) standing in for rows above startIndex. */
	topPad: number;
	/** Spacer height (px) standing in for rows below endIndex. */
	bottomPad: number;
}

/** Which row range to mount for a scrolled, fixed-row-height table.
 * Fail-fast on a non-positive rowHeight (would divide-by-zero into NaN
 * indices); rowCount <= 0 or a non-finite viewport both resolve to an
 * empty window rather than throwing (both are real transient UI states -
 * an empty pane, a not-yet-measured wrapper). */
export function computeVirtualWindow(params: {
	scrollTop: number;
	viewportHeight: number;
	rowHeight: number;
	rowCount: number;
	overscan: number;
	/** Sticky header height inside the scrollport (e.g. TrackTable thead). */
	headerOffsetPx?: number;
}): VirtualWindow {
	const { scrollTop, viewportHeight, rowHeight, rowCount, overscan, headerOffsetPx = 0 } =
		params;
	if (rowHeight <= 0) {
		throw new Error(`computeVirtualWindow: rowHeight must be positive, got ${rowHeight}`);
	}
	if (rowCount <= 0 || viewportHeight <= 0) {
		return { startIndex: 0, endIndex: 0, topPad: 0, bottomPad: 0 };
	}
	// Clamp BEFORE deriving start/end: scrollTop is caller-owned state that
	// can still reflect a deeper list (e.g. a search/filter just shrank
	// rowCount out from under an unchanged scroll position). Without this,
	// only endIndex would clamp to rowCount while startIndex stayed past
	// it, producing an empty slice behind a stale, oversized top spacer.
	const maxFirstVisible = Math.max(0, rowCount - 1);
	const firstVisible = Math.min(
		maxFirstVisible,
		Math.floor(Math.max(0, scrollTop) / rowHeight)
	);
	const visibleCount = Math.ceil(Math.max(0, viewportHeight - headerOffsetPx) / rowHeight);
	const startIndex = Math.max(0, firstVisible - overscan);
	const endIndex = Math.min(rowCount, firstVisible + visibleCount + overscan);
	return {
		startIndex,
		endIndex,
		topPad: startIndex * rowHeight,
		bottomPad: (rowCount - endIndex) * rowHeight
	};
}

/** Scroll position that places a row at the given offset from the top of the
 * row-visible band (below a sticky header). */
export function scrollTopForRowIndex(params: {
	rowIndex: number;
	rowHeight: number;
	headerOffsetPx?: number;
	offsetFromTopPx?: number;
}): number {
	const header = params.headerOffsetPx ?? 0;
	const offset = params.offsetFromTopPx ?? 0;
	const rowPixels = params.rowIndex * params.rowHeight - offset;
	// Row 0 is already visible at scrollTop 0 below the sticky header.
	return Math.max(0, rowPixels + (params.rowIndex > 0 ? header : 0));
}

/** Preserve a row's offset from the top of the visible band across a viewport
 * resize (e.g. MORE/LESS deck layout). Clamps so the row stays intersecting. */
export function scrollTopToKeepRowVisible(params: {
	rowIndex: number;
	rowHeight: number;
	headerOffsetPx?: number;
	viewportHeight: number;
	priorScrollTop: number;
	priorViewportHeight: number;
}): number {
	const header = params.headerOffsetPx ?? 0;
	const { rowIndex, rowHeight, priorScrollTop, priorViewportHeight, viewportHeight } = params;
	if (rowIndex < 0 || viewportHeight <= 0 || priorViewportHeight <= 0) {
		return Math.max(0, priorScrollTop);
	}
	const rowTop = header + rowIndex * rowHeight;
	const priorVisibleTop = priorScrollTop + header;
	const offsetFromTop = Math.max(0, rowTop - priorVisibleTop);
	let next = scrollTopForRowIndex({
		rowIndex,
		rowHeight,
		headerOffsetPx: header,
		offsetFromTopPx: offsetFromTop
	});
	const rowBottom = rowTop + rowHeight;
	if (rowTop < next + header) next = Math.max(0, rowTop - header);
	if (rowBottom > next + viewportHeight) next = Math.max(0, rowBottom - viewportHeight);
	return next;
}

/** Whether a row is scrolled above or below the row-visible band. */
export function masterFoldVisibility(params: {
	rowIndex: number;
	rowHeight: number;
	scrollTop: number;
	viewportHeight: number;
	headerOffsetPx?: number;
	slopPx?: number;
}): 'above' | 'below' | null {
	const { rowIndex, rowHeight, scrollTop, viewportHeight } = params;
	if (rowIndex < 0 || viewportHeight <= 0) return null;
	const header = params.headerOffsetPx ?? 0;
	const slop = params.slopPx ?? 2;
	const top = header + rowIndex * rowHeight;
	const bottom = top + rowHeight;
	if (bottom <= scrollTop + header + slop) return 'above';
	if (top >= scrollTop + viewportHeight - slop) return 'below';
	return null;
}

// ------------------------------------------------------- cursor pagination

/** One cursor-paginated page, matching TracksPageHydrated (api-rb.ts) -
 * duck-typed here so this module stays independent of api-rb's types. */
export interface CursorPage<T> {
	items: T[];
	next_cursor: string | null;
}

/** One completed cursor page. `done` is `next_cursor === null`. */
export interface CursorPageInfo<T> {
	items: T[];
	loaded: number;
	pageCount: number;
	done: boolean;
}

/** Walk a cursor-paginated endpoint, firing `onPage` after every successful
 * fetch (including the confirming empty page for an exact N * pageSize
 * library). maxPages is a runaway-loop safety ceiling (a backend that
 * never returns next_cursor: null is a real bug to surface loudly, not
 * silently truncate) - NOT a library-size cap, and must not become one:
 * the backend's cursor is a "maybe more" heuristic (it hands back a
 * non-null cursor whenever a page comes back full, whether or not a next
 * page actually has rows - apps/webui/server/backend.py's
 * `next_cursor = page[-1].stable_id if len(page) == limit else None`), so
 * ANY library whose size is an exact multiple of the page size needs one
 * extra confirming empty-page fetch before next_cursor goes null. A low
 * ceiling turns that ordinary case into a false "pagination bug" failure
 * for a real, if large, library - default is generous enough that no
 * plausible real library reaches it (500/page * 20000 = 10,000,000 rows)
 * while still being finite so a truly broken backend (cursor never
 * advancing) fails loudly instead of looping forever.
 *
 * `shouldContinue` is cooperative cancel: when it returns false, stop
 * requesting further pages and return without throwing. A cancelled load
 * is not a pagination bug. */
export async function forEachCursorPage<T>(
	fetchPage: (cursor: string | undefined) => Promise<CursorPage<T>>,
	opts: {
		maxPages?: number;
		shouldContinue?: () => boolean;
		onPage: (info: CursorPageInfo<T>) => void;
	}
): Promise<void> {
	const maxPages = opts.maxPages ?? 20000;
	if (!Number.isSafeInteger(maxPages) || maxPages <= 0) {
		throw new Error(`fetchAllPages: maxPages must be a positive integer, got ${maxPages}`);
	}
	let cursor: string | undefined;
	let pages = 0;
	let loaded = 0;
	const seenCursors = new Set<string>();
	for (;;) {
		if (opts.shouldContinue?.() === false) return;
		if (cursor !== undefined) {
			if (seenCursors.has(cursor)) {
				throw new Error(
					`fetchAllPages: repeated cursor ${JSON.stringify(cursor)} - likely a pagination bug`
				);
			}
			seenCursors.add(cursor);
		}
		const page = await fetchPage(cursor);
		pages += 1;
		loaded += page.items.length;
		const next = page.next_cursor;
		opts.onPage({ items: page.items, loaded, pageCount: pages, done: next === null });
		if (next === null) return;
		if (pages >= maxPages) {
			throw new Error(
				`fetchAllPages: exceeded ${maxPages} pages without next_cursor going null - ` +
					'likely a pagination bug, not a real library size'
			);
		}
		cursor = next;
	}
}

/** Walk a cursor-paginated endpoint to exhaustion, concatenating every
 * page's items in order. Thin wrapper over {@link forEachCursorPage} so
 * ColumnBrowser and the All Tracks refresh path stay byte-compatible. */
export async function fetchAllPages<T>(
	fetchPage: (cursor: string | undefined) => Promise<CursorPage<T>>,
	opts: {
		maxPages?: number;
		/** Called after every successful page append, including the final
		 * confirming empty page - the honest, page-granular progress signal
		 * for ad59ac's load indicator. `loaded` is cumulative rows fetched so
		 * far; `pageCount` is how many page fetches have landed. Never
		 * interpolated between pages - a stalled request simply does not
		 * call this again, which is what makes a hang visible. */
		onPage?: (info: { loaded: number; pageCount: number }) => void;
	} = {}
): Promise<T[]> {
	const out: T[] = [];
	await forEachCursorPage(fetchPage, {
		...(opts.maxPages !== undefined ? { maxPages: opts.maxPages } : {}),
		onPage: (info) => {
			out.push(...info.items);
			opts.onPage?.({ loaded: info.loaded, pageCount: info.pageCount });
		}
	});
	return out;
}

export { createRowVisibilityObserver } from './observe-row';
