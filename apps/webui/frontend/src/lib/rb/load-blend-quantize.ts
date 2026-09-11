/**
 * Bar-1 of the current/next 4-bar phrase for Load-to-CH blend release (issue #286).
 *
 * Independent per deck. Does not fold tempo or plan a follower sync.
 */
import type { AnlzBeat } from '$lib/rb/anlz-types';
import {
	nextDownbeatAtOrAfter,
	quantizeToNearestDownbeat,
	validateBeatGrid
} from '$lib/rb/beat-sync-math';

export function phraseBar1TargetMs(input: {
	positionMs: number;
	beats: readonly AnlzBeat[] | null;
	phrases: ReadonlyArray<{ start_ms: number; end_ms: number }>;
}): number | null {
	if (!Number.isFinite(input.positionMs) || input.positionMs < 0) return null;
	const gridOk = beatGridOk(input.beats);
	if (input.phrases.length > 0) {
		const startMs = phraseTargetStartMs(input.positionMs, input.phrases);
		if (!gridOk || input.beats === null) return startMs;
		return snapDownbeatMs(input.beats, startMs);
	}
	if (gridOk && input.beats !== null) {
		const fourBar = fourBarStartsSec(input.beats);
		if (fourBar.length > 0) {
			return pickAtOrAfterOrNearestMs(fourBar, input.positionMs);
		}
		return nextDownbeatAtOrAfter(input.beats, input.positionMs / 1000) * 1000;
	}
	return null;
}

function beatGridOk(beats: readonly AnlzBeat[] | null): beats is readonly AnlzBeat[] {
	if (beats === null) return false;
	try {
		validateBeatGrid(beats);
		return true;
	} catch {
		return false;
	}
}

function phraseTargetStartMs(
	positionMs: number,
	phrases: ReadonlyArray<{ start_ms: number; end_ms: number }>
): number {
	const ordered = phrases.slice().sort((a, b) => a.start_ms - b.start_ms);
	const next = ordered.find((phrase) => phrase.start_ms >= positionMs);
	if (next !== undefined) return next.start_ms;
	const containing = ordered.find(
		(phrase) => phrase.start_ms <= positionMs && positionMs < phrase.end_ms
	);
	if (containing !== undefined) return containing.start_ms;
	return ordered[ordered.length - 1].start_ms;
}

function snapDownbeatMs(beats: readonly AnlzBeat[], positionMs: number): number {
	try {
		return quantizeToNearestDownbeat(beats, positionMs / 1000) * 1000;
	} catch {
		return positionMs;
	}
}

/** Downbeats whose index in the n===1 list is a multiple of 4 (bar 1 of a 4-bar block). */
function fourBarStartsSec(beats: readonly AnlzBeat[]): number[] {
	const downbeats = beats.filter((beat) => beat.n === 1);
	if (downbeats.length < 4) return [];
	return downbeats.filter((_beat, index) => index % 4 === 0).map((beat) => beat.t);
}

function pickAtOrAfterOrNearestMs(startsSec: readonly number[], positionMs: number): number {
	const positionSec = positionMs / 1000;
	const next = startsSec.find((t) => t >= positionSec);
	if (next !== undefined) return next * 1000;
	return nearestEarlierTieSec(startsSec, positionSec) * 1000;
}

function nearestEarlierTieSec(timesSec: readonly number[], positionSec: number): number {
	let best = timesSec[0];
	let bestDist = Math.abs(positionSec - best);
	for (const t of timesSec) {
		const dist = Math.abs(positionSec - t);
		if (dist < bestDist || (dist === bestDist && t < best)) {
			best = t;
			bestDist = dist;
		}
	}
	return best;
}
