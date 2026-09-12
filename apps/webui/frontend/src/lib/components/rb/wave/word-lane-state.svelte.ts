/**
 * Reactive word-lane lifecycle for one wave row: read the two lyric prefs,
 * kick the on-demand lyric load for the row's loaded track, and project the
 * cache entry into the selection LyricLanes.svelte switches on.
 *
 * Modelled on lyrics-fetch.svelte.ts (the line lane's fetch half): the
 * components stay pure display projections and the subscription bookkeeping
 * lives here rather than in an already-large WaveRow.svelte. Dependencies
 * are injected so a test can drive this without the real cache or the prefs
 * singleton, and the pure decision lives in word-lane-state.ts.
 * .svelte.ts extension is REQUIRED for the $derived / $effect runes.
 */
import type { KaraokeWord } from '$lib/api-karaoke';
import { lyricsCache, type LyricEntry } from '$lib/lyrics/lyrics-cache.svelte';
import { uiPrefs } from '$lib/rb/prefs.svelte';
import { selectWordLane, type WordLanePrefs, type WordLaneSelection } from './word-lane-state';

export interface WordLaneDeps {
	uiPrefs: WordLanePrefs;
	lyricEntry: (stableId: string) => LyricEntry | null;
	loadLyrics: (stableId: string) => Promise<void>;
}

export type WordLaneState = Readonly<WordLaneSelection<KaraokeWord>>;

export function createWordLaneState(
	stableId: () => string | null,
	deps: WordLaneDeps = {
		uiPrefs,
		lyricEntry: lyricsCache.entry,
		loadLyrics: lyricsCache.load
	}
): WordLaneState {
	const selection = $derived.by((): WordLaneSelection<KaraokeWord> => {
		const sid = stableId();
		const entry = sid === null ? null : deps.lyricEntry(sid);
		return selectWordLane(entry, deps.uiPrefs);
	});
	// Fetch on track load while the overlay is on. The cache dedupes: 'none'
	// and 'loaded' entries never refetch and in-flight loads coalesce, so the
	// pref is a real cost gate - off means the words are never fetched.
	$effect(() => {
		const sid = stableId();
		if (sid !== null && selection.on) void deps.loadLyrics(sid);
	});
	return {
		get on() {
			return selection.on;
		},
		get words() {
			return selection.words;
		},
		get unavailable() {
			return selection.unavailable;
		},
		get error() {
			return selection.error;
		}
	};
}
