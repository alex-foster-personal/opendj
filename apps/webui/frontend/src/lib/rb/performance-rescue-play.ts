/**
 * Pure planner for simultaneous rescue play starts.
 */

import type { DeckId } from '$lib/rb/deck-id';
import { commonSyncScheduleTimes } from '$lib/player/transport/schedule-math';

export interface RescuePlayTarget {
	deck: DeckId;
	beat_phase_ms: number | null;
}

export interface RescuePlaySchedule {
	deck: DeckId;
	startAtContextTime: number;
}

/** Schedule every playing deck at one shared AudioContext time. */
export function planRescueSimultaneousPlay(
	targets: RescuePlayTarget[],
	contextTime: number,
	leadSeconds = 0.05
): RescuePlaySchedule[] {
	if (targets.length === 0) return [];
	if (!Number.isFinite(contextTime) || contextTime < 0) {
		throw new RangeError(`contextTime must be finite and non-negative, got ${contextTime}`);
	}
	if (!Number.isFinite(leadSeconds) || leadSeconds < 0) {
		throw new RangeError(`leadSeconds must be finite and non-negative, got ${leadSeconds}`);
	}
	for (const target of targets) {
		if (
			target.beat_phase_ms !== null &&
			(!Number.isFinite(target.beat_phase_ms) || target.beat_phase_ms < 0)
		) {
			throw new RangeError(
				`beat_phase_ms for deck ${target.deck} must be null or non-negative, got ${target.beat_phase_ms}`
			);
		}
	}
	const syncAt = contextTime + leadSeconds;
	const scheduleTimes = commonSyncScheduleTimes(syncAt, targets.length);
	return targets.map((target, index) => ({
		deck: target.deck,
		startAtContextTime: scheduleTimes[index]!
	}));
}
