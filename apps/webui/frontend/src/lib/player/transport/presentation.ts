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
 * DEPENDENCY DIRECTION, load-bearing: clock -> presentation, never the reverse.
 * `_publishPresentedTransport` and `_tick` need `_rt`, so they stay in the
 * engine until S6 and land in transport/clock, which imports this module. If
 * this module ever imports clock or runtime, module-init order can hand one
 * side `undefined` and the waveform renders blank until a hot reload.
 */

import { _assertKeyShift } from '$lib/player/key/camelot';
import {
	_positionForSegment,
	deckReachedEnd,
	pausedSeekClock
} from '$lib/player/transport/schedule-math';
import type { _ClockSegment } from '$lib/player/transport/schedule-math';

export interface PresentedTransportSchedule extends _ClockSegment {
	revision: number;
	supersededByRevision: number | null;
}

export interface PresentedTransportTimeline {
	paused_position_sec: number;
	presented_position_sec: number;
	presented_active: boolean;
	desired_revision: number;
	presented_revision: number;
	last_presentation_context_time_s: number | null;
	last_presentation_performance_time_ms: number | null;
	schedules: PresentedTransportSchedule[];
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
		last_presentation_performance_time_ms: null,
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
	_prunePresentedTransportSchedules(timeline);
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

function _prunePresentedTransportSchedules(timeline: PresentedTransportTimeline): void {
	const presentedAt = timeline.last_presentation_context_time_s;
	const effective =
		presentedAt === null ? null : _effectivePresentedScheduleAt(timeline, presentedAt);
	timeline.schedules = timeline.schedules.filter(
		(schedule) =>
			schedule.supersededByRevision === null &&
			(presentedAt === null || schedule === effective || schedule.startContextTime > presentedAt)
	);
}

function _presentedObservation(
	timeline: PresentedTransportTimeline,
	accepted: boolean,
	outputStarted: boolean
): PresentedTransportObservation {
	return {
		accepted,
		output_started: outputStarted,
		presentation_context_time_s: timeline.last_presentation_context_time_s,
		position_sec: timeline.presented_position_sec,
		audible: timeline.presented_active,
		transport_pending: timeline.presented_revision !== timeline.desired_revision,
		desired_revision: timeline.desired_revision,
		presented_revision: timeline.presented_revision
	};
}

export function observePresentedTransportTimeline(
	timeline: PresentedTransportTimeline,
	outputTimestamp: { contextTime: number; performanceTime: number },
	durationSec: number
): PresentedTransportObservation {
	if (!Number.isFinite(durationSec) || durationSec <= 0) {
		throw new RangeError(`durationSec must be finite and positive, got ${durationSec}`);
	}
	const { contextTime, performanceTime } = outputTimestamp;
	if (
		!Number.isFinite(contextTime) ||
		!Number.isFinite(performanceTime) ||
		contextTime < 0 ||
		performanceTime < 0
	) {
		throw new RangeError(
			`output timestamp must contain finite non-negative values, got ` +
				`contextTime=${contextTime}, performanceTime=${performanceTime}`
		);
	}
	// Chromium may expose the current performance clock while the output frame
	// remains at zero during device warmup. Context time is presentation truth.
	if (contextTime === 0) {
		return _presentedObservation(
			timeline,
			false,
			timeline.last_presentation_context_time_s !== null
		);
	}
	const previousContextTime = timeline.last_presentation_context_time_s;
	if (previousContextTime !== null && contextTime < previousContextTime) {
		return _presentedObservation(timeline, false, true);
	}

	const selected = _effectivePresentedScheduleAt(timeline, contextTime);
	if (selected !== null && selected.revision < timeline.presented_revision) {
		throw new Error(
			`presented schedule revision regressed from ${timeline.presented_revision} to ` +
				`${selected.revision} at contextTime=${contextTime}`
		);
	}

	if (selected === null) {
		timeline.presented_position_sec = timeline.paused_position_sec;
		timeline.presented_active = false;
	} else {
		const crossesNewRevision = selected.revision > timeline.presented_revision;
		const positionSec = selected.active
			? _positionForSegment(selected, contextTime, durationSec)
			: crossesNewRevision
				? selected.startPositionSec
				: timeline.paused_position_sec;
		const audible = selected.active && !deckReachedEnd(positionSec, durationSec, selected.loop);
		timeline.presented_position_sec = positionSec;
		timeline.presented_active = audible;
		timeline.presented_revision = Math.max(timeline.presented_revision, selected.revision);
		if (!audible) timeline.paused_position_sec = positionSec;
	}
	timeline.last_presentation_context_time_s = contextTime;
	// Performance time is correlation/diagnostic data, never transport authority.
	timeline.last_presentation_performance_time_ms = performanceTime;
	_prunePresentedTransportSchedules(timeline);
	return _presentedObservation(timeline, true, true);
}
