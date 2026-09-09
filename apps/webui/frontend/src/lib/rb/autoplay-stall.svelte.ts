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
import {
	describeAutoPlayStall,
	type AutoPlayStall,
	type AutoPlayStallReason
} from '$lib/rb/autoplay-stall';
import type { AutoPlayTrackRow } from '$lib/rb/auto-play-chain';

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

/**
 * Drop the stall. Callers must have a reason AutoPlay can play again.
 *
 * The controller calls this on disarm and on uninstall: a switched-off AutoPlay
 * is not a stalled one, and telling the operator that a feature they turned off
 * stopped their music is its own mis-report.
 */
export function clearAutoPlayStall(): void {
	autoPlayStall.current = null;
}

/** Read side for non-reactive consumers (ui-mirror, tests). */
export function readAutoPlayStall(): AutoPlayStall | null {
	return autoPlayStall.current;
}

/**
 * Record the picker running out of candidates.
 *
 * The reason is derived HERE rather than at the call site so the controller
 * keeps one line per terminal branch, and so the mapping from
 * (all-missing, enforce-order) to a reason lives beside the words it selects.
 */
export function noteAutoPlayExhaustion(input: {
	source_stable_id: string;
	all_missing: boolean;
	enforce_order: boolean;
	remaining: readonly AutoPlayTrackRow[];
}): void {
	const reason: AutoPlayStallReason = input.all_missing
		? 'missing-audio'
		: input.enforce_order
			? 'no-next-in-order'
			: 'no-compatible-track';
	raiseAutoPlayStall(
		describeAutoPlayStall({
			reason,
			source_stable_id: input.source_stable_id,
			blocked: input.remaining
		})
	);
}

/**
 * Record a handoff that will not be retried.
 *
 * Both branches pin the controller's `_triggeredFor` to the source track for
 * the rest of its playout, so nothing else will ever start: same terminal
 * class as exhaustion, same durable state.
 */
export function noteAutoPlayHandoffStall(
	reason: 'handoff-attempts-exhausted' | 'handoff-incomplete',
	sourceStableId: string,
	detail: string
): void {
	raiseAutoPlayStall(
		describeAutoPlayStall({ reason, source_stable_id: sourceStableId, blocked: [], detail })
	);
}

/**
 * Retire the stall once sound is actually back.
 *
 * AUDIBLE, not playing (Codex r3973806301). `playing` is written optimistically
 * the moment a play is REQUESTED; `audible` is published from the presented
 * transport observation, i.e. from output that actually happened. Gating on
 * `playing` meant that starting another track while the output device was dead
 * deleted the explanation on the next poll with the room still silent, which is
 * the whole failure class this state exists for, reintroduced by its own clear
 * rule.
 *
 * A DIFFERENT track, because the stalled one merely ENDING is the moment the
 * room goes quiet, and that is exactly when the explanation has to stay up.
 */
export function retireAutoPlayStallIfAudible(sourceStableId: string, audible: boolean): void {
	const stall = autoPlayStall.current;
	if (stall === null) return;
	if (sourceStableId === stall.source_stable_id) return;
	if (!audible) return;
	clearAutoPlayStall();
}
