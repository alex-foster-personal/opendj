/**
 * CUEOUT-14: the alignment policy, separate from the calibration state machine
 * in cue-align.svelte.ts. Pure functions only: the mode table that turns a
 * measured offset into delays, and the readiness rules that decide whether a
 * calibration may start. The modal, the cluster button and the engine all read
 * these, so none of them needs the state machine to answer "may I calibrate?".
 */

import {
	HEAD_DELAY_MAX_MS,
	MASTER_DELAY_MAX_MS,
	assertHeadphoneAlignmentMode,
	assertHeadphoneOutputMode
} from '$lib/player/constants';
import { CUE_LATENCY_GAIN_STEPS, CUE_LATENCY_PREROLL_MS, cueLatencyCaptureMs } from '$lib/player/cue-latency';

/** Three back-to-back master/cue pairs in stage two; the ramps only find the level. */
export const CUE_ALIGN_RUNS = 3;
/** The longest lag either bus may show; sizes the capture window. */
export const CUE_ALIGN_MAX_LAG_MS = MASTER_DELAY_MAX_MS;

export interface AlignmentPlan {
	head_delay_ms: number;
	master_delay_ms: number;
	/** Standing copy for the cluster when a mode leaves the phones behind or a cap bit. */
	warning: string | null;
	capped: boolean;
}

function _assertOffsetMs(offsetMs: unknown): asserts offsetMs is number {
	if (typeof offsetMs !== 'number' || !Number.isFinite(offsetMs)) {
		throw new RangeError(`alignment offset must be a finite number of ms, got ${String(offsetMs)}`);
	}
}

/**
 * The cue-minus-master gap a plan leaves on purpose: none when it fully
 * corrects, the whole offset in headphones_only, the remainder past a cap.
 * Verification scores the measured gap against this, not against zero.
 */
export function intendedResidualMs(offsetMs: number, plan: AlignmentPlan): number {
	return Math.round(offsetMs) - plan.master_delay_ms + plan.head_delay_ms;
}

/**
 * The mode table from the spec. `offsetMs = cue_latency_ms - master_latency_ms`:
 * negative means the phones are AHEAD (delay the phones), positive BEHIND
 * (delay the room, or in headphones_only warn and delay nothing).
 */
export function deriveAlignment(mode: unknown, offsetMs: unknown): AlignmentPlan {
	assertHeadphoneAlignmentMode(mode);
	_assertOffsetMs(offsetMs);
	const rounded = Math.round(offsetMs);
	if (rounded === 0) return { head_delay_ms: 0, master_delay_ms: 0, warning: null, capped: false };
	if (rounded < 0) {
		const wanted = -rounded;
		const capped = wanted > HEAD_DELAY_MAX_MS;
		return {
			head_delay_ms: Math.min(HEAD_DELAY_MAX_MS, wanted),
			master_delay_ms: 0,
			warning: capped
				? `HEAD DELAY capped at ${HEAD_DELAY_MAX_MS} ms; the phones are ${wanted} ms ahead of the room.`
				: null,
			capped
		};
	}
	if (mode === 'headphones_only') {
		return {
			head_delay_ms: 0,
			master_delay_ms: 0,
			warning: `headphones are ${rounded} ms behind; switch alignment mode to delay the room`,
			capped: false
		};
	}
	const capped = rounded > MASTER_DELAY_MAX_MS;
	return {
		head_delay_ms: 0,
		master_delay_ms: Math.min(MASTER_DELAY_MAX_MS, rounded),
		warning: capped
			? `room delay capped at ${MASTER_DELAY_MAX_MS} ms; the phones are ${rounded} ms behind, so ${rounded - MASTER_DELAY_MAX_MS} ms remain.`
			: null,
		capped
	};
}

/** CALIBRATE is live only in two_outputs with a selected cue sink. The label
 * (wired jack, Bluetooth, anything) is deliberately not consulted. */
export function calibrateButtonEnabled(args: {
	output_mode: unknown;
	selected_output_device_id: string | null;
}): boolean {
	assertHeadphoneOutputMode(args.output_mode);
	if (args.selected_output_device_id !== null && typeof args.selected_output_device_id !== 'string') {
		throw new TypeError('selected headphone output device id must be a string or null');
	}
	return args.output_mode === 'two_outputs' && args.selected_output_device_id !== null;
}

/** Every reason calibration cannot start right now, in the operator's words.
 * The modal, the error the engine raises and the agent-facing commands all
 * read from this one list, so a missing audio graph can never be reported as
 * a missing device. An empty list means calibration is ready to run. */
export function calibrationBlockers(args: {
	audio_graph_ready: unknown;
	output_mode: unknown;
	selected_output_device_id: string | null;
}): string[] {
	if (typeof args.audio_graph_ready !== 'boolean') {
		throw new TypeError('audio_graph_ready must be a boolean');
	}
	const blockers: string[] = [];
	if (!args.audio_graph_ready) {
		blockers.push('the audio graph is not built yet, so load a deck and start playback once');
	}
	if (
		!calibrateButtonEnabled({
			output_mode: args.output_mode,
			selected_output_device_id: args.selected_output_device_id
		})
	) {
		if (args.output_mode !== 'two_outputs') {
			blockers.push(`CUE OUT is ${String(args.output_mode)}, and calibration needs two_outputs`);
		}
		if (args.selected_output_device_id === null) {
			blockers.push('no headphone output device is selected in the I/O pane');
		}
	}
	return blockers;
}

/** Roughly how long a full run takes, for the intro copy. */
/** Every capture a run can make: each gain rung on both buses while finding the
 * level, each measured probe plus its one louder retry, and the verify pair plus
 * its one retry. An upper bound, so a healthy run ends early rather than
 * overrunning the promise and looking hung. */
const CUE_ALIGN_MAX_CAPTURES = 2 * CUE_LATENCY_GAIN_STEPS.length + CUE_ALIGN_RUNS * 2 * 2 + 2 * 2;

export function estimatedCalibrationSeconds(): number {
	const perCaptureMs = CUE_LATENCY_PREROLL_MS + cueLatencyCaptureMs(CUE_ALIGN_MAX_LAG_MS);
	return Math.ceil((perCaptureMs * CUE_ALIGN_MAX_CAPTURES) / 1000);
}
