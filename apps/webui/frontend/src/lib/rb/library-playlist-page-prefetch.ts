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

const inflight = new Map<string, Promise<PlaylistPageResult>>();

let fetchPlaylistPage: FetchPlaylistPage = (playlistId, limit, offset) =>
	listPlaylistTracksPage(playlistId, { limit, offset });

/** Test seam: inject a fake page fetch without mocking production api-rb. */
export function setFetchPlaylistPageForTests(fn: FetchPlaylistPage | null): void {
	fetchPlaylistPage = fn ?? ((playlistId, limit, offset) =>
		listPlaylistTracksPage(playlistId, { limit, offset }));
}

/** Test-only reset for the in-flight map and injected seam. */
export function resetPlaylistPagePrefetchForTests(): void {
	inflight.clear();
	setFetchPlaylistPageForTests(null);
}

/** Idempotent: concurrent callers share one in-flight first-page GET. */
export function prefetchPlaylistFirstPage(
	playlistId: string,
	pageSize = PLAYLIST_PREFETCH_PAGE_SIZE
): void {
	if (inflight.has(playlistId)) return;
	const promise = fetchPlaylistPage(playlistId, pageSize, 0);
	inflight.set(playlistId, promise);
	promise.catch(() => {
		inflight.delete(playlistId);
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

/** First-page fetch: join prefetch when offset is 0, else live GET. */
export async function fetchPlaylistFirstPage(
	playlistId: string,
	offset: number,
	pageSize = PLAYLIST_PREFETCH_PAGE_SIZE
): Promise<PlaylistPageResult> {
	if (offset !== 0) {
		return fetchPlaylistPage(playlistId, pageSize, offset);
	}
	let promise = inflight.get(playlistId);
	if (promise === undefined) {
		promise = fetchPlaylistPage(playlistId, pageSize, 0);
		inflight.set(playlistId, promise);
	}
	try {
		return await promise;
	} catch (error) {
		inflight.delete(playlistId);
		return fetchPlaylistPage(playlistId, pageSize, 0);
	}
}
