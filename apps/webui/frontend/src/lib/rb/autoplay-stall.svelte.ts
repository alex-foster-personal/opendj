/**
 * PLAY-08 reactive holder for the AutoPlay stall descriptor.
 *
 * Separate from the pure module for the same reason autoplay-queue.svelte.ts is
 * separate from auto-play.ts: this file owns a lifetime, not a decision.
 *
 * The lifetime is the whole point of the requirement. A stall must OUTLIVE the
 * event that caused it, including the source deck reaching the end of its track
 * and going idle - that transition is exactly when the room goes quiet, so
 * clearing on it would delete the explanation at the moment it is needed. It is
 * cleared only by something that genuinely un-stalls AutoPlay: a committed
 * handoff, a new candidate feed, or AutoPlay being disarmed.
 */
import type { AutoPlayStall } from '$lib/rb/autoplay-stall';

export const autoPlayStall = $state<{ current: AutoPlayStall | null }>({ current: null });

/**
 * Record a terminal AutoPlay stop.
 *
 * The LATEST stall wins: a second terminal branch after a feed change is newer
 * information, and silently keeping the first would describe a state that is no
 * longer the one the operator is in.
 */
export function raiseAutoPlayStall(stall: AutoPlayStall): void {
	autoPlayStall.current = stall;
}

/** Drop the stall. Callers must have a reason AutoPlay can play again. */
export function clearAutoPlayStall(): void {
	autoPlayStall.current = null;
}

/** Read side for non-reactive consumers (ui-mirror, tests). */
export function readAutoPlayStall(): AutoPlayStall | null {
	return autoPlayStall.current;
}
