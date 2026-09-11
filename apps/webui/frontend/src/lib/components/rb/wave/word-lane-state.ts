/**
 * Pure word-lane selection: WHICH overlay one wave row should show, given
 * the lyric cache entry for its loaded track and the two lyric prefs.
 *
 * Split out of word-lane-state.svelte.ts so the decision is testable under
 * plain node (tests/unit/word-lane-state.test.mjs) while the rune half stays
 * pure lifecycle. Generic over the word shape and importing nothing, so this
 * module carries no dependency on $lib/api or the lyric cache - the same
 * dependency-free contract word-lanes.ts keeps.
 */

/** Mirrors $lib/lyrics/lyrics-cache's LyricEntryState. */
export type WordLaneEntryState = 'loading' | 'loaded' | 'none' | 'error';

/** The subset of that cache's LyricEntry this decision actually reads. */
export interface WordLaneEntry<TWord> {
	state: WordLaneEntryState;
	track: { words: TWord[] } | null;
	error: string | null;
}

/** The subset of uiPrefs that gates the word lane. */
export interface WordLanePrefs {
	lyrics_global: boolean;
	lyrics_waveform_overlay: boolean;
}

export interface WordLaneSelection<TWord> {
	/** Both pref gates open: this row may paint words once it has them. */
	on: boolean;
	/** Loaded words, or null whenever there is nothing honest to paint. */
	words: TWord[] | null;
	/** A REAL verdict - the pipeline has no words for this track (404 ->
	 * 'none', or a loaded payload with zero words). Never "not fetched yet". */
	unavailable: boolean;
	/** Fetch failure message, surfaced rather than swallowed. */
	error: string | null;
}

/**
 * Project one cache entry plus the prefs into the render decision.
 *
 * `on` depends ONLY on the prefs, so a row whose track has no words still
 * reports the overlay as enabled (the caller falls back to the line lane);
 * `words` is non-null only for a loaded payload that actually has words.
 */
export function selectWordLane<TWord>(
	entry: WordLaneEntry<TWord> | null,
	prefs: WordLanePrefs
): WordLaneSelection<TWord> {
	const on = prefs.lyrics_global && prefs.lyrics_waveform_overlay;
	if (entry === null) {
		return { on, words: null, unavailable: false, error: null };
	} else if (entry.state === 'error') {
		if (entry.error === null) {
			throw new Error("selectWordLane: cache entry state 'error' carries no message");
		}
		return { on, words: null, unavailable: false, error: entry.error };
	} else if (entry.state === 'none') {
		return { on, words: null, unavailable: true, error: null };
	} else if (entry.state === 'loading') {
		return { on, words: null, unavailable: false, error: null };
	}
	// 'loaded'. A payload with zero words is the same honest verdict as a 404.
	const words = entry.track === null ? [] : entry.track.words;
	return {
		on,
		words: words.length > 0 ? words : null,
		unavailable: words.length === 0,
		error: null
	};
}
