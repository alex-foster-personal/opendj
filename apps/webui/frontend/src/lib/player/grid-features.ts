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
import type { AnlzBeat, AnlzData } from '$lib/rb/anlz-types';
import type { DeckState } from '$lib/rb/deck-state-types';

/**
 * The one sentence every inert sync / quantize control shows on hover when the
 * track simply has no usable grid.
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

const _NAMED_SOURCES = new Set<string>(['own', 'rekordbox']);

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

function _beatgridSource(anlz: Pick<AnlzData, 'beatgrid'> | null | undefined): unknown {
	return anlz?.beatgrid?.source;
}

/** Fail-closed trust predicate for grid-dependent controls (NATIVE-01/03).
 *
 * Identifies the selected source ONLY from `anlz.beatgrid.source`, never from
 * the presence or absence of any other field. Any value other than the two
 * named literals is its own explicit untrusted branch. An own `status: ok` grid
 * with several tempo anchors is trusted when `static_grid_untrusted` is not
 * `true` (omitted on the multi-anchor `/anlz` wire, `false` on the record).
 * Do not treat `tempo_changes.length > 0` as no grid.
 */
export function hasTrustedBeatGrid(anlz: Pick<AnlzData, 'beatgrid'> | null | undefined): boolean {
	if (anlz === null || anlz === undefined) return false;
	const source = _beatgridSource(anlz);
	if (typeof source !== 'string' || !_NAMED_SOURCES.has(source)) {
		return false;
	}
	if (!hasRealBeatGrid(anlz.beatgrid.beats)) return false;
	if (source === 'rekordbox') return true;
	const bg = anlz.beatgrid;
	if (bg.status !== 'ok') return false;
	if (bg.static_grid_untrusted === true) return false;
	return true;
}

export function deckHasRealBeatGrid(st: Pick<DeckState, 'anlz'>): boolean {
	return hasRealBeatGrid(st.anlz?.beatgrid.beats);
}

export function deckHasTrustedBeatGrid(st: Pick<DeckState, 'anlz'>): boolean {
	return hasTrustedBeatGrid(st.anlz);
}

/** Whether beat ticks and tempo-change markers should paint on the deck.
 *
 * Own `status: failed` paints NO beats (never rekordbox's in its place).
 * Own `static_grid_untrusted: true` still paints ticks and tempo markers.
 */
export function shouldPaintBeatGrid(anlz: Pick<AnlzData, 'beatgrid'> | null | undefined): boolean {
	if (anlz === null || anlz === undefined) return false;
	const source = _beatgridSource(anlz);
	if (source === 'own' && anlz.beatgrid.status === 'failed') return false;
	return hasRealBeatGrid(anlz.beatgrid.beats);
}

/** Hover title for inert quantize / beat-sync controls on a loaded deck. */
export function gridFeatureInertTip(
	st: Pick<DeckState, 'anlz' | 'stable_id'>
): string {
	if (st.stable_id === null) return GRID_FEATURE_TIP;
	const anlz = st.anlz;
	if (anlz === null || anlz === undefined) return GRID_FEATURE_TIP;
	const source = _beatgridSource(anlz);
	if (source === 'own' && anlz.beatgrid.status === 'failed') {
		const reason = anlz.beatgrid.reason;
		if (typeof reason === 'string' && reason.length > 0) return reason;
		return 'own beatgrid analysis failed';
	}
	if (source === 'own' && anlz.beatgrid.static_grid_untrusted === true) {
		const first = anlz.tempo_changes?.[0];
		if (first !== undefined) {
			return `static grid untrusted - tempo change at ${first.at_s.toFixed(3)}s`;
		}
		return 'static grid untrusted - tempo change detected';
	}
	return GRID_FEATURE_TIP;
}

/** Quantize as the transport actually applies it: what the DJ asked for, AND
 * a trusted grid to snap to. */
export function effectiveQuantize(st: Pick<DeckState, 'anlz' | 'quantize_enabled'>): boolean {
	return st.quantize_enabled && deckHasTrustedBeatGrid(st);
}

/** Beat Sync as the transport actually applies it: what the DJ asked for, AND
 * a trusted grid to phase-lock with. BAR planning, not this gate, filters
 * `beatIsExtrapolated` anchors at plan time. */
export function effectiveBeatSync(st: Pick<DeckState, 'anlz' | 'beat_sync_enabled'>): boolean {
	return st.beat_sync_enabled && deckHasTrustedBeatGrid(st);
}

/**
 * PLAY-25: may this follower phase-lock to that master right now?
 *
 * Both decks need a trusted grid: the follower's own (`effectiveBeatSync`) AND
 * the master's. Tue 6 Oct 2026, silver preview 17:35Z: with Beat Sync Max on,
 * AutoPlay loaded the next track on deck 1 while the playing master (deck 2)
 * had a 0-beat grid. `play` joined deck 1 to deck 2's phase, `requireBeatGrid`
 * threw on the master, the handoff aborted after the load, and the set went
 * silent when deck 2 ran out. A gridless master means the follower starts
 * unsynced, never that it does not start.
 */
export function canPhaseLockTo(
	follower: Pick<DeckState, 'anlz' | 'beat_sync_enabled'>,
	master: Pick<DeckState, 'anlz'>
): boolean {
	return effectiveBeatSync(follower) && deckHasTrustedBeatGrid(master);
}

/**
 * Whether a LOADED track's missing or untrusted grid is what makes this deck's
 * sync and quantize controls inert.
 *
 * An empty deck is deliberately excluded: nothing has been loaded to analyse,
 * so blaming the grid would send a new user looking for a track that is not
 * there. Empty decks keep their existing controls and copy.
 */
export function gridFeaturesInert(st: Pick<DeckState, 'anlz' | 'stable_id'>): boolean {
	return st.stable_id !== null && !deckHasTrustedBeatGrid(st);
}
