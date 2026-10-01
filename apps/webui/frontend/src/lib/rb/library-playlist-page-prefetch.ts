/**
 * Playlist first-page prefetch and in-flight join (PERF-UI-05, issue #3746).
 *
 * Mirrors library-boot-hydration's boot tracks join: tree intent starts
 * GET /playlists/{id}/tracks?offset=0 before the click, and fillPlaylistPane
 * joins the same promise instead of cold-fetching.
 */

import {
	listPlaylistTracksPage,
	type PlaylistTracksPageHydrated
} from '$lib/rb/api-rb';

/** Must match fill-playlist-pane PLAYLIST_FIRST_PAGE. */
export const PLAYLIST_PREFETCH_PAGE_SIZE = 30;

/** Cap eager tree prefetches so large libraries do not stampede the daemon. */
export const PLAYLIST_TREE_PREFETCH_MAX = 8;

export type PlaylistPageResult = {
	page: PlaylistTracksPageHydrated;
	etag: string;
};

type FetchPlaylistPage = (
	playlistId: string,
	limit: number,
	offset: number
) => Promise<PlaylistPageResult>;

/** A prefetch older than this is not joined: the switch fetches fresh rows. */
export const PLAYLIST_PREFETCH_MAX_AGE_MS = 15_000;

type PrefetchEntry = {
	promise: Promise<PlaylistPageResult>;
	pageSize: number;
	startedAt: number;
};

/** Prefetches not yet consumed by a switch. A switch takes its entry out, so
 * every later switch to the same playlist reads the route again: a page is
 * joined at most once and never served as a cache. */
const inflight = new Map<string, PrefetchEntry>();

let fetchPlaylistPage: FetchPlaylistPage = (playlistId, limit, offset) =>
	listPlaylistTracksPage(playlistId, { limit, offset });

let now: () => number = () => performance.now();

/** Test seam: inject a fake page fetch without mocking production api-rb. */
export function setFetchPlaylistPageForTests(fn: FetchPlaylistPage | null): void {
	fetchPlaylistPage = fn ?? ((playlistId, limit, offset) =>
		listPlaylistTracksPage(playlistId, { limit, offset }));
}

/** Test seam: inject the clock that ages prefetches. */
export function setPrefetchClockForTests(fn: (() => number) | null): void {
	now = fn ?? (() => performance.now());
}

/** Test-only reset for the in-flight map and injected seams. */
export function resetPlaylistPagePrefetchForTests(): void {
	inflight.clear();
	setFetchPlaylistPageForTests(null);
	setPrefetchClockForTests(null);
}

function isFresh(entry: PrefetchEntry): boolean {
	return now() - entry.startedAt <= PLAYLIST_PREFETCH_MAX_AGE_MS;
}

/** Idempotent while a fresh prefetch is pending or unconsumed. */
export function prefetchPlaylistFirstPage(
	playlistId: string,
	pageSize = PLAYLIST_PREFETCH_PAGE_SIZE
): void {
	const existing = inflight.get(playlistId);
	if (existing !== undefined && isFresh(existing)) return;
	const promise = fetchPlaylistPage(playlistId, pageSize, 0);
	const entry: PrefetchEntry = { promise, pageSize, startedAt: now() };
	inflight.set(playlistId, entry);
	promise.catch(() => {
		if (inflight.get(playlistId) === entry) inflight.delete(playlistId);
	});
}

/** Prefetch up to max playlist first pages (tree-visible intent). */
export function prefetchPlaylistTreeIntent(
	playlistIds: readonly string[],
	max = PLAYLIST_TREE_PREFETCH_MAX
): void {
	for (const playlistId of playlistIds.slice(0, max)) {
		prefetchPlaylistFirstPage(playlistId);
	}
}

/** Page fetch for a switch: at offset 0, consume a fresh prefetch of the same
 * size if one exists (falling back to a live GET if it failed); otherwise a
 * live GET. */
export async function fetchPlaylistFirstPage(
	playlistId: string,
	offset: number,
	pageSize = PLAYLIST_PREFETCH_PAGE_SIZE
): Promise<PlaylistPageResult> {
	if (offset !== 0) {
		return fetchPlaylistPage(playlistId, pageSize, offset);
	}
	const entry = inflight.get(playlistId);
	inflight.delete(playlistId);
	if (entry === undefined || entry.pageSize !== pageSize || !isFresh(entry)) {
		return fetchPlaylistPage(playlistId, pageSize, 0);
	}
	try {
		return await entry.promise;
	} catch {
		return fetchPlaylistPage(playlistId, pageSize, 0);
	}
}
