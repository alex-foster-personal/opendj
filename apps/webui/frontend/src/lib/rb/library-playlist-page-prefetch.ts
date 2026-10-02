/**
 * Playlist first-page prefetch and in-flight join (PERF-UI-05, issue #3746).
 *
 * Mirrors library-boot-hydration's boot tracks join: tree intent starts
 * GET /playlists/{id}/tracks?offset=0 before the click, and fillPlaylistPane
 * joins the same promise instead of cold-fetching.
 */

import { listPlaylistTracksPage, type PlaylistTracksPageHydrated } from '$lib/rb/api-rb';

/** Must match fill-playlist-pane PLAYLIST_FIRST_PAGE (a unit test pins both). */
export const PLAYLIST_PREFETCH_PAGE_SIZE = 30;

/** Cap eager tree prefetches so large libraries do not stampede the daemon. */
export const PLAYLIST_TREE_PREFETCH_MAX = 8;

/** A prefetch older than this is not joined: the switch fetches fresh rows. */
export const PLAYLIST_PREFETCH_MAX_AGE_MS = 15_000;

export type PlaylistPageResult = {
	page: PlaylistTracksPageHydrated;
	etag: string;
};

type FetchPlaylistPage = (
	playlistId: string,
	limit: number,
	offset: number
) => Promise<PlaylistPageResult>;

type PrefetchEntry = {
	promise: Promise<PlaylistPageResult>;
	pageSize: number;
	startedAt: number;
};

/**
 * One prefetch map over an injected page fetch and clock. The app uses the
 * shared instance below; tests build their own with fakes.
 *
 * A switch takes its entry out of the map, so every later switch to the same
 * playlist reads the route again: a page is joined at most once and is never
 * served as a cache.
 */
export function createPlaylistPagePrefetch(
	fetchPage: FetchPlaylistPage = (playlistId, limit, offset) =>
		listPlaylistTracksPage(playlistId, { limit, offset }),
	now: () => number = () => performance.now()
) {
	const inflight = new Map<string, PrefetchEntry>();

	const isFresh = (entry: PrefetchEntry): boolean =>
		now() - entry.startedAt <= PLAYLIST_PREFETCH_MAX_AGE_MS;

	/** Drop entries no switch can join any more, so hovering a large tree
	 * does not keep every first page it ever read for the life of the tab. */
	function sweepStale(): void {
		for (const [playlistId, entry] of inflight) {
			if (!isFresh(entry)) inflight.delete(playlistId);
		}
	}

	/** Forget a playlist's prefetch because its membership was just written:
	 * the page it holds (and its etag) predates the write, so joining it
	 * would paint removed rows back and arm the next edit with a stale etag. */
	function invalidate(playlistId: string): void {
		inflight.delete(playlistId);
	}

	/** Forget every prefetch: a playlists/tracks change event or a bus resync
	 * says some membership or row moved, and not necessarily which. */
	function invalidateAll(): void {
		inflight.clear();
	}

	/** Idempotent while a fresh prefetch is pending or unconsumed. A rejected
	 * prefetch stays in the map so the switch that joins it reports that
	 * failure (fail-fast); the no-op catch only marks it handled until then. */
	function prefetchFirstPage(playlistId: string, pageSize = PLAYLIST_PREFETCH_PAGE_SIZE): void {
		const existing = inflight.get(playlistId);
		if (existing !== undefined && isFresh(existing)) return;
		sweepStale();
		const promise = fetchPage(playlistId, pageSize, 0);
		const entry: PrefetchEntry = { promise, pageSize, startedAt: now() };
		inflight.set(playlistId, entry);
		promise.catch(() => undefined);
	}

	/** Prefetch up to max playlist first pages (tree-visible intent). */
	function prefetchTreeIntent(
		playlistIds: readonly string[],
		max = PLAYLIST_TREE_PREFETCH_MAX
	): void {
		for (const playlistId of playlistIds.slice(0, max)) {
			prefetchFirstPage(playlistId);
		}
	}

	/** Page fetch for a switch: at offset 0, consume a fresh prefetch of the
	 * same size if one exists, resolving or rejecting exactly as it did, so a
	 * failed prefetch reaches the caller's load-error path and is never masked
	 * by a second GET; otherwise a live GET. */
	async function fetchFirstPage(
		playlistId: string,
		offset: number,
		pageSize = PLAYLIST_PREFETCH_PAGE_SIZE
	): Promise<PlaylistPageResult> {
		if (offset !== 0) {
			return fetchPage(playlistId, pageSize, offset);
		}
		const entry = inflight.get(playlistId);
		inflight.delete(playlistId);
		if (entry === undefined || entry.pageSize !== pageSize || !isFresh(entry)) {
			return fetchPage(playlistId, pageSize, 0);
		}
		return entry.promise;
	}

	/** Entries currently held (tests pin the stale sweep with it). */
	const size = (): number => inflight.size;

	return { prefetchFirstPage, prefetchTreeIntent, fetchFirstPage, invalidate, invalidateAll, size };
}

const shared = createPlaylistPagePrefetch();

export const prefetchPlaylistFirstPage = shared.prefetchFirstPage;
export const prefetchPlaylistTreeIntent = shared.prefetchTreeIntent;
export const fetchPlaylistFirstPage = shared.fetchFirstPage;
export const invalidatePlaylistFirstPage = shared.invalidate;
export const invalidateAllPlaylistFirstPages = shared.invalidateAll;
