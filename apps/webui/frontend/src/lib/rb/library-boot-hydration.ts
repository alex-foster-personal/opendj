/**
 * Early library hydration at SPA boot (PERF-UI-03, issue #2697).
 *
 * Fires ui-prefs and the first All Tracks page as soon as the layout module
 * loads, before BrandLaunch's first frame. BrowserPanel joins the same
 * in-flight promises instead of re-fetching.
 *
 * LIBM-138: the first page is small, so the first rows do not wait on 500 of
 * them, and the walk tells the boot scheduler when it starts and ends so
 * deferred boot requests stay off the engine until the listing is in.
 */

import {
	listPlaylistsHydrated,
	listTracksHydrated,
	type PlaylistSummaryHydrated,
	type TracksPageHydrated
} from '$lib/rb/api-rb';
import { bootScheduler } from '$lib/rb/boot-scheduler';
import { hydrateConfirmPrefsFromDisk } from '$lib/rb/prefs.svelte';

/** Per-request page size for BrowserPanel's listing pages after the first. */
export const LIBRARY_BOOT_PAGE_SIZE = 500;

/** Rows in the first All Tracks page: more than a tall viewport shows, and a
 * fifth of the engine work of a full page (measured Thu 1 Oct 2026: 0.15 s
 * for 100 rows against 1.1 to 1.6 s for 500 on the same engine). */
export const LIBRARY_BOOT_FIRST_PAGE_SIZE = 100;

export interface LibraryBootPrefetch {
	readonly startedAt: number;
	readonly tracksPromise: Promise<TracksPageHydrated>;
	readonly prefsPromise: Promise<void>;
	readonly playlistsPromise: Promise<PlaylistSummaryHydrated[]>;
}

/** @deprecated Use LibraryBootPrefetch */
export type BootTracksPrefetch = LibraryBootPrefetch;

let bootPrefetch: LibraryBootPrefetch | null = null;

type FetchBootTracksPage = (limit: number, cursor?: string) => Promise<TracksPageHydrated>;
type FetchBootPlaylists = () => Promise<PlaylistSummaryHydrated[]>;
type PrefsHydrator = () => Promise<void>;

let fetchBootTracksPage: FetchBootTracksPage = (limit, cursor) =>
	listTracksHydrated({ limit, cursor });
let settleBootListingWalk: (() => void) | null = null;
let fetchBootPlaylists: FetchBootPlaylists = () => listPlaylistsHydrated({ fast: true });
let fallbackTracksFetch: FetchBootTracksPage = (limit) => listTracksHydrated({ limit });
let fallbackPlaylistsFetch: FetchBootPlaylists = () => listPlaylistsHydrated({ fast: true });
let prefsHydrator: PrefsHydrator = () => hydrateConfirmPrefsFromDisk();

/** Test seam: inject a fake tracks fetch without mocking production api-rb. */
export function setFetchBootTracksPageForTests(fn: FetchBootTracksPage | null): void {
	fetchBootTracksPage = fn ?? ((limit, cursor) => listTracksHydrated({ limit, cursor }));
}

/** Test seam: inject a fake playlists fetch without mocking production api-rb. */
export function setFetchBootPlaylistsForTests(fn: FetchBootPlaylists | null): void {
	fetchBootPlaylists = fn ?? (() => listPlaylistsHydrated({ fast: true }));
}

/** Test seam: inject a fake ui-prefs hydrator. */
export function setPrefsHydratorForTests(fn: PrefsHydrator | null): void {
	prefsHydrator = fn ?? (() => hydrateConfirmPrefsFromDisk());
}

/** Test seam: inject the fallback tracks fetch used after prefetch rejection. */
export function setFallbackTracksFetchForTests(fn: FetchBootTracksPage | null): void {
	fallbackTracksFetch = fn ?? ((limit) => listTracksHydrated({ limit }));
}

/** Test seam: inject the fallback playlists fetch used after prefetch rejection. */
export function setFallbackPlaylistsFetchForTests(fn: FetchBootPlaylists | null): void {
	fallbackPlaylistsFetch = fn ?? (() => listPlaylistsHydrated({ fast: true }));
}

/** Test-only reset for the boot singleton and injected seams. */
export function resetLibraryBootHydrationForTests(): void {
	bootPrefetch = null;
	bootListingWalkSettled();
	setFetchBootTracksPageForTests(null);
	setFetchBootPlaylistsForTests(null);
	setPrefsHydratorForTests(null);
	setFallbackTracksFetchForTests(null);
	setFallbackPlaylistsFetchForTests(null);
}

function bootNowMs(): number {
	if (typeof performance !== 'undefined' && typeof performance.timeOrigin === 'number') {
		return performance.timeOrigin;
	}
	return Date.now();
}

/** Whether a boot on `pathname` mounts the pane that walks All Tracks. Only
 * that pane ends the walk, so only there may the walk hold deferred boot work:
 * anywhere else the hold would sit until the scheduler's ceiling. */
export function routeRunsBootListingWalk(pathname: string): boolean {
	return pathname.replace(/\/+$/, '') === '/performance';
}

/** Idempotent: second and later calls are no-ops. `listingWalkRuns` is false
 * on a route with no All Tracks pane: the prefetch still fires, nothing holds. */
export function startLibraryBootHydration(listingWalkRuns = true): void {
	if (bootPrefetch !== null) return;
	const startedAt = bootNowMs();
	if (listingWalkRuns) settleBootListingWalk = bootScheduler.listingWalkStarted();
	bootPrefetch = {
		startedAt,
		prefsPromise: prefsHydrator(),
		tracksPromise: fetchBootTracksPage(LIBRARY_BOOT_FIRST_PAGE_SIZE),
		playlistsPromise: fetchBootPlaylists()
	};
}

/** Read-only boot prefetch state; throws if hydration was never started. */
export function bootTracksPrefetch(): LibraryBootPrefetch {
	if (bootPrefetch === null) {
		throw new Error('startLibraryBootHydration() was not called before bootTracksPrefetch()');
	}
	return bootPrefetch;
}

/** Join the boot playlists prefetch or fall back to a live fast GET. */
export async function bootPlaylistsPrefetch(): Promise<PlaylistSummaryHydrated[]> {
	try {
		return await bootTracksPrefetch().playlistsPromise;
	} catch {
		return fallbackPlaylistsFetch();
	}
}

/** Whether the boot pane can open All Tracks without the playlist tree yet. */
export function canBootAllTracksEarly(args: {
	remembered: { kind: 'all_tracks' | 'playlist' } | null;
	url_playlist_id?: string | null;
	source: 'collection' | 'spotify';
	spotify_selected_id: string | null;
}): boolean {
	if (args.source === 'spotify' && args.spotify_selected_id !== null) return false;
	const urlPlaylistId = args.url_playlist_id ?? null;
	if (
		urlPlaylistId !== null &&
		urlPlaylistId.length > 0 &&
		urlPlaylistId !== 'all'
	) {
		return false;
	}
	const remembered = args.remembered;
	if (remembered !== null && remembered.kind === 'playlist') return false;
	return true;
}

/** Whether the boot All Tracks walk is still holding deferred boot work. */
export function bootListingWalkInFlight(): boolean {
	return settleBootListingWalk !== null;
}

/** The boot All Tracks walk is over: its last page arrived, a page failed, or
 * the boot opened another pane. Lets deferred boot work through. Idempotent. */
export function bootListingWalkSettled(): void {
	settleBootListingWalk?.();
	settleBootListingWalk = null;
}

async function _bootTracksPage(cursor: string | undefined): Promise<TracksPageHydrated> {
	if (cursor !== undefined) return fetchBootTracksPage(LIBRARY_BOOT_PAGE_SIZE, cursor);
	try {
		return await bootTracksPrefetch().tracksPromise;
	} catch {
		return fallbackTracksFetch(LIBRARY_BOOT_FIRST_PAGE_SIZE);
	}
}

/** One All Tracks page: the first joins the boot prefetch (or falls back to a
 * live GET), the rest are fetched by cursor. Ends the boot walk on the last
 * page or on a failure, which is still thrown to the caller. */
export async function fetchBootTracksFirstPage(
	cursor: string | undefined
): Promise<TracksPageHydrated> {
	let page: TracksPageHydrated;
	try {
		page = await _bootTracksPage(cursor);
	} catch (error) {
		bootListingWalkSettled();
		throw error;
	}
	if (page.next_cursor === null) bootListingWalkSettled();
	return page;
}
