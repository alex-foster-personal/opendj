/**
 * Loop endpoint math. Pure: takes a PQTZ beat grid and millisecond endpoints,
 * returns millisecond endpoints or throws. Extracted verbatim from
 * audio-engine.svelte.ts (T4 S2).
 *
 * An empty or collapsed loop is always an explicit error rather than a silent
 * clamp, because a zero-length loop is inaudible and would strand the deck with
 * no way for the DJ to tell why.
 *
 * The 5 loop and safety-loop class methods join this module at S10, once the
 * scheduler seam they mutate through has been extracted.
 */

import {
	quantizeToNearestBeat,
	quantizeToNearestGridBeat,
	validateBeatGrid
} from '$lib/rb/beat-sync-math';
import type { AnlzBeat } from '$lib/rb/anlz-types';

type LoopSnapshot = {
	in_ms: number;
	out_ms: number;
	engaged: boolean;
	beat_length: number | null;
};

type SafetyLoopSnapshot = {
	in_ms: number;
	out_ms: number;
	beat_length: number | null;
	armed: boolean;
};

/** Resolve the complete loop snapshot before scheduling. Manual loops snap
 * to the selected grid when available; PQTZ beat loops keep their already
 * resolved fractional endpoints. Both paths remain bounded by real duration. */
export function resolvedLoopState(
	loop: { in_ms: number; out_ms: number },
	beatLength: number | null,
	durationMs: number,
	beats: readonly AnlzBeat[] | null,
	gridBeats: 1 | 4 | 8 | null
): LoopSnapshot {
	const snapped = beats !== null && gridBeats !== null && beatLength === null
		? quantizedLoopEndpointsMs(beats, loop, true, gridBeats)
		: quantizedLoopEndpointsMs([], loop, false);
	return {
		...loopEndpointsWithinDurationMs(snapped, durationMs),
		engaged: true,
		beat_length: beatLength
	};
}

/** Keep a saved safety loop current only when it was captured from the exact
 * engaged loop being resized. A separately saved range remains intentional. */
export function replaceMatchingSafetyLoopSnapshot(
	safety: SafetyLoopSnapshot | null,
	previous: LoopSnapshot,
	next: LoopSnapshot
): SafetyLoopSnapshot | null {
	if (
		safety === null ||
		safety.in_ms !== previous.in_ms ||
		safety.out_ms !== previous.out_ms ||
		safety.beat_length !== previous.beat_length
	) {
		return safety;
	}
	return {
		in_ms: next.in_ms,
		out_ms: next.out_ms,
		beat_length: next.beat_length,
		armed: safety.armed
	};
}

/** Rebuild an armed safety slot as an exact PQTZ beat loop. The caller
 * decides which playback event triggers engagement; this helper only refuses
 * slots that cannot be reconstructed on-grid or that overrun duration. */
export function phaseLockedSafetyLoop(
	beats: readonly AnlzBeat[],
	safety: SafetyLoopSnapshot,
	durationMs: number
): LoopSnapshot | null {
	if (safety.beat_length === null) return null;
	if (!Number.isFinite(safety.beat_length) || safety.beat_length <= 0) return null;
	try {
		const range = exactBeatLoopRangeMs(beats, safety.in_ms, safety.beat_length, safety.in_ms);
		if (range.out_ms > durationMs) return null;
		return { ...range, engaged: true, beat_length: safety.beat_length };
	} catch {
		return null;
	}
}

/** True when linear playback on the current control segment has reached the
 * saved safety loop's exclusive out. Uses the control segment, not presented
 * position_ms, so a seek that starts past out does not count as reaching it. */
export function playbackReachedSafetyLoopOut(input: {
	active: boolean;
	startPositionSec: number;
	startContextTime: number;
	tempoRatio: number;
	atContextTime: number;
	safety: SafetyLoopSnapshot | null;
	liveLoop: LoopSnapshot | null;
}): boolean {
	const { safety, liveLoop } = input;
	if (!input.active) return false;
	if (safety === null || !safety.armed) return false;
	if (liveLoop !== null && liveLoop.engaged) return false;
	const outSec = safety.out_ms / 1000;
	if (!Number.isFinite(outSec) || input.startPositionSec >= outSec) return false;
	if (
		!Number.isFinite(input.startPositionSec) ||
		!Number.isFinite(input.startContextTime) ||
		!Number.isFinite(input.tempoRatio) ||
		!Number.isFinite(input.atContextTime)
	) {
		throw new RangeError('playbackReachedSafetyLoopOut requires finite segment times and tempo');
	}
	const elapsed = Math.max(0, input.atContextTime - input.startContextTime);
	const linear = input.startPositionSec + elapsed * input.tempoRatio;
	return linear >= outSec;
}

/** Explicit loop exit (click-out / MIDI loop exit) disarms SAFE without
 * clearing the saved endpoints, so the DJ can re-arm later. */
export function disarmSafetyLoopOnExplicitExit(
	safety: SafetyLoopSnapshot | null
): SafetyLoopSnapshot | null {
	if (safety === null || !safety.armed) return safety;
	return { ...safety, armed: false };
}

export function quantizedPositionMs(
	beats: readonly AnlzBeat[],
	positionMs: number,
	quantizeEnabled: boolean,
	/** The deck's selected quantize grid (pin a67bafbfc4b0); unused when quantizeEnabled is false - callers with nothing to snap to may pass 1. */
	gridBeats: 1 | 4 | 8 = 1
): number {
	if (!Number.isFinite(positionMs) || positionMs < 0) {
		throw new RangeError(`positionMs must be a finite non-negative number, got ${positionMs}`);
	}
	if (!quantizeEnabled) return positionMs;
	return quantizeToNearestGridBeat(beats, positionMs / 1000, gridBeats) * 1000;
}

export function quantizedLoopEndpointsMs(
	beats: readonly AnlzBeat[],
	loop: { in_ms: number; out_ms: number },
	quantizeEnabled: boolean,
	/** The deck's selected quantize grid (pin a67bafbfc4b0). Unused when
	 * quantizeEnabled is false - callers with nothing to snap to may pass 1. */
	gridBeats: 1 | 4 | 8 = 1
): { in_ms: number; out_ms: number } {
	if (
		!Number.isFinite(loop.in_ms) ||
		!Number.isFinite(loop.out_ms) ||
		loop.in_ms < 0 ||
		loop.out_ms <= loop.in_ms
	) {
		throw new RangeError(`loop requires finite 0 <= in_ms < out_ms, got ${loop.in_ms}..${loop.out_ms}`);
	}
	if (!quantizeEnabled) return { ...loop };
	const snapped = {
		in_ms: quantizeToNearestGridBeat(beats, loop.in_ms / 1000, gridBeats) * 1000,
		out_ms: quantizeToNearestGridBeat(beats, loop.out_ms / 1000, gridBeats) * 1000
	};
	if (snapped.out_ms <= snapped.in_ms) {
		throw new RangeError(
			`quantized loop collapsed at ${snapped.in_ms}ms; choose endpoints spanning distinct PQTZ beats`
		);
	}
	return snapped;
}

/** Bound a valid loop to the decoded audio duration. Overshoot is expected
 * for a final PQTZ interval that extends past the decoded buffer boundary;
 * an empty loop remains an explicit error. */
export function loopEndpointsWithinDurationMs(
	loop: { in_ms: number; out_ms: number },
	durationMs: number
): { in_ms: number; out_ms: number } {
	if (!Number.isFinite(durationMs) || durationMs <= 0) {
		throw new RangeError(`decoded duration must be finite and positive, got ${durationMs}`);
	}
	if (
		!Number.isFinite(loop.in_ms) ||
		!Number.isFinite(loop.out_ms) ||
		loop.in_ms < 0 ||
		loop.out_ms <= loop.in_ms
	) {
		throw new RangeError(`loop requires finite 0 <= in_ms < out_ms, got ${loop.in_ms}..${loop.out_ms}`);
	}
	const out_ms = Math.min(loop.out_ms, durationMs);
	if (out_ms <= loop.in_ms) {
		throw new RangeError(
			`loop would be empty at decoded duration ${durationMs}ms, got ${loop.in_ms}..${loop.out_ms}`
		);
	}
	return { in_ms: loop.in_ms, out_ms };
}

/**
 * Whether `beatCount` beats fit from the anchor AND the resulting loop ends
 * at or before the decoded audio duration, without throwing. A grid-only fit
 * check can still land past duration, at which point
 * `loopEndpointsWithinDurationMs` clips the engaged loop shorter than the
 * requested beat count; a render-path gate must use this instead, so a
 * choice is only enabled when engaging it will not silently clip. Mirrors
 * `beatJumpMovesTransportWithinDuration`'s try/catch predicate shape: a
 * render-path gate needs a boolean, not a thrown edge case, while the actual
 * engage command still fails loudly on a bad grid.
 */
export function beatLoopFitsWithinDuration(
	beats: readonly AnlzBeat[],
	positionMs: number,
	beatCount: number,
	durationMs: number,
	startMs?: number
): boolean {
	try {
		const range = exactBeatLoopRangeMs(beats, positionMs, beatCount, startMs);
		return range.out_ms <= durationMs;
	} catch {
		return false;
	}
}

/** Move an engaged, beat-aligned loop by whole real PQTZ beats without
 * changing its beat length. A boundary that cannot retain the whole loop is
 * refused rather than silently exiting or shortening the live loop.
 *
 * A manually-set loop endpoint is not required to sit exactly on a real PQTZ
 * beat (pin 334a50710ef0 defect B) - the DJ may have nudged it a few ms off
 * grid on purpose. The shift is computed on the nearest grid beat so the
 * whole-beat delta still makes sense, but each endpoint's own off-grid offset
 * from that nearest beat is carried forward unchanged rather than discarded,
 * so a manual loop never gets silently snapped to the grid by a beat jump. */
export function shiftLiveBeatLoopRangeMs(
	beats: readonly AnlzBeat[],
	loop: { in_ms: number; out_ms: number },
	deltaBeats: number,
	durationMs: number
): { in_ms: number; out_ms: number } {
	validateBeatGrid(beats);
	if (
		!Number.isFinite(loop.in_ms) ||
		!Number.isFinite(loop.out_ms) ||
		loop.in_ms < 0 ||
		loop.out_ms <= loop.in_ms
	) {
		throw new RangeError(`loop requires finite 0 <= in_ms < out_ms, got ${loop.in_ms}..${loop.out_ms}`);
	}
	if (!Number.isFinite(durationMs) || durationMs <= 0) {
		throw new RangeError(`decoded duration must be finite and positive, got ${durationMs}`);
	}
	if (!Number.isInteger(deltaBeats) || deltaBeats === 0) {
		throw new RangeError(`deltaBeats must be a non-zero integer, got ${deltaBeats}`);
	}
	const inSec = quantizeToNearestBeat(beats, loop.in_ms / 1000);
	const outSec = quantizeToNearestBeat(beats, loop.out_ms / 1000);
	const inIndex = beats.findIndex((beat) => beat.t === inSec);
	const outIndex = beats.findIndex((beat) => beat.t === outSec);
	const beatLength = outIndex - inIndex;
	const nextIn = inIndex + deltaBeats;
	const nextOut = nextIn + beatLength;
	if (inIndex < 0 || beatLength <= 0 || nextIn < 0 || nextOut >= beats.length) {
		throw new RangeError(`shifted live loop does not fit after ${deltaBeats} PQTZ beats`);
	}
	const inOffsetMs = loop.in_ms - inSec * 1000;
	const outOffsetMs = loop.out_ms - outSec * 1000;
	const nextInMs = beats[nextIn].t * 1000 + inOffsetMs;
	const nextOutMs = beats[nextOut].t * 1000 + outOffsetMs;
	// sol-review P1/P2 (334a50710ef0): the beat INDEX check above cannot see
	// a preserved manual offset - a negative inOffsetMs on an early beat can
	// push nextInMs below zero, and sufficiently different in/out offsets can
	// make the range empty or reversed, even though nextIn/nextOut are valid
	// indices. Refuse the whole shift rather than install it, matching this
	// function's existing policy above of refusing a boundary it cannot keep
	// whole instead of silently clamping or exiting.
	if (nextInMs < 0 || nextOutMs <= nextInMs) {
		throw new RangeError(
			`shifted live loop with preserved offsets ${nextInMs}..${nextOutMs}ms is empty, reversed, or negative`
		);
	}
	if (nextOutMs > durationMs) {
		throw new RangeError(`shifted live loop exceeds decoded duration ${durationMs}ms`);
	}
	return { in_ms: nextInMs, out_ms: nextOutMs };
}

/** An engaged loop's out point is exclusive and its in point is inclusive.
 * A beat jump that lands at or past out, or strictly before in, must be
 * pulled back inside, otherwise quantized seek exits the loop by contract.
 * All other targets retain their exact grid position.
 *
 * `beatJumpTargetMs` always returns a grid-EXACT time, but a preserved
 * manual loop endpoint can be off-grid (pin 334a50710ef0 defect B). An
 * exact-equality check against `loop.out_ms` can therefore never fire for
 * an off-grid out - defect A resurfacing for exactly the loops defect B
 * exists to preserve - so both boundaries are compared with < / >=
 * against the loop's real (possibly off-grid) ms values, not matched to a
 * specific beat index. */
export function targetWithinShiftedLiveLoopMs(
	beats: readonly AnlzBeat[],
	targetMs: number,
	loop: { in_ms: number; out_ms: number }
): number {
	validateBeatGrid(beats);
	if (!Number.isFinite(targetMs) || targetMs < 0) {
		throw new RangeError(`targetMs must be finite and non-negative, got ${targetMs}`);
	}
	if (targetMs >= loop.in_ms && targetMs < loop.out_ms) return targetMs;
	if (targetMs < loop.in_ms) {
		const followingBeat = beats.find((beat) => beat.t * 1000 >= loop.in_ms);
		const followingMs = followingBeat === undefined ? null : followingBeat.t * 1000;
		if (followingMs === null || followingMs >= loop.out_ms) {
			throw new RangeError(`shifted live loop in ${loop.in_ms}ms has no PQTZ beat before its exclusive out`);
		}
		return followingMs;
	}
	let precedingMs: number | null = null;
	for (const beat of beats) {
		if (beat.t * 1000 >= loop.out_ms) break;
		precedingMs = beat.t * 1000;
	}
	if (precedingMs === null || precedingMs < loop.in_ms) {
		throw new RangeError(`shifted live loop has no PQTZ beat before exclusive out ${loop.out_ms}ms`);
	}
	return precedingMs;
}

/** Whether a quantized seek target must exit an engaged loop. Exclusive out:
 * a target landing at or past the loop's out boundary always exits, matching
 * real Rekordbox behaviour for an ordinary manual seek that snaps outside a
 * loop. A beat-jump loop shift keeps this from firing by having its caller
 * skip the deck's coarser grid re-snap upstream (`quantizedSeek`'s
 * `skipGridQuantize`), so `targetMs` here is already the exact, loop-safe
 * beat and never arrives re-snapped onto that boundary (pin 334a50710ef0
 * defect A). */
export function loopExitOnSeekMs(
	targetMs: number,
	loop: { in_ms: number; out_ms: number; engaged: boolean } | null
): boolean {
	return loop !== null && loop.engaged && (targetMs < loop.in_ms || targetMs >= loop.out_ms);
}

/** True when `positionSec` lies within one beat interval of the grid: from a
 * beat before the first beat to a beat after the last, each edge measured by
 * its own outermost interval. A grid of fewer than two beats has no interval,
 * so it keeps the old behaviour (always snaps). */
export function seekTargetWithinGridSpan(beats: readonly AnlzBeat[], positionSec: number): boolean {
	if (beats.length < 2) return true;
	const first = beats[0].t;
	const last = beats[beats.length - 1].t;
	const before = first - (beats[1].t - first);
	const after = last + (last - beats[beats.length - 2].t);
	return positionSec >= before && positionSec <= after;
}

/** `quantizedSeek`'s whole target-and-exit decision in one call, so the
 * production seek path and a direct test call are the same code (pin
 * 334a50710ef0 defect A). `beats` is null exactly when `quantizedSeek` has
 * nothing to snap to (quantize off or a gridless deck) - `ms` passes
 * through unchanged. `skipGridQuantize` is the beat-jump loop-shift escape:
 * true bypasses the deck's own coarser 1/4/8-beat re-snap entirely, so an
 * already loop-safe exact beat can never be pushed back onto the loop's
 * exclusive out boundary and disengage it underneath the jump.
 *
 * A target more than one beat outside the grid's span is NOT snapped (#5601):
 * there is no beat there to quantize to. Snapping it to the nearest beat put a
 * 156 s seek on the last beat of a grid that stopped at 36 s while the audio
 * ran to 180 s, every retry. `phaseKeepingLandingSec` already treats off-grid
 * targets the same way. */
export function quantizedSeekDecisionMs(
	beats: readonly AnlzBeat[] | null,
	ms: number,
	gridBeats: 1 | 4 | 8,
	skipGridQuantize: boolean,
	loop: { in_ms: number; out_ms: number; engaged: boolean } | null
): { targetMs: number; exitLoop: boolean } {
	const snap = beats !== null && !skipGridQuantize && seekTargetWithinGridSpan(beats, ms / 1000);
	const targetMs = snap ? quantizeToNearestGridBeat(beats, ms / 1000, gridBeats) * 1000 : ms;
	return { targetMs, exitLoop: loopExitOnSeekMs(targetMs, loop) };
}

/** The latest real PQTZ downbeat (bar 1, `n === 1`) at or before `positionMs`,
 * never the upcoming one. A fresh loop engaged with no explicit anchor should
 * feel forgiving about exact click timing: if the DJ's click landed slightly
 * late relative to the bar, quantizing forward to the NEXT downbeat would
 * silently skip a whole bar, which reads as far more wrong than starting a
 * beat or two behind where they meant to loop from. Matching the preceding
 * beat is the safer assumption. Only ever used for a fresh four-beat loop
 * with no explicit start_ms - every other loop length or an explicit anchor
 * keeps the existing nearest-beat behaviour. */
export function precedingDownbeatMs(beats: readonly AnlzBeat[], positionMs: number): number {
	validateBeatGrid(beats);
	if (!Number.isFinite(positionMs) || positionMs < 0) {
		throw new RangeError(`positionMs must be finite and non-negative, got ${positionMs}`);
	}
	const positionSec = positionMs / 1000;
	let candidate: AnlzBeat | null = null;
	for (const beat of beats) {
		if (beat.t > positionSec) break;
		if (beat.n === 1) candidate = beat;
	}
	if (candidate === null) {
		throw new RangeError(`no PQTZ downbeat (n===1) at or before ${positionMs}ms`);
	}
	return candidate.t * 1000;
}

/** Resize an engaged, beat-aligned loop to `beatCount` beats, anchored so
 * one side stays fixed (or both move together for `center`):
 *   - 'start': loop-in fixed, loop-out moves (current halve/double behaviour).
 *   - 'end': loop-out fixed, loop-in moves.
 *   - 'center': loop-out and loop-in move by equal, opposite PQTZ beat
 *     counts, like a Photoshop centered transform.
 * All three land on real PQTZ beat timestamps or throw - a centered resize
 * whose beat delta cannot be split evenly across both sides is refused
 * rather than guessed at, since sub-beat loops are not supported. */
export function resizedLoopRangeMs(
	beats: readonly AnlzBeat[],
	loop: { in_ms: number; out_ms: number },
	beatCount: number,
	anchor: 'start' | 'end' | 'center',
	durationMs: number
): { in_ms: number; out_ms: number } {
	validateBeatGrid(beats);
	if (!Number.isInteger(beatCount) || beatCount <= 0) {
		throw new RangeError(`beatCount must be a positive integer, got ${beatCount}`);
	}
	if (!Number.isFinite(durationMs) || durationMs <= 0) {
		throw new RangeError(`decoded duration must be finite and positive, got ${durationMs}`);
	}
	if (
		!Number.isFinite(loop.in_ms) ||
		!Number.isFinite(loop.out_ms) ||
		loop.in_ms < 0 ||
		loop.out_ms <= loop.in_ms
	) {
		throw new RangeError(`loop requires finite 0 <= in_ms < out_ms, got ${loop.in_ms}..${loop.out_ms}`);
	}
	const inSec = quantizeToNearestBeat(beats, loop.in_ms / 1000);
	const outSec = quantizeToNearestBeat(beats, loop.out_ms / 1000);
	const inIndex = beats.findIndex((beat) => beat.t === inSec);
	const outIndex = beats.findIndex((beat) => beat.t === outSec);
	if (inIndex < 0 || outIndex <= inIndex) {
		throw new RangeError(`loop ${loop.in_ms}..${loop.out_ms}ms does not align to real PQTZ beats`);
	}
	let newIn: number;
	let newOut: number;
	if (anchor === 'start') {
		newIn = inIndex;
		newOut = inIndex + beatCount;
	} else if (anchor === 'end') {
		newIn = outIndex - beatCount;
		newOut = outIndex;
	} else {
		const currentLength = outIndex - inIndex;
		const delta = beatCount - currentLength;
		if (delta % 2 !== 0) {
			throw new RangeError(
				`centered resize from ${currentLength} to ${beatCount} beats is not evenly splittable across both sides`
			);
		}
		const half = delta / 2;
		newIn = inIndex - half;
		newOut = outIndex + half;
	}
	if (newIn < 0 || newOut <= newIn || newOut >= beats.length) {
		throw new RangeError(
			`resized loop (${anchor}) to ${beatCount} beats does not fit on the grid from ${loop.in_ms}..${loop.out_ms}ms`
		);
	}
	if (beats[newOut].t * 1000 > durationMs) {
		throw new RangeError(`resized loop exceeds decoded duration ${durationMs}ms`);
	}
	return { in_ms: beats[newIn].t * 1000, out_ms: beats[newOut].t * 1000 };
}

export function exactBeatLoopRangeMs(
	beats: readonly AnlzBeat[],
	positionMs: number,
	beatCount: number,
	startMs?: number
): { in_ms: number; out_ms: number } {
	if (!Number.isFinite(beatCount) || beatCount <= 0) {
		throw new RangeError(`beatCount must be finite and positive, got ${beatCount}`);
	}
	const anchorMs = startMs ?? positionMs;
	if (!Number.isFinite(anchorMs) || anchorMs < 0) {
		throw new RangeError(`loop anchor must be a finite non-negative number, got ${anchorMs}`);
	}
	const startSec = quantizeToNearestBeat(beats, anchorMs / 1000);
	const startIndex = beats.findIndex((beat) => beat.t === startSec);
	const endIndex = startIndex + beatCount;
	if (startIndex < 0 || Math.ceil(endIndex) >= beats.length) {
		throw new RangeError(
			`${beatCount} PQTZ beats do not fit from loop anchor ${anchorMs}ms`
		);
	}
	// Fractions interpolate the measured adjacent PQTZ interval, never nominal
	// BPM. Whole-beat endpoints remain the exact recorded timestamps.
	const whole = Math.floor(endIndex);
	const fraction = endIndex - whole;
	const endSec = fraction === 0 ? beats[whole].t
		: beats[whole].t + fraction * (beats[whole + 1].t - beats[whole].t);
	return { in_ms: startSec * 1000, out_ms: endSec * 1000 };
}
