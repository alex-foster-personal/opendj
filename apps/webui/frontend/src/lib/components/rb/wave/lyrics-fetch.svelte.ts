/** Reactive one-fetch-per-track lyrics state for a wave row's own loaded
 * deck. LyricsLane is a pure display projection with no fetch of its own;
 * this hook is the fetch/lifecycle half, kept out of WaveRow.svelte itself
 * so its cancellation bookkeeping doesn't grow an already-large file.
 * Takes the fetcher as a parameter rather than importing `$lib/rb/api-rb`
 * directly, so this module stays off that file's import graph.
 * .svelte.ts extension is REQUIRED for the $state rune (RECON-FRONTEND 10.1).
 */
import { optionalResources } from '$lib/rb/optional-resource-availability';
import type { LyricLine } from './lyrics-lane';
import { shouldFetchTrackLyrics } from './lyrics-cached-ids';
import { ensureLyricsCachedIdsLoaded, getLyricsCachedIds } from './lyrics-cached-ids.svelte';

type LyricsPayload = { lines: LyricLine[] };

export function createLyricsFetchState(
	stableId: () => string | null,
	fetchLyrics: (stableId: string) => Promise<LyricsPayload | null>,
	cachedLyricsIds: () => ReadonlySet<string> | null = getLyricsCachedIds
): { readonly lyrics: LyricsPayload | null; readonly loadError: Error | null } {
	let lyrics = $state<LyricsPayload | null>(null);
	let loadError = $state<Error | null>(null);
	$effect(() => {
		ensureLyricsCachedIdsLoaded();
		const sid = stableId();
		const cachedIds = cachedLyricsIds();
		let cancelled = false;
		lyrics = null;
		loadError = null;
		if (sid === null) return;
		if (optionalResources(sid).lyrics === false) return;
		if (!shouldFetchTrackLyrics(sid, cachedIds)) return;
		void (async () => {
			try {
				const result = await fetchLyrics(sid);
				if (!cancelled) lyrics = result;
			} catch (error: unknown) {
				if (!cancelled) loadError = error instanceof Error ? error : new Error(String(error));
			}
		})();
		return () => {
			cancelled = true;
		};
	});
	return {
		get lyrics() {
			return lyrics;
		},
		get loadError() {
			return loadError;
		}
	};
}
