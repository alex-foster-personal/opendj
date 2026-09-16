/**
 * Tempo match for the library preview voice (CUEOUT-15 R6).
 *
 * the maintainer, Wed 16 Sep 2026: "Should we, even now v1, have previews beat sync by
 * default just for the sanity of the listener? Can be part of the BeatSyncMax
 * toggle." This is the tempo half of that. The preview is not a deck: it has no
 * stretch worklet, so the only tempo control it has is the source node's
 * `playbackRate`, which moves pitch with tempo exactly as a turntable does.
 * Inside a pitch range that is audible as "the same record, nudged", which is
 * what a DJ expects from a cue-bus preview; a wider stretch would need the deck
 * engine and belongs to CUEOUT-16 with phase alignment.
 *
 * Mini-PRD:
 * - ✔︎ R6.1 With the mode off, the preview plays at its own tempo, always.
 *   - [if] mode is 'off' [then ⛔️] rate is 1 whatever the tempos are
 * - ✔︎ R6.2 A preview started while a master deck plays matches that tempo when
 *   the match fits the pitch range.
 *   - [if] master 128 and track 124 [then ⛔️] rate is 128/124
 * - ✔︎ R6.3 A match that does not fit the range is not forced.
 *   - [if] master 128 and track 100 [then ⛔️] rate is 1, not 1.28
 * - ✔︎ R6.4 Half and double time count as a match, because a 174 drum and bass
 *   master against an 87 track is the same pulse.
 *   - [if] master 174 and track 87 [then ⛔️] rate is 1 (87 doubled is 174)
 *   - [if] master 70 and track 140 [then ⛔️] rate is 1 (140 halved is 70)
 * - ✔︎ R6.5 Missing or nonsense tempo data never produces a silent wrong answer.
 *   - [if] either BPM is null, zero, negative or not finite [then ⛔️] rate is 1
 *
 * Pure. No audio nodes, no stores, no DOM.
 */

import { tempoBoundsFromPitchRange } from '$lib/rb/auto-play';

/** Preview tempo behaviour. Persisted as a pref, read per preview start. */
export type PreviewBeatSync = 'off' | 'tempo';

/**
 * Pitch range the match must fit inside, in percent.
 *
 * 16 is the deck default the rest of the app reasons with (`auto-play.ts`'s
 * walkthrough and DeckHeader both use it), so a preview matches exactly the
 * tempos AutoPlay would already have called compatible.
 */
export const PREVIEW_TEMPO_RANGE_PCT = 16;

export interface PreviewSyncInput {
	mode: PreviewBeatSync;
	/** Effective BPM of the playing master deck, or null when none is playing. */
	masterBpm: number | null;
	/** The previewed track's own BPM, or null when it has not been analyzed. */
	trackBpm: number | null;
}

export interface PreviewSyncRate {
	/** `playbackRate` for the preview source. 1 means "leave it alone". */
	rate: number;
	/** True only when the rate came from a tempo match. */
	matched: boolean;
}

const _UNMATCHED: PreviewSyncRate = { rate: 1, matched: false };

function _usableBpm(bpm: number | null): number | null {
	if (bpm === null || !Number.isFinite(bpm) || bpm <= 0) return null;
	return bpm;
}

/**
 * Playback rate for a preview about to start.
 *
 * Half and double time are considered because they are the same pulse: an 87
 * BPM track under a 174 BPM master is already in time, and speeding it up 2x to
 * "match" would be the wrong answer musically as well as outside the range.
 * The candidate closest to 1 wins, so an in-range straight match is never
 * passed over for a folded one.
 */
export function previewSyncRate({ mode, masterBpm, trackBpm }: PreviewSyncInput): PreviewSyncRate {
	if (mode === 'off') return _UNMATCHED;
	const master = _usableBpm(masterBpm);
	const track = _usableBpm(trackBpm);
	if (master === null || track === null) return _UNMATCHED;

	const { min, max } = tempoBoundsFromPitchRange(PREVIEW_TEMPO_RANGE_PCT);
	let best: number | null = null;
	for (const candidate of [master / track, master / (track * 2), (master * 2) / track]) {
		if (candidate < min || candidate > max) continue;
		if (best === null || Math.abs(candidate - 1) < Math.abs(best - 1)) best = candidate;
	}
	if (best === null) return _UNMATCHED;
	return { rate: best, matched: true };
}
