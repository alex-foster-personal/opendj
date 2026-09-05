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

import { quantizeToNearestBeat, validateBeatGrid } from '$lib/rb/beat-sync-math';
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

/** A natural-end safety re-entry is permitted only when it can be rebuilt as
 * an exact PQTZ beat loop. A manual, incomplete, or duration-overrunning slot
 * deliberately falls through to the ordinary natural-end stop. */
export function phaseLockedSafetyLoopAtTrackEnd(
	beats: readonly AnlzBeat[],
	safety: SafetyLoopSnapshot,
	durationMs: number
): LoopSnapshot | null {
	if (safety.beat_length === null) return null;
	if (!Number.isInteger(safety.beat_length) || safety.beat_length <= 0) return null;
	try {
		const range = exactBeatLoopRangeMs(beats, safety.in_ms, safety.beat_length, safety.in_ms);
		if (range.out_ms > durationMs) return null;
		return { ...range, engaged: true, beat_length: safety.beat_length };
	} catch {
		return null;
	}
}

export function quantizedLoopEndpointsMs(
	beats: readonly AnlzBeat[],
	loop: { in_ms: number; out_ms: number },
	quantizeEnabled: boolean
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
		in_ms: quantizeToNearestBeat(beats, loop.in_ms / 1000) * 1000,
		out_ms: quantizeToNearestBeat(beats, loop.out_ms / 1000) * 1000
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
 * refused rather than silently exiting or shortening the live loop. */
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
	if (beats[nextOut].t * 1000 > durationMs) {
		throw new RangeError(`shifted live loop exceeds decoded duration ${durationMs}ms`);
	}
	return { in_ms: beats[nextIn].t * 1000, out_ms: beats[nextOut].t * 1000 };
}

/** An engaged loop's out point is exclusive. A beat jump that lands exactly
 * there must use the preceding real PQTZ beat, otherwise quantized seek exits
 * the loop by contract. All other targets retain their exact grid position. */
export function targetWithinShiftedLiveLoopMs(
	beats: readonly AnlzBeat[],
	targetMs: number,
	loop: { in_ms: number; out_ms: number }
): number {
	validateBeatGrid(beats);
	if (!Number.isFinite(targetMs) || targetMs < 0) {
		throw new RangeError(`targetMs must be finite and non-negative, got ${targetMs}`);
	}
	if (targetMs !== loop.out_ms) return targetMs;
	const outIndex = beats.findIndex((beat) => beat.t * 1000 === loop.out_ms);
	if (outIndex < 1) {
		throw new RangeError(`shifted live loop out ${loop.out_ms}ms is not a movable PQTZ beat`);
	}
	const inLoopTargetMs = beats[outIndex - 1].t * 1000;
	if (inLoopTargetMs < loop.in_ms) {
		throw new RangeError(`shifted live loop has no PQTZ beat before exclusive out ${loop.out_ms}ms`);
	}
	return inLoopTargetMs;
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
	if (!Number.isInteger(beatCount) || beatCount <= 0) {
		throw new RangeError(`beatCount must be a positive integer, got ${beatCount}`);
	}
	const anchorMs = startMs ?? positionMs;
	if (!Number.isFinite(anchorMs) || anchorMs < 0) {
		throw new RangeError(`loop anchor must be a finite non-negative number, got ${anchorMs}`);
	}
	const startSec = quantizeToNearestBeat(beats, anchorMs / 1000);
	const startIndex = beats.findIndex((beat) => beat.t === startSec);
	const endIndex = startIndex + beatCount;
	if (startIndex < 0 || endIndex >= beats.length) {
		throw new RangeError(
			`${beatCount} PQTZ beats do not fit from loop anchor ${anchorMs}ms`
		);
	}
	return { in_ms: startSec * 1000, out_ms: beats[endIndex].t * 1000 };
}
