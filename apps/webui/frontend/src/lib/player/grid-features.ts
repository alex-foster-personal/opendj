/**
 * Which deck features a loaded track's PQTZ grid can actually support.
 *
 * Requirements:
 *   ✔︎ ✅ 🎯 Never gate transport on a grid-dependent feature.
 *     [if] play, pause or cue is asked of a gridless deck [then] it runs unquantized
 *   ✔︎ ✅ 🎯 Fold the grid check into the quantize / beat-sync flags, not into the flags' values.
 *     [if] a track with no real grid is loaded [then] both flags keep their value and stop taking effect
 *   ✔︎ ✅ 🎯 Give every inert sync / quantize control one shared sentence.
 *     [if] two controls explain the same inert state [then] they must read identically
 *
 * quantize_enabled and beat_sync_enabled both DEFAULT to true, which is
 * rekordbox parity for an analysed library. Those defaults used to reach
 * _requireBeatGrid from inside play() and pause(), so a first-run user who
 * imported an unanalysed file could neither start a deck nor stop one that was
 * already playing. The rule that replaces it: a flag says what the DJ wants,
 * the grid says whether it can happen, and only the SECOND of those may ever
 * stop transport - which it never does.
 */

import { validateBeatGrid } from '$lib/rb/beat-sync-math';
import type { AnlzBeat, DeckState } from '$lib/rb/types';

/**
 * The one sentence every inert sync / quantize control shows on hover.
 *
 * Deliberately NOT the 'not implemented - see PARITY-TODO' stub wording, which
 * means "never built" and is policed as a literal by inert-controls.test.mjs.
 * Quantize and Beat Sync are built and work; this track just has nothing for
 * them to lock to, which is a different fact and earns its own sentence. It
 * also names what still works, because the failure this replaces was a user
 * concluding the deck itself was broken.
 */
export const GRID_FEATURE_TIP =
	'needs a beat grid - analyse this track for quantize and beat sync; play, pause and cue still work';

/**
 * Whether these beats are a real PQTZ grid the beat math can plan against.
 *
 * A predicate around the validator rather than a second copy of its rules, so
 * "usable grid" cannot mean one thing here and another inside the math. The
 * catch is the conversion from validator-throws to predicate-returns and
 * nothing else: no caller loses an error it would otherwise have seen, because
 * every caller of this function is deciding, not executing.
 */
export function hasRealBeatGrid(
	beats: readonly AnlzBeat[] | null | undefined
): beats is readonly AnlzBeat[] {
	if (beats === null || beats === undefined) return false;
	try {
		validateBeatGrid(beats);
	} catch {
		return false;
	}
	return true;
}

export function deckHasRealBeatGrid(st: Pick<DeckState, 'anlz'>): boolean {
	return hasRealBeatGrid(st.anlz?.beatgrid.beats);
}

/** Quantize as the transport actually applies it: what the DJ asked for, AND
 * a grid to snap to. */
export function effectiveQuantize(st: Pick<DeckState, 'anlz' | 'quantize_enabled'>): boolean {
	return st.quantize_enabled && deckHasRealBeatGrid(st);
}

/** Beat Sync as the transport actually applies it: what the DJ asked for, AND
 * a grid to phase-lock with. */
export function effectiveBeatSync(st: Pick<DeckState, 'anlz' | 'beat_sync_enabled'>): boolean {
	return st.beat_sync_enabled && deckHasRealBeatGrid(st);
}

/**
 * Whether a LOADED track's missing grid is what makes this deck's sync and
 * quantize controls inert.
 *
 * An empty deck is deliberately excluded: nothing has been loaded to analyse,
 * so blaming the grid would send a new user looking for a track that is not
 * there. Empty decks keep their existing controls and copy.
 */
export function gridFeaturesInert(st: Pick<DeckState, 'anlz' | 'stable_id'>): boolean {
	return st.stable_id !== null && !deckHasRealBeatGrid(st);
}
