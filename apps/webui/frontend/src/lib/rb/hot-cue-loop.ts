/**
 * HOTCUE-LOOP-01: which loop a hot cue saved right now would store.
 *
 * rekordbox/CDJ behaviour, mimicked: a hot cue set while a loop is ACTIVE is
 * a loop hot cue, the loop's own start and end, not the playhead. A stored
 * (display-only) loop that is not engaged does not count. Pure, so the pad,
 * the controller pad and the deck actions share one rule.
 */
import type { LoopState } from './deck-state-types';

/** The engaged loop as integer cue ms (the cue store's contract), or null. */
export function engagedLoopForHotCue(deck: { loop: LoopState | null }): { in_ms: number; out_ms: number } | null {
	const loop = deck.loop;
	if (loop === null || !loop.engaged) return null;
	const in_ms = Math.round(loop.in_ms);
	const out_ms = Math.round(loop.out_ms);
	if (!Number.isFinite(in_ms) || !Number.isFinite(out_ms) || in_ms < 0 || out_ms <= in_ms) return null;
	return { in_ms, out_ms };
}
