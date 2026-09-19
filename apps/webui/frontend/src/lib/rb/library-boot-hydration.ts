/**
 * Early library hydration at SPA boot (PERF-UI-03, issue #2697).
 *
 * Fires ui-prefs and the first All Tracks page as soon as the layout module
 * loads, before BrandLaunch's first frame. BrowserPanel joins the same
 * in-flight promises instead of re-fetching.
 */

import {
	listPlaylistsHydrated,
	listTracksHydrated,
	type PlaylistSummaryHydrated,
	type TracksPageHydrated
} from '$lib/rb/api-rb';
import { hydrateConfirmPrefsFromDisk } from '$lib/rb/prefs.svelte';

/** Per-request page size for the boot tracks prefetch and BrowserPanel. */
export const LIBRARY_BOOT_PAGE_SIZE = 500;

export interface LibraryBootPrefetch {
	readonly startedAt: number;
	readonly tracksPromise: Promise<TracksPageHydrated>;
	readonly prefsPromise: Promise<void>;
	readonly playlistsPromise: Promise<PlaylistSummaryHydrated[]>;
}

/** @deprecated Use LibraryBootPrefetch */
export type BootTracksPrefetch = LibraryBootPrefetch;

let bootPrefetch: LibraryBootPrefetch | null = null;

type FetchBootTracksPage = (limit: number) => Promise<TracksPageHydrated>;
type FetchBootPlaylists = () => Promise<PlaylistSummaryHydrated[]>;
type PrefsHydrator = () => Promise<void>;

let fetchBootTracksPage: FetchBootTracksPage = (limit) => listTracksHydrated({ limit });
let fetchBootPlaylists: FetchBootPlaylists = () => listPlaylistsHydrated({ fast: true });
let fallbackTracksFetch: FetchBootTracksPage = (limit) => listTracksHydrated({ limit });
let fallbackPlaylistsFetch: FetchBootPlaylists = () => listPlaylistsHydrated({ fast: true });
let prefsHydrator: PrefsHydrator = () => hydrateConfirmPrefsFromDisk();

/** Test seam: inject a fake tracks fetch without mocking production api-rb. */
export function setFetchBootTracksPageForTests(fn: FetchBootTracksPage | null): void {
	fetchBootTracksPage = fn ?? ((limit) => listTracksHydrated({ limit }));
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

/** Idempotent: second and later calls are no-ops. */
export function startLibraryBootHydration(): void {
	if (bootPrefetch !== null) return;
	const startedAt = bootNowMs();
	bootPrefetch = {
		startedAt,
		prefsPromise: prefsHydrator(),
		tracksPromise: fetchBootTracksPage(LIBRARY_BOOT_PAGE_SIZE),
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

/** First-page tracks fetch: join boot prefetch or fall back to a live GET. */
export async function fetchBootTracksFirstPage(
	cursor: string | undefined
): Promise<TracksPageHydrated> {
	if (cursor !== undefined) {
		return listTracksHydrated({ limit: LIBRARY_BOOT_PAGE_SIZE, cursor });
	}
	try {
		return await bootTracksPrefetch().tracksPromise;
	} catch {
		return fallbackTracksFetch(LIBRARY_BOOT_PAGE_SIZE);
	}
}
