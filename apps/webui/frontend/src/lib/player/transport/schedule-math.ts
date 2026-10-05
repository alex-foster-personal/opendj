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

import {
	PROCESSOR_ONSET_RAMP_FACTOR,
	SYNC_SCHEDULE_SAFETY_S,
	TRANSPORT_IMMEDIATE_SAFETY_S
} from '$lib/player/constants';
import { inputToOutputMs, isMeasuredLatencyFloor } from '$lib/player/transport/press-audible';
import type { LoopState } from '$lib/rb/deck-state-types';

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
 * The lead an IMMEDIATE transport schedule must give the processor for the
 * start to arrive CRISP rather than soft.
 *
 * This is the round-2 replacement for passing the processor's own
 * `latency()` self-report as the lead. Two facts make the substitution safe,
 * both measured (`.planning/latency-round2-design.md`):
 *
 * 1. The self-report is a LIVE-INPUT figure. In buffer mode - the only mode our
 *    decks ever use - the worklet compensates it internally, so audio scheduled
 *    with ample lead arrives exactly on time with no trace of it. Leading by
 *    the self-report bought nothing.
 * 2. What a short lead DOES cost is the onset ramp, and the ramp is a fixed
 *    fraction of the STFT block. Because `latency()` equals the block length
 *    exactly for this processor, the ramp is derivable from the same cached
 *    number, with no new query on the transport path.
 *
 * The durable rule this encodes: lead by what the processor measurably NEEDS,
 * not by what it reports about itself. A future processor plugs in by supplying
 * its own `rampFactor` rather than by re-teaching every call site.
 *
 * `rampFactor` is bounded to (0, 1]: the ramp is a fraction OF the block, so a
 * factor above 1 means the derivation was inverted somewhere, and a factor of 0
 * means a start with no lead at all, which is measurably soft.
 */
export function processorOnsetLeadSec(
	processorLatencySec: number,
	rampFactor: number = PROCESSOR_ONSET_RAMP_FACTOR
): number {
	for (const [name, value] of Object.entries({ processorLatencySec, rampFactor })) {
		if (!Number.isFinite(value) || value < 0) {
			throw new RangeError(`${name} must be finite and non-negative, got ${value}`);
		}
	}
	if (rampFactor <= 0) {
		throw new RangeError(
			`rampFactor must be greater than zero, got ${rampFactor}: a zero lead means ` +
				'every scheduled start pays the whole onset ramp as lateness'
		);
	}
	if (rampFactor > 1) {
		throw new RangeError(
			`rampFactor ${rampFactor} exceeds 1: the onset ramp is a FRACTION of the block ` +
				'length, so a factor above 1 means the derivation was inverted and the lead ' +
				'is larger than the self-report it was meant to replace'
		);
	}
	return processorLatencySec * rampFactor;
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
 *
 * ROUND 2 renamed the middle argument from `latencySec` to `processorLeadSec`,
 * because what belongs there stopped being the processor's self-report and
 * became `processorOnsetLeadSec(...)` of it. The old name would now describe
 * the wrong quantity, and a call site handing this function a raw `latency()`
 * on the strength of that name would silently reinstate the 128ms schedule.
 */
export function safeTransportScheduleTime(
	nowContextTime: number,
	processorLeadSec: number,
	safetySec = TRANSPORT_IMMEDIATE_SAFETY_S
): number {
	for (const [name, value] of Object.entries({ nowContextTime, processorLeadSec, safetySec })) {
		if (!Number.isFinite(value) || value < 0) {
			throw new RangeError(`${name} must be finite and non-negative, got ${value}`);
		}
	}
	if (safetySec === 0) throw new RangeError('safetySec must be greater than zero');
	return nowContextTime + processorLeadSec + safetySec;
}

/**
 * LATENCY-03: the stage row for one scheduled transport mutation.
 *
 * `effectiveWhenSec` MUST be the POST-CLAMP time actually handed to the
 * WebAudio call, never the time a call site requested. The two differ exactly
 * when the not-in-the-past floor re-inflates a schedule, which is the failure
 * this instrument exists to catch: an offset logged from the requested value
 * would read 8ms while the real schedule sat at 128ms, and the guard would pass
 * while the thing it guards was broken. Both are recorded so a clamp is visible
 * as a gap between them rather than as silence.
 *
 * Every device floor travels WITH the sample: baseLatency and outputLatency are
 * machine-specific, so a number compared across machines without them is a
 * number compared against nothing.
 *
 * ROUND 2 split the processor term in two, because they stopped being the same
 * number. `processorLeadSec` is what the schedule floor actually CHARGED (the
 * onset-ramp lead); `processorLatencySec` is what the processor SAYS about
 * itself. Reporting only the self-report would make `safety_ms` - defined as
 * the offset with the processor's share removed - read as a large negative and
 * quietly pass a `<= 30ms` ceiling while meaning nothing. Both are logged, so
 * the gap between them is exactly the dead weight round 2 removed.
 *
 * PERF-R4 / Q1 adds the half this row could never see. Every number above is
 * measured from `contextTimeSec`, which is read INSIDE the engine's serialized
 * schedule body - downstream of the command scheduler's scope wait, the context
 * resume, and the deck's own schedule tail. `pressToScheduleMs` is the ms from
 * the operator's input stamp to that read, so `input_to_audible_ms` is the
 * whole S2 budget rather than its cheap tail. It is OPTIONAL because most
 * schedules have no press behind them (beat-sync follower alignment, autoplay,
 * slip resume); those rows must not be quoted as if a human had pressed
 * something, so the two keys are absent rather than zero.
 *
 * `input_to_audible_ms` is a MODEL of the audible instant, not a measurement of
 * one: it stops at the scheduled context time and deliberately excludes
 * `base_latency_ms` and `output_latency_ms`, which travel with the sample so a
 * consumer can add the device floor back when comparing across machines.
 *
 * `input_to_output_ms` is that same press with the floor ADDED, i.e. the press
 * carried to sound leaving the device, and it is the figure closest to what an
 * operator's ears measure. It appears only when every term of the floor was
 * really measured. "Add the device floor back" turned out to be advice a
 * consumer could not safely follow: `outputLatency` is a present, finite,
 * `number`-typed ZERO until the AudioContext has rendered, so the addition
 * silently contributed nothing on exactly the schedule that matters most, the
 * first play of a session. The floor terms are therefore omitted rather than
 * zeroed, this sum is withheld rather than partial, and the row's
 * `latency_floor` label states which of the two happened.
 */
export function scheduleOffsetStages(input: {
	contextTimeSec: number;
	requestedWhenSec: number;
	effectiveWhenSec: number;
	processorLeadSec: number;
	processorLatencySec: number;
	/**
	 * Q1: the device floor, where the platform actually knows it.
	 *
	 * OPTIONAL because unavailability is a real state here, not a fault. An
	 * unmeasured term is OMITTED from the stages rather than emitted as a zero
	 * that reads exactly like a measurement of zero - see `press-audible.ts`
	 * for the engine numbers, and for why the observed failure is a
	 * present-but-zero property rather than an absent one.
	 */
	baseLatencySec: number | undefined;
	outputLatencySec: number | undefined;
	/** CUEOUT-14: the room delay line, carried into `input_to_output_ms` only;
	 * validated by `inputToOutputMs`. */
	masterDelayMs?: number;
	active: boolean;
	/** Q1: ms from the input stamp to the `contextTimeSec` read. */
	pressToScheduleMs?: number;
}): Record<string, number> {
	for (const [name, value] of Object.entries(input)) {
		if (name === 'active' || name === 'pressToScheduleMs' || name === 'masterDelayMs') continue;
		if (name === 'baseLatencySec' || name === 'outputLatencySec') continue;
		if (!Number.isFinite(value)) throw new RangeError(`${name} must be finite, got ${value}`);
	}
	const pressToScheduleMs = input.pressToScheduleMs;
	if (pressToScheduleMs !== undefined) {
		if (!Number.isFinite(pressToScheduleMs) || pressToScheduleMs < 0) {
			throw new RangeError(
				`pressToScheduleMs must be finite and non-negative, got ${pressToScheduleMs}: a ` +
					'schedule cannot reach the audio clock before the input that asked for it, so ' +
					'a negative delta means the stamp came from a different clock epoch and the ' +
					'number would understate the P0 budget it gates'
			);
		}
	}
	if (typeof input.active !== 'boolean') {
		throw new TypeError(`active must be boolean, got ${String(input.active)}`);
	}
	if (input.effectiveWhenSec < input.contextTimeSec) {
		throw new RangeError(
			`effective schedule ${input.effectiveWhenSec} precedes context time ` +
				`${input.contextTimeSec}: a mutation cannot be scheduled in the past`
		);
	}
	if (input.processorLeadSec < 0 || input.processorLatencySec < 0) {
		throw new RangeError(
			`processor lead ${input.processorLeadSec} and latency ${input.processorLatencySec} ` +
				'must be non-negative'
		);
	}
	if (input.processorLeadSec > input.processorLatencySec) {
		throw new RangeError(
			`processor lead ${input.processorLeadSec}s exceeds the self-report ` +
				`${input.processorLatencySec}s: the onset-ramp lead is a FRACTION of the block ` +
				'the self-report equals, so a larger lead means round 2 got inverted and the ' +
				'schedule is now slower than the 128ms it replaced'
		);
	}
	const round = (value: number): number => Math.round(value * 1000) / 1000;
	const scheduledOffsetMs = (input.effectiveWhenSec - input.contextTimeSec) * 1000;
	const processorLeadMs = input.processorLeadSec * 1000;
	const inputToOutput = inputToOutputMs({
		pressToScheduleMs,
		scheduledOffsetMs,
		baseLatencySec: input.baseLatencySec,
		outputLatencySec: input.outputLatencySec,
		...(input.masterDelayMs === undefined ? {} : { masterDelayMs: input.masterDelayMs })
	});
	return {
		// The Class A budget turns on this one, post-clamp.
		scheduled_offset_ms: round(scheduledOffsetMs),
		// What the call site asked for. Below scheduled_offset_ms means a clamp fired.
		requested_offset_ms: round((input.requestedWhenSec - input.contextTimeSec) * 1000),
		// The margin the schedule policy owns, with the processor's share removed.
		safety_ms: round(scheduledOffsetMs - processorLeadMs),
		// The onset-ramp lead the floor actually charged for the processor.
		processor_lead_ms: round(processorLeadMs),
		// What the processor says about ITSELF. Round 2 stopped spending this;
		// it stays logged so the gap to processor_lead_ms is visible.
		processor_latency_ms: round(input.processorLatencySec * 1000),
		// Q1: present only when actually measured. A term the platform has not
		// filled in yet is absent, and the row's `latency_floor` label says so,
		// because an `output_latency_ms: 0` is indistinguishable from a real
		// reading of zero and understates the floor by ~16ms on the shipped
		// engine. Absent is honest; zero is a lie with a number on it.
		...(isMeasuredLatencyFloor(input.baseLatencySec)
			? { base_latency_ms: round((input.baseLatencySec as number) * 1000) }
			: {}),
		...(isMeasuredLatencyFloor(input.outputLatencySec)
			? { output_latency_ms: round((input.outputLatencySec as number) * 1000) }
			: {}),
		active: input.active ? 1 : 0,
		// Q1, appended so the ratchet keys keep their place in the `[perf]` line.
		...(pressToScheduleMs === undefined
			? {}
			: {
					// The invisible half: input stamp -> the currentTime read above.
					press_to_schedule_ms: round(pressToScheduleMs),
					// The S2 number. Both halves or it measures the wrong thing.
					// Models the SCHEDULED START, so it stops short of the device
					// floor; `input_to_output_ms` below is the same press carried
					// all the way to sound leaving the output.
					input_to_audible_ms: round(pressToScheduleMs + scheduledOffsetMs)
				}),
		// Q1: press -> sound out of the device, floor included. Emitted only when
		// EVERY term is a measurement, never as a partial sum: a press-to-output
		// figure missing its output stage is not a smaller number, it is a wrong
		// one, and wrong in the flattering direction.
		...(inputToOutput === undefined ? {} : { input_to_output_ms: round(inputToOutput) })
	};
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
	// Route through the shared end-of-track tolerance (see clampSeekTargetMs):
	// a paused position saved from a deck that played to completion can read
	// up to 1ms past this raw decoded-buffer duration. Re-thrown with this
	// function's own wording so a position genuinely outside the track still
	// reports "position ... duration", not the generic helper's message.
	let clampedMs: number;
	try {
		clampedMs = clampSeekTargetMs(positionMs, durationMs, 'pausedSeekClock');
	} catch {
		throw new RangeError(`position must be within track duration 0..${durationMs}, got ${positionMs}`);
	}
	return { position_ms: clampedMs, start_offset_sec: clampedMs / 1000 };
}

/**
 * Clamp a seek target to a decoded track's duration, tolerating the one case
 * a raw float duration and an integer-rounded saved position can disagree on:
 * a track that played to the end. `durationMs` comes from a decoded buffer's
 * sample count (a float with sub-ms remainder); a position persisted from it
 * is rounded, so it can read up to 1ms higher than the raw duration it was
 * taken from. Without this, session restore throws for every deck that had
 * simply played to completion (seen live: "cueJump: ms must be within
 * 0..248059, got 248059" -- the deck was never restored).
 *
 * Only that rounding margin is forgiven. `ms` further past the end is still a
 * real error: inclusive-rounded is the upper bound, not "anything goes".
 */
export function clampSeekTargetMs(ms: number, durationMs: number, label = 'seek'): number {
	if (!Number.isFinite(durationMs) || durationMs <= 0) {
		throw new RangeError(`${label}: duration must be finite and positive, got ${durationMs}`);
	}
	if (!Number.isFinite(ms) || ms < 0 || ms > Math.round(durationMs)) {
		throw new RangeError(`${label}: ms must be within 0..${Math.round(durationMs)}, got ${ms}`);
	}
	return ms > durationMs ? durationMs : ms;
}

export function decodedTransportDurationMs(decodedDurationSec: number): number {
	if (!Number.isFinite(decodedDurationSec) || decodedDurationSec <= 0) {
		throw new RangeError(
			`decoded audio duration must be finite and positive, got ${decodedDurationSec}`
		);
	}
	return decodedDurationSec * 1000;
}
