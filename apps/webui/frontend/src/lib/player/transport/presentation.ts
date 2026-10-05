/**
 * The presented-transport mirror: what the DJ is actually hearing, as opposed
 * to what has been asked for. Extracted verbatim from audio-engine.svelte.ts
 * (T4 S3).
 *
 * A transport mutation is scheduled into the future, so between the request and
 * the moment audio actually starts at the new rate there is a window where the
 * desired state and the audible state disagree. This module owns that window:
 * schedules are acknowledged, superseded, pruned, and observed against
 * getOutputTimestamp() truth, and `audible` / `transport_pending` fall out of
 * that reconciliation rather than being set optimistically.
 *
 * Its acceptance rules exist because each has already gone wrong once: an old
 * schedule must never rewind presented state, and a {0,0} or repeated output
 * timestamp must leave pending state intact rather than reporting arrival.
 *
 * CLOCK AUTHORITY, and it is no longer unconditional. The output timestamp is
 * presentation truth WHILE IT IS ADVANCING. On Wed 2 Sep 2026 it stopped
 * advancing under a CoreAudio overload storm while the render-thread sample
 * clock kept going, and because this module trusted it unconditionally the
 * waveform froze for twenty minutes over live audio. It now falls back to the
 * sample clock for the duration of such a stall, reports the stall, and hands
 * authority straight back when the output timestamp recovers. The playhead
 * leads the audible position by the output latency while that fallback is
 * active, which is a real cost and is accepted deliberately:
 * `.planning/hardening-ledger/decisions/presentation-clock-fallback.md` is the
 * decision, its rejected alternative, and the conditions to revisit it.
 *
 * DEPENDENCY DIRECTION, load-bearing: clock -> presentation, never the reverse.
 * `_publishPresentedTransport` and `_tick` need `_rt`, so they stay in the
 * engine until S6 and land in transport/clock, which imports this module. If
 * this module ever imports clock or runtime, module-init order can hand one
 * side `undefined` and the waveform renders blank until a hot reload.
 */

import { _assertKeyShift, composeStretchSemitones } from '$lib/player/key/camelot';
import {
	_positionForSegment,
	deckReachedEnd,
	pausedSeekClock
} from '$lib/player/transport/schedule-math';
import type { _ClockSegment } from '$lib/player/transport/schedule-math';
import { createSlipAnchor } from '$lib/player/transport/slip-anchor';
import type { SlipAnchor, SlipTempoBoundary } from '$lib/player/transport/slip-anchor';

export interface PresentedTransportSchedule extends _ClockSegment {
	revision: number;
	supersededByRevision: number | null;
}

/**
 * How long the output timestamp may stand still before it is called stalled.
 *
 * Long enough that a dropped frame, a GC pause or a slow paint is not an
 * incident; short enough that an operator does not mix on a frozen playhead.
 */
export const PRESENTATION_STALL_MS = 500;

/** Which clock the position in an observation was computed from. */
export type PresentationClockSource = 'output' | 'sample';

export interface PresentedTransportTimeline {
	paused_position_sec: number;
	presented_position_sec: number;
	presented_active: boolean;
	desired_revision: number;
	presented_revision: number;
	last_presentation_context_time_s: number | null;
	/**
	 * CUEOUT-14: the instant the last accepted frame was PRESENTED at, which is
	 * `last_presentation_context_time_s` minus the room delay (or the render
	 * clock when a raised lag degraded the frame). Schedule pruning keys off
	 * this, never off the raw clock, so a schedule the room has not heard yet
	 * survives until it has.
	 */
	last_presented_instant_s: number | null;
	last_presentation_performance_time_ms: number | null;
	/**
	 * When the output timestamp was first seen to stop advancing, on the
	 * performance clock. null whenever it is advancing normally.
	 *
	 * Held as the START of the freeze rather than a duration so the elapsed time
	 * is derived from whatever sample arrives next, and a run of frames nobody
	 * observed cannot be lost.
	 */
	output_frozen_since_ms: number | null;
	schedules: PresentedTransportSchedule[];
}

/** Inputs shared by KEY SYNC commands and its listener-facing UI preview. */
export interface KeySyncEffectiveOffsetSource {
	audible: boolean;
	transportPending: boolean;
	pendingMutation: boolean;
	control: {
		tempoRatio: number;
		masterTempoEnabled: boolean;
		keyShiftSemitones: number;
	};
	presentation: PresentedTransportTimeline;
}

function _keySyncIsQuiescent(source: KeySyncEffectiveOffsetSource): boolean {
	return (
		!source.audible &&
		!source.transportPending &&
		!source.pendingMutation &&
		!source.presentation.presented_active &&
		source.presentation.desired_revision === source.presentation.presented_revision
	);
}

export function keySyncPreviewAvailable(source: KeySyncEffectiveOffsetSource): boolean {
	if (_keySyncIsQuiescent(source)) return true;
	const presentedAt = source.presentation.last_presentation_context_time_s;
	return presentedAt !== null && _effectivePresentedScheduleAt(source.presentation, presentedAt) !== null;
}

/** The key UI may read only the schedule that has crossed presentation time. */
export function presentedKeyShiftSemitonesAt(
	timeline: PresentedTransportTimeline,
	contextTime: number
): number | null {
	if (!Number.isFinite(contextTime) || contextTime < 0) {
		throw new RangeError(`presentation context time must be finite and non-negative, got ${contextTime}`);
	}
	return _effectivePresentedScheduleAt(timeline, contextTime)?.keyShiftSemitones ?? null;
}

export function presentedEffectiveAudibleSemitones(timeline: PresentedTransportTimeline): number {
	const presentedAt = timeline.last_presentation_context_time_s;
	if (presentedAt === null) {
		throw new Error('KEY SYNC requires output presentation truth before deriving effective offsets');
	}
	const schedule = _effectivePresentedScheduleAt(timeline, presentedAt);
	if (schedule === null) {
		throw new Error('KEY SYNC requires an output-presented schedule before deriving effective offsets');
	}
	return composeStretchSemitones(
		schedule.tempoRatio,
		schedule.masterTempoEnabled ?? true,
		schedule.keyShiftSemitones ?? 0
	);
}

/** A stopped deck uses its desired controls; live or queued decks use output truth. */
export function keySyncEffectiveAudibleSemitones(source: KeySyncEffectiveOffsetSource): number {
	for (const [name, value] of Object.entries({
		audible: source.audible,
		transportPending: source.transportPending,
		pendingMutation: source.pendingMutation
	})) {
		if (typeof value !== 'boolean') throw new TypeError(`KEY SYNC ${name} must be boolean`);
	}
	if (_keySyncIsQuiescent(source)) {
		return composeStretchSemitones(
			source.control.tempoRatio,
			source.control.masterTempoEnabled,
			source.control.keyShiftSemitones
		);
	}
	return presentedEffectiveAudibleSemitones(source.presentation);
}

/** A live command must start from its output-presented manual shift. */
export function keySyncManualShiftBaseline(source: KeySyncEffectiveOffsetSource): number {
	if (_keySyncIsQuiescent(source)) {
		_assertKeyShift(source.control.keyShiftSemitones);
		return source.control.keyShiftSemitones;
	}
	const presentedAt = source.presentation.last_presentation_context_time_s;
	if (presentedAt === null) {
		throw new Error('KEY SYNC requires output presentation truth before deriving its manual baseline');
	}
	const baseline = presentedKeyShiftSemitonesAt(source.presentation, presentedAt);
	if (baseline === null) {
		throw new Error('KEY SYNC requires an output-presented schedule before deriving its manual baseline');
	}
	_assertKeyShift(baseline);
	return baseline;
}

export interface PresentedTransportObservation {
	accepted: boolean;
	output_started: boolean;
	presentation_context_time_s: number | null;
	position_sec: number;
	audible: boolean;
	transport_pending: boolean;
	desired_revision: number;
	presented_revision: number;
	/**
	 * true stalled, false advancing, **null NOT KNOWN**.
	 *
	 * The third state is not a nicety. The stall is measured against
	 * `performanceTime`, and when that value is unreadable the honest answer is
	 * that we cannot tell - collapsing it into either neighbour would either
	 * raise an alarm on evidence that does not exist or hide a real freeze.
	 */
	clock_stalled: boolean | null;
	/** Which clock `position_sec` came from. 'sample' means the fallback is live. */
	clock_source: PresentationClockSource;
}

export function createPresentedTransportTimeline(
	pausedPositionSec: number
): PresentedTransportTimeline {
	if (!Number.isFinite(pausedPositionSec) || pausedPositionSec < 0) {
		throw new RangeError(
			`pausedPositionSec must be finite and non-negative, got ${pausedPositionSec}`
		);
	}
	return {
		paused_position_sec: pausedPositionSec,
		presented_position_sec: pausedPositionSec,
		presented_active: false,
		desired_revision: 0,
		presented_revision: 0,
		last_presentation_context_time_s: null,
		last_presented_instant_s: null,
		last_presentation_performance_time_ms: null,
		output_frozen_since_ms: null,
		schedules: []
	};
}

export function setPausedTransportTimelineCursor(
	timeline: PresentedTransportTimeline,
	positionSec: number,
	durationSec: number
): void {
	const clock = pausedSeekClock(positionSec * 1000, durationSec * 1000);
	if (timeline.presented_active || timeline.desired_revision !== timeline.presented_revision) {
		throw new Error('paused transport cursor cannot move while audio is active or pending');
	}
	timeline.paused_position_sec = clock.start_offset_sec;
	timeline.presented_position_sec = clock.start_offset_sec;
}

export function acknowledgePresentedTransportSchedule(
	timeline: PresentedTransportTimeline,
	schedule: Omit<PresentedTransportSchedule, 'supersededByRevision'>
): void {
	if (!Number.isInteger(schedule.revision) || schedule.revision <= 0) {
		throw new RangeError(`schedule revision must be a positive integer, got ${schedule.revision}`);
	}
	if (!Number.isFinite(schedule.startContextTime) || schedule.startContextTime < 0) {
		throw new RangeError(
			`schedule startContextTime must be finite and non-negative, got ${schedule.startContextTime}`
		);
	}
	if (!Number.isFinite(schedule.startPositionSec) || schedule.startPositionSec < 0) {
		throw new RangeError(
			`schedule startPositionSec must be finite and non-negative, got ${schedule.startPositionSec}`
		);
	}
	if (!Number.isFinite(schedule.tempoRatio) || schedule.tempoRatio <= 0) {
		throw new RangeError(
			`schedule tempoRatio must be finite and positive, got ${schedule.tempoRatio}`
		);
	}

	let supersededByRevision: number | null = null;
	if (schedule.revision <= timeline.desired_revision) {
		supersededByRevision = timeline.desired_revision;
	} else {
		for (const existing of timeline.schedules) {
			if (
				existing.supersededByRevision === null &&
				existing.revision > timeline.presented_revision &&
				existing.startContextTime >= schedule.startContextTime
			) {
				existing.supersededByRevision = schedule.revision;
			}
		}
		timeline.desired_revision = schedule.revision;
	}
	const masterTempoEnabled = schedule.masterTempoEnabled ?? true;
	const keyShiftSemitones = schedule.keyShiftSemitones ?? 0;
	_assertKeyShift(keyShiftSemitones);
	timeline.schedules.push({
		...schedule,
		loop: schedule.loop === null ? null : { ...schedule.loop },
		masterTempoEnabled,
		keyShiftSemitones,
		supersededByRevision
	});
	_prunePresentedTransportSchedules(timeline, timeline.last_presented_instant_s);
}

function _laterPresentedSchedule(
	candidate: PresentedTransportSchedule,
	selected: PresentedTransportSchedule | null
): boolean {
	return (
		selected === null ||
		candidate.startContextTime > selected.startContextTime ||
		(candidate.startContextTime === selected.startContextTime &&
			candidate.revision > selected.revision)
	);
}

/** Exported (name kept) for the key, slip and sync call sites still in the
 * engine; they resolve the schedule that has crossed the presentation clock. */
export function _effectivePresentedScheduleAt(
	timeline: PresentedTransportTimeline,
	contextTime: number
): PresentedTransportSchedule | null {
	let selected: PresentedTransportSchedule | null = null;
	for (const candidate of timeline.schedules) {
		if (
			candidate.supersededByRevision === null &&
			candidate.startContextTime <= contextTime &&
			_laterPresentedSchedule(candidate, selected)
		) {
			selected = candidate;
		}
	}
	return selected;
}

/** `presentedAt` is the instant the frame was PRESENTED at (the lagged one,
 * CUEOUT-14), never the raw output clock: pruning at the raw clock would drop
 * a schedule the room has not heard yet and paint the paused cursor next frame. */
function _prunePresentedTransportSchedules(
	timeline: PresentedTransportTimeline,
	presentedAt: number | null
): void {
	const effective =
		presentedAt === null ? null : _effectivePresentedScheduleAt(timeline, presentedAt);
	timeline.schedules = timeline.schedules.filter(
		(schedule) =>
			schedule.supersededByRevision === null &&
			(presentedAt === null || schedule === effective || schedule.startContextTime > presentedAt)
	);
}

/**
 * CUEOUT-14: the instant the ROOM is hearing when the render clock reads
 * `clockTime`. Normally `clockTime - presentationLagSec`. A lag RAISED mid-play
 * can point that instant before every surviving schedule (the older ones were
 * pruned while the lag was smaller): rather than blip to the paused cursor, or
 * regress the presented revision, such a frame degrades to the render clock,
 * which is exactly what every frame did before the lag existed. A schedule
 * that has crossed the render clock but was never presented stays pending -
 * that is the lag doing its job, not a degradation.
 */
function _presentationInstant(
	timeline: PresentedTransportTimeline,
	clockTime: number,
	presentationLagSec: number
): number {
	if (presentationLagSec === 0) return clockTime;
	const lagged = clockTime - presentationLagSec;
	const atLag = _effectivePresentedScheduleAt(timeline, lagged);
	if (atLag !== null && atLag.revision >= timeline.presented_revision) return lagged;
	const atClock = _effectivePresentedScheduleAt(timeline, clockTime);
	if (atClock === null || atClock.revision > timeline.presented_revision) return lagged;
	return clockTime;
}

function _presentedObservation(
	timeline: PresentedTransportTimeline,
	accepted: boolean,
	outputStarted: boolean,
	clockStalled: boolean | null = false,
	clockSource: PresentationClockSource = 'output'
): PresentedTransportObservation {
	return {
		accepted,
		output_started: outputStarted,
		clock_stalled: clockStalled,
		clock_source: clockSource,
		presentation_context_time_s: timeline.last_presentation_context_time_s,
		position_sec: timeline.presented_position_sec,
		audible: timeline.presented_active,
		transport_pending: timeline.presented_revision !== timeline.desired_revision,
		desired_revision: timeline.desired_revision,
		presented_revision: timeline.presented_revision
	};
}

/**
 * Advance presented state to `effectiveTime`, whichever clock supplied it.
 *
 * Extracted so the fallback runs the SAME arithmetic as the normal path rather
 * than a second copy of it: a fallback that computed the position differently
 * would make the playhead jump at the moment of the stall, on top of already
 * leading by the output latency.
 */
function _applyPresentedAt(
	timeline: PresentedTransportTimeline,
	effectiveTime: number,
	durationSec: number
): void {
	const selected = _effectivePresentedScheduleAt(timeline, effectiveTime);
	if (selected !== null && selected.revision < timeline.presented_revision) {
		throw new Error(
			`presented schedule revision regressed from ${timeline.presented_revision} to ` +
				`${selected.revision} at contextTime=${effectiveTime}`
		);
	}
	if (selected === null) {
		timeline.presented_position_sec = timeline.paused_position_sec;
		timeline.presented_active = false;
		return;
	}
	const crossesNewRevision = selected.revision > timeline.presented_revision;
	const positionSec = selected.active
		? _positionForSegment(selected, effectiveTime, durationSec)
		: crossesNewRevision
			? selected.startPositionSec
			: timeline.paused_position_sec;
	const audible = selected.active && !deckReachedEnd(positionSec, durationSec, selected.loop);
	timeline.presented_position_sec = positionSec;
	timeline.presented_active = audible;
	timeline.presented_revision = Math.max(timeline.presented_revision, selected.revision);
	if (!audible) timeline.paused_position_sec = positionSec;
}

/**
 * Observe one frame of output truth, and keep the playhead moving if it lies.
 *
 * `sampleClockTimeS` is `AudioContext.currentTime` - the render-thread clock,
 * the same source `deckAudioClockPositionMs()` already reads. It is OPTIONAL:
 * without it this function still detects and reports a stall, it simply has
 * nothing to fall back to. That is deliberate, so a caller that cannot supply
 * the clock degrades to detection rather than to silence.
 *
 * See `.planning/hardening-ledger/decisions/presentation-clock-fallback.md`
 * for why the fallback exists, and for the output-latency lead it costs.
 */
export function observePresentedTransportTimeline(
	timeline: PresentedTransportTimeline,
	outputTimestamp: { contextTime: number; performanceTime: number },
	durationSec: number,
	sampleClockTimeS?: number,
	presentationLagSec = 0
): PresentedTransportObservation {
	if (!Number.isFinite(durationSec) || durationSec <= 0) {
		throw new RangeError(`durationSec must be finite and positive, got ${durationSec}`);
	}
	// CUEOUT-14: the room delay line sits after every clock this function reads,
	// so what the room hears is this many seconds behind the render clock.
	if (!Number.isFinite(presentationLagSec) || presentationLagSec < 0) {
		throw new RangeError(
			`presentation lag must be finite and non-negative seconds, got ${presentationLagSec}`
		);
	}
	const { contextTime, performanceTime } = outputTimestamp;
	// contextTime IS transport authority, so it keeps failing fast: a playhead
	// computed from a value that is not a time is worse than a frozen one.
	if (!Number.isFinite(contextTime) || contextTime < 0) {
		throw new RangeError(
			`output timestamp contextTime must be finite and non-negative, got ${contextTime}`
		);
	}
	// performanceTime is correlation/diagnostic data and NEVER transport
	// authority - it says so three lines below in the code that stores it. It
	// used to throw here, and since WebKit EXTRAPOLATES it, one bad extrapolation
	// could take out the rAF loop that paints the waveform for the rest of the
	// session. It now degrades to "unreadable", which costs only the ability to
	// time a stall.
	const clockMs =
		Number.isFinite(performanceTime) && performanceTime >= 0 ? performanceTime : null;

	const previousContextTime = timeline.last_presentation_context_time_s;
	const outputStarted = previousContextTime !== null;
	// Three ways the output clock fails to advance, and all three froze the
	// waveform on Wed 2 Sep 2026: it repeats, it regresses, or it drops to 0.
	const outputAdvancing =
		contextTime > 0 && (previousContextTime === null || contextTime > previousContextTime);
	// A repeat is USABLE even though it is not advancing, and the distinction is
	// load-bearing. rAF runs at ~60Hz while the device hands over frames at its
	// own rate, so a repeated timestamp is the ordinary case several times a
	// second on a healthy machine; recomputing at the same time yields the same
	// position, which is correct. What a repeat must NOT do is reset the freeze
	// timer - a run of them for longer than the window is exactly the freeze.
	const outputUsable =
		contextTime > 0 && (previousContextTime === null || contextTime >= previousContextTime);

	let clockStalled: boolean | null;
	if (outputAdvancing) {
		timeline.output_frozen_since_ms = null;
		clockStalled = false;
	} else if (!outputStarted) {
		// Waiting for a device to hand over its first frame is not a freeze.
		clockStalled = false;
	} else if (clockMs === null) {
		// The clock the stall is measured against is unreadable. "I cannot tell"
		// and "it has stalled" are different answers and must not be collapsed.
		clockStalled = null;
	} else {
		if (timeline.output_frozen_since_ms === null) timeline.output_frozen_since_ms = clockMs;
		clockStalled = clockMs - timeline.output_frozen_since_ms > PRESENTATION_STALL_MS;
	}

	const fallbackTime =
		clockStalled === true &&
		sampleClockTimeS !== undefined &&
		Number.isFinite(sampleClockTimeS) &&
		previousContextTime !== null &&
		sampleClockTimeS > previousContextTime
			? sampleClockTimeS
			: null;

	// The fallback is checked FIRST once the clock is stalled: a repeated
	// timestamp is still "usable" arithmetic, but using it is precisely what
	// paints the identical frame forever.
	if (fallbackTime === null && outputUsable) {
		const presentedAt = _presentationInstant(timeline, contextTime, presentationLagSec);
		_applyPresentedAt(timeline, presentedAt, durationSec);
		timeline.last_presentation_context_time_s = contextTime;
		timeline.last_presented_instant_s = presentedAt;
		if (clockMs !== null) timeline.last_presentation_performance_time_ms = clockMs;
		_prunePresentedTransportSchedules(timeline, presentedAt);
		return _presentedObservation(timeline, true, true, clockStalled, 'output');
	}

	if (fallbackTime !== null) {
		_applyPresentedAt(
			timeline,
			_presentationInstant(timeline, fallbackTime, presentationLagSec),
			durationSec
		);
		if (clockMs !== null) timeline.last_presentation_performance_time_ms = clockMs;
		// last_presentation_context_time_s is deliberately NOT advanced: it is the
		// last value the output clock was TRUSTED at, and it is what recovery is
		// detected against. Advancing it here would make the output timestamp look
		// permanently behind and the fallback would never hand authority back.
		return _presentedObservation(timeline, true, true, true, 'sample');
	}

	return _presentedObservation(timeline, false, outputStarted, clockStalled, 'output');
}

/** Retain accepted, effective future presentation schedules after a SLIP anchor. */
export function slipTempoBoundariesAfterAnchor(
	timeline: PresentedTransportTimeline,
	anchor: SlipAnchor
): SlipTempoBoundary[] {
	const validAnchor = createSlipAnchor(anchor);
	const candidates = timeline.schedules
		.filter(
			(schedule) =>
				schedule.supersededByRevision === null &&
				schedule.active &&
				schedule.startContextTime > validAnchor.startContextTime
		)
		.sort(
			(left, right) =>
				left.startContextTime - right.startContextTime || left.revision - right.revision
		);
	const boundaries: SlipTempoBoundary[] = [];
	for (const schedule of candidates) {
		const previous = boundaries[boundaries.length - 1];
		if (previous?.startContextTime === schedule.startContextTime) {
			previous.tempoRatio = schedule.tempoRatio;
		} else {
			boundaries.push({ startContextTime: schedule.startContextTime, tempoRatio: schedule.tempoRatio });
		}
	}
	return boundaries;
}

/** Create a hidden SLIP anchor from the listener-facing engaged loop only. */
export function presentedSlipAnchor(
	timeline: PresentedTransportTimeline,
	durationSec: number
): SlipAnchor {
	if (!Number.isFinite(durationSec) || durationSec <= 0) {
		throw new RangeError(`SLIP duration must be positive and finite, got ${durationSec}`);
	}
	const presentedAt = timeline.last_presentation_context_time_s;
	if (presentedAt === null) throw new Error('SLIP requires output presentation truth before activation');
	const schedule = _effectivePresentedScheduleAt(timeline, presentedAt);
	if (schedule === null || !schedule.active || schedule.loop?.engaged !== true) {
		throw new Error('SLIP requires an output-presented engaged loop before activation');
	}
	return createSlipAnchor({
		startContextTime: presentedAt,
		startPositionSec: _positionForSegment(schedule, presentedAt, durationSec),
		tempoRatio: schedule.tempoRatio,
		durationSec
	});
}

// ---------------------------------------------------- slip anchor re-export
//
// The SLIP hidden-transport algebra lives WHOLE in transport/slip-anchor.ts
// (pure, importless). It is re-exported here so the engine reaches it through
// the presentation barrel it already imports, rather than taking a new direct
// module edge.

export {
	createSlipAnchor,
	rebaseSlipAnchor,
	shouldActivateSlip,
	slipHiddenPositionSec,
	slipHiddenPositionWithTempoBoundaries
} from '$lib/player/transport/slip-anchor';
export type { SlipAnchor, SlipTempoBoundary } from '$lib/player/transport/slip-anchor';
