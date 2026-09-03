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

import { quantizeToNearestBeat } from '$lib/rb/beat-sync-math';
import type { AnlzBeat } from '$lib/rb/anlz-types';

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
