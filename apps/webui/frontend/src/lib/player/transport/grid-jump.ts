/**
 * Snapped deck jumps (DECKUX-6794): Cmd/Ctrl+Left and Right land on a FIXED bar
 * grid instead of counting from wherever the playhead has drifted to, so
 * Right then Left replays the same passage.
 *
 * The grid is the Cmd K preview's ((private path)): bar 1,
 * then every `stepBars` bars, plus a stop at any section start a step would
 * pass over. After a section stop the count restarts from that section.
 *
 * - Right goes to the first landing point after the current bar.
 * - Left goes back to the start of the block the playhead is in, which replays
 *   it. In the first bar of a block it goes one block further back, so Right
 *   then Left returns to the block you left.
 *
 * Pure: the engine turns the plan into a whole-beat `beatJump`, so loops,
 * phase keeping, duration clamps and Beat Sync followers behave exactly as
 * they do for the on-screen jump buttons. A PLAYING deck keeps its position
 * inside its bar (so a synced deck stays downbeat on downbeat with the
 * master); a paused deck lands on the target bar's downbeat.
 *
 * Bars are counted on the grid's real downbeats (`n === 1`), never by BPM, so
 * a drifting grid still lands on its own downbeats.
 */

import type { AnlzBeat } from '$lib/rb/anlz-types';
import { gridBeatPosition, validateBeatGrid } from '$lib/rb/beat-sync-math';

export type GridJumpDirection = 1 | -1;

export type GridJumpPlan =
	| {
			kind: 'jump';
			/** Whole grid beats to hand to `beatJump`; never 0. */
			deltaBeats: number;
			/** 1-based bar number the jump lands in. */
			targetBar: number;
			/** True when the landing point is a section start rather than a grid step. */
			sectionStart: boolean;
	  }
	/** Before the first beat, Right: an exact seek to bar 1's downbeat (no beat count to jump by). */
	| { kind: 'seek'; targetMs: number; targetBar: 1 }
	| { kind: 'refused'; reason: string };

export interface GridJumpInput {
	beats: readonly AnlzBeat[];
	/** Section start times in seconds (phrase starts); may be empty. */
	sectionStartsSec: readonly number[];
	/** The deck's anchor: its projected playhead, in ms. */
	anchorMs: number;
	direction: GridJumpDirection;
	/** Block length in bars: 4, or 8 with Shift. */
	stepBars: number;
	/** Transport running or scheduled to: keep the position inside the bar. */
	playing: boolean;
}

/** Beat indices of the grid's downbeats, in order. */
export function downbeatIndices(beats: readonly AnlzBeat[]): number[] {
	const out: number[] = [];
	for (let i = 0; i < beats.length; i++) if (beats[i].n === 1) out.push(i);
	return out;
}

function nearestIndex(times: readonly number[], t: number): number {
	let lo = 0;
	let hi = times.length - 1;
	while (lo < hi) {
		const mid = (lo + hi) >> 1;
		if (times[mid] < t) lo = mid + 1;
		else hi = mid;
	}
	if (lo > 0 && t - times[lo - 1] <= times[lo] - t) return lo - 1;
	return lo;
}

/**
 * Landing points as 0-based bar indices: bar 0, then every `stepBars`, with a
 * stop at each section start a step would cross (the Cmd K `grid()` rule).
 */
export function gridJumpPoints(
	barCount: number,
	sectionBars: readonly number[],
	stepBars: number
): number[] {
	if (!Number.isInteger(stepBars) || stepBars < 1)
		throw new RangeError(`stepBars must be a positive integer; got ${stepBars}`);
	const points = [0];
	let at = 0;
	while (at < barCount - 1) {
		const to = Math.min(barCount - 1, at + stepBars);
		let hit: number | null = null;
		for (const s of sectionBars) if (s > at && s < to && (hit === null || s < hit)) hit = s;
		at = hit ?? to;
		points.push(at);
	}
	return points;
}

export function planGridJump(input: GridJumpInput): GridJumpPlan {
	const { beats, anchorMs, direction, stepBars, playing } = input;
	validateBeatGrid(beats);
	if (direction !== 1 && direction !== -1)
		throw new RangeError(`direction must be 1 or -1; got ${direction}`);
	if (!Number.isFinite(anchorMs) || anchorMs < 0)
		throw new RangeError(`anchorMs must be finite and >= 0; got ${anchorMs}`);
	const downbeats = downbeatIndices(beats);
	if (downbeats.length < 2)
		return { kind: 'refused', reason: 'track has no bar grid - snapped jump unavailable' };
	const anchorSec = anchorMs / 1000;
	const position = gridBeatPosition(beats, anchorSec);
	if (position === null && anchorSec < beats[0].t && direction === 1) {
		// A lead-in before the first beat: Right enters bar 1 from outside the
		// grid, which a whole-beat jump cannot express, so it is an exact seek.
		return { kind: 'seek', targetMs: beats[downbeats[0]].t * 1000, targetBar: 1 };
	}
	if (position === null)
		return {
			kind: 'refused',
			reason: 'playhead is outside the beatgrid - snapped jump unavailable'
		};

	const times = beats.map((b) => b.t);
	// The beat the move counts from: a playing deck keeps its phase from the
	// beat it is inside; a paused deck snaps to its nearest beat first.
	const anchorBeat = playing ? Math.floor(position) : nearestIndex(times, anchorSec);
	// 0-based bar the anchor beat sits in; -1 before the first downbeat.
	let bar = -1;
	for (let i = 0; i < downbeats.length && downbeats[i] <= anchorBeat; i++) bar = i;

	const downbeatTimes = downbeats.map((i) => times[i]);
	const sectionBars = [
		...new Set(
			input.sectionStartsSec.filter(Number.isFinite).map((s) => nearestIndex(downbeatTimes, s))
		)
	];
	const points = gridJumpPoints(downbeats.length, sectionBars, stepBars);

	let target: number | null;
	if (direction === 1) {
		target = points.find((p) => p > bar) ?? null;
		if (target === null) return { kind: 'refused', reason: 'last block - nothing further ahead' };
	} else if (bar < 0) {
		target = 0;
	} else {
		let cellIndex = 0;
		for (let i = 0; i < points.length; i++) if (points[i] <= bar) cellIndex = i;
		target = points[cellIndex] === bar ? points[Math.max(0, cellIndex - 1)] : points[cellIndex];
	}

	// Playing: move whole bars, so the beat-in-bar and its phase carry over.
	// Paused: land on the target downbeat itself.
	const from = playing && bar >= 0 ? downbeats[bar] : anchorBeat;
	const deltaBeats = downbeats[target] - from;
	if (deltaBeats === 0) return { kind: 'refused', reason: `already at bar ${target + 1}` };
	return {
		kind: 'jump',
		deltaBeats,
		targetBar: target + 1,
		sectionStart: sectionBars.includes(target)
	};
}
