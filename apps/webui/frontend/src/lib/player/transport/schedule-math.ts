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
 * This module is the player's pure leaf, so anything that several modules need
 * and that has no runtime access lands here rather than in the module the T4
 * table names. `_positionForSegment` (4 callers in 3 modules) and
 * `pausedSeekClock` (presentation plus commands) arrived that way; see the T4
 * report. Still outstanding: `naturalEndNeedsRevisionedStop`, which needs the
 * `PresentedTransportObservation` type and joins once presentation is imported
 * without a cycle.
 */

import { SYNC_SCHEDULE_SAFETY_S, TRANSPORT_IMMEDIATE_SAFETY_S } from '$lib/player/constants';
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

/**
 * The shared context time a beat-sync group launches at. Keeps the 100ms
 * `SYNC_SCHEDULE_SAFETY_S` margin deliberately: every participant must
 * acknowledge before this instant or the group is audibly out of phase.
 */
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

/**
 * The earliest context time a PLAIN transport mutation may be scheduled at.
 *
 * The default safety is the immediate-transport margin, not the beat-sync one:
 * this function's job is only "not in the past", and charging a single deck's
 * play/pause the 100ms multi-deck alignment margin is what made the button feel
 * slow (LATENCY-01). A caller that genuinely needs decks to agree on one shared
 * instant passes `SYNC_SCHEDULE_SAFETY_S` explicitly, or uses
 * `safeSyncScheduleTime`.
 */
export function safeTransportScheduleTime(
	nowContextTime: number,
	latencySec: number,
	safetySec = TRANSPORT_IMMEDIATE_SAFETY_S
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

/** One constant-rate stretch of transport: a start point, a rate, and the loop
 * in force. Everything that answers "where is the playhead" projects one of
 * these. Exported (name kept) because presentation, slip and clock all consume
 * it; see the T4 report for why it is here rather than in transport/clock. */
export interface _ClockSegment {
	active: boolean;
	loop: LoopState | null;
	startContextTime: number;
	startPositionSec: number;
	tempoRatio: number;
	masterTempoEnabled?: boolean;
	keyShiftSemitones?: number;
}

export function _positionForSegment(segment: _ClockSegment, at: number, durationSec: number): number {
	if (!segment.active) return segment.startPositionSec;
	const elapsed = Math.max(0, at - segment.startContextTime);
	const linear = segment.startPositionSec + elapsed * segment.tempoRatio;
	const loop = segment.loop;
	if (loop !== null && loop.engaged) {
		const loopStart = loop.in_ms / 1000;
		const loopEnd = loop.out_ms / 1000;
		if (linear >= loopEnd) return loopStart + ((linear - loopStart) % (loopEnd - loopStart));
	}
	return Math.min(linear, durationSec);
}

export function projectedLoopAwareTransportPosition(input: {
	active: boolean;
	loop: LoopState | null;
	startContextTime: number;
	startPositionSec: number;
	tempoRatio: number;
	projectAt: number;
	durationSec: number;
}): number {
	for (const [name, value] of Object.entries(input)) {
		if (name !== 'loop' && name !== 'active' && !Number.isFinite(value)) {
			throw new RangeError(`${name} must be finite, got ${String(value)}`);
		}
	}
	if (input.tempoRatio <= 0) throw new RangeError('tempoRatio must be positive');
	if (input.durationSec <= 0) throw new RangeError('durationSec must be positive');
	return _positionForSegment(
		{
			active: input.active,
			loop: input.loop,
			startContextTime: input.startContextTime,
			startPositionSec: input.startPositionSec,
			tempoRatio: input.tempoRatio
		},
		input.projectAt,
		input.durationSec
	);
}

export function pausedSeekClock(
	positionMs: number,
	durationMs: number
): { position_ms: number; start_offset_sec: number } {
	if (!Number.isFinite(durationMs) || durationMs <= 0) {
		throw new RangeError(`duration must be finite and positive, got ${durationMs}`);
	}
	if (!Number.isFinite(positionMs) || positionMs < 0 || positionMs > durationMs) {
		throw new RangeError(`position must be within track duration 0..${durationMs}, got ${positionMs}`);
	}
	return { position_ms: positionMs, start_offset_sec: positionMs / 1000 };
}
