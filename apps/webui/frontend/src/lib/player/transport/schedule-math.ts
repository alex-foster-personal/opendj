/**
 * Pure schedule-time safety and position projection. No runtime, no stores, no
 * audio nodes: every function here is a total function of its arguments.
 * Extracted verbatim from audio-engine.svelte.ts (T4 S2).
 *
 * These are the guards that keep a mutation from being scheduled in the past,
 * and the arithmetic that answers "where is the playhead at context time t".
 * They are the cheap half of the split precisely because they can be checked
 * without an AudioContext.
 *
 * NOT YET HERE (see the T4 report): `projectedLoopAwareTransportPosition` and
 * `naturalEndNeedsRevisionedStop`, both R23. The first calls
 * `_positionForSegment`, which lives in transport/clock and does not move until
 * S6; the second needs the `PresentedTransportObservation` type, which does not
 * move until S4. They join this module then.
 */

import { SYNC_SCHEDULE_SAFETY_S } from '$lib/player/constants';
import type { LoopState } from '$lib/rb/types';

export function supersedingScheduleTime(
	requestedContextTime: number,
	pendingContextTime: number | null,
	minimumContextTime = 0
): number {
	if (!Number.isFinite(requestedContextTime) || requestedContextTime < 0) {
		throw new RangeError(
			`requestedContextTime must be finite and non-negative, got ${requestedContextTime}`
		);
	}
	if (!Number.isFinite(minimumContextTime) || minimumContextTime < 0) {
		throw new RangeError(
			`minimumContextTime must be finite and non-negative, got ${minimumContextTime}`
		);
	}
	if (requestedContextTime < minimumContextTime) {
		throw new RangeError(
			`requestedContextTime ${requestedContextTime} precedes minimumContextTime ` +
				`${minimumContextTime}`
		);
	}
	if (pendingContextTime === null) return requestedContextTime;
	if (!Number.isFinite(pendingContextTime) || pendingContextTime < 0) {
		throw new RangeError(
			`pendingContextTime must be finite and non-negative, got ${pendingContextTime}`
		);
	}
	if (pendingContextTime < minimumContextTime) return requestedContextTime;
	return Math.min(requestedContextTime, pendingContextTime);
}

export function commonSyncScheduleTimes(
	syncAtContextTime: number,
	participantCount: number
): number[] {
	if (!Number.isFinite(syncAtContextTime) || syncAtContextTime < 0) {
		throw new RangeError(
			`syncAtContextTime must be finite and non-negative, got ${syncAtContextTime}`
		);
	}
	if (!Number.isInteger(participantCount) || participantCount <= 0) {
		throw new RangeError(`participant count must be a positive integer, got ${participantCount}`);
	}
	return Array.from({ length: participantCount }, () => syncAtContextTime);
}

export function pendingSyncWaitTarget(
	requestedSafeContextTime: number,
	pendingContextTimes: readonly number[]
): number | null {
	if (!Number.isFinite(requestedSafeContextTime) || requestedSafeContextTime < 0) {
		throw new RangeError(
			`requestedSafeContextTime must be finite and non-negative, got ${requestedSafeContextTime}`
		);
	}
	const unsafePendingTimes = pendingContextTimes.filter((contextTime) => {
		if (!Number.isFinite(contextTime) || contextTime < 0) {
			throw new RangeError(
				`pending sync context time must be finite and non-negative, got ${contextTime}`
			);
		}
		return contextTime < requestedSafeContextTime;
	});
	return unsafePendingTimes.length === 0 ? null : Math.max(...unsafePendingTimes);
}

export function safeSyncScheduleTime(
	nowContextTime: number,
	maxLatencySec: number,
	masterReadyContextTime: number,
	safetySec = SYNC_SCHEDULE_SAFETY_S
): number {
	for (const [name, value] of Object.entries({
		nowContextTime,
		maxLatencySec,
		masterReadyContextTime,
		safetySec
	})) {
		if (!Number.isFinite(value) || value < 0) {
			throw new RangeError(`${name} must be finite and non-negative, got ${value}`);
		}
	}
	return Math.max(
		nowContextTime + maxLatencySec + safetySec,
		masterReadyContextTime + safetySec
	);
}

export function safeTransportScheduleTime(
	nowContextTime: number,
	latencySec: number,
	safetySec = SYNC_SCHEDULE_SAFETY_S
): number {
	for (const [name, value] of Object.entries({ nowContextTime, latencySec, safetySec })) {
		if (!Number.isFinite(value) || value < 0) {
			throw new RangeError(`${name} must be finite and non-negative, got ${value}`);
		}
	}
	if (safetySec === 0) throw new RangeError('safetySec must be greater than zero');
	return nowContextTime + latencySec + safetySec;
}

export function projectedTransportPosition(input: {
	now: number;
	startContextTime: number;
	startPositionSec: number;
	tempoRatio: number;
	projectAt: number;
}): number {
	for (const [name, value] of Object.entries(input)) {
		if (!Number.isFinite(value)) throw new RangeError(`${name} must be finite, got ${value}`);
	}
	if (input.tempoRatio <= 0) throw new RangeError('tempoRatio must be positive');
	if (input.projectAt < input.now) throw new RangeError('projectAt must not precede now');
	const projectionEpoch = Math.max(input.now, input.startContextTime);
	return input.startPositionSec + Math.max(0, input.projectAt - projectionEpoch) * input.tempoRatio;
}

export function normalizeEngagedLoopPositionSec(
	positionSec: number,
	loop: LoopState | null
): number {
	if (!Number.isFinite(positionSec) || positionSec < 0) {
		throw new RangeError(`positionSec must be finite and non-negative, got ${positionSec}`);
	}
	if (loop === null || !loop.engaged) return positionSec;
	const loopStartSec = loop.in_ms / 1000;
	const loopEndSec = loop.out_ms / 1000;
	const loopSpanSec = loopEndSec - loopStartSec;
	if (!Number.isFinite(loopSpanSec) || loopStartSec < 0 || loopSpanSec <= 0) {
		throw new RangeError(`engaged loop must satisfy 0 <= in_ms < out_ms`);
	}
	if (positionSec >= loopStartSec && positionSec < loopEndSec) return positionSec;
	const wrappedOffsetSec =
		((positionSec - loopStartSec) % loopSpanSec + loopSpanSec) % loopSpanSec;
	return loopStartSec + wrappedOffsetSec;
}

export function normalizeScheduledTransportEntrySec(
	positionSec: number,
	durationSec: number,
	loop: LoopState | null,
	active: boolean
): number {
	if (!Number.isFinite(durationSec) || durationSec <= 0) {
		throw new RangeError(`durationSec must be finite and positive, got ${durationSec}`);
	}
	if (!Number.isFinite(positionSec) || positionSec < 0 || positionSec > durationSec) {
		throw new RangeError(
			`positionSec must be within 0..${durationSec}, got ${positionSec}`
		);
	}
	if (typeof active !== 'boolean') {
		throw new TypeError(`active must be boolean, got ${String(active)}`);
	}
	if (!active) return positionSec;
	const normalizedPositionSec = normalizeEngagedLoopPositionSec(positionSec, loop);
	if (normalizedPositionSec > durationSec) {
		throw new RangeError(
			`normalized loop position ${normalizedPositionSec} exceeds duration ${durationSec}`
		);
	}
	return normalizedPositionSec;
}

export function deckReachedEnd(
	positionSec: number,
	durationSec: number,
	loop: LoopState | null
): boolean {
	return !loop?.engaged && positionSec >= durationSec;
}

/** Play on a finished (end-of-track) deck restarts from 0; otherwise resume. */
export function playResumePositionSec(
	positionSec: number,
	durationSec: number,
	loop: LoopState | null
): number {
	return deckReachedEnd(positionSec, durationSec, loop) ? 0 : positionSec;
}
