/**
 * Beat-stamp encode/decode for RESCUE-01 on-disk snapshots.
 * Pure math - no hot-cue-label or wave-math imports.
 */

import type { AnlzBeat } from './anlz-types';

export type RescueBeatStamp =
	| {
			kind: 'beatgrid';
			beat_index: number;
			beat_n: number;
			phase: number;
	  }
	| {
			kind: 'sample';
			position_ms: number;
	  };

function beatCoordinate(beats: readonly Pick<AnlzBeat, 't'>[], time: number): number | null {
	if (beats.length < 2 || time < beats[0].t || time > beats[beats.length - 1].t) return null;
	let low = 0;
	let high = beats.length - 1;
	while (low < high) {
		const middle = (low + high) >>> 1;
		if (beats[middle].t < time) low = middle + 1;
		else high = middle;
	}
	if (beats[low].t === time) return low;
	const previous = low - 1;
	const interval = beats[low].t - beats[previous].t;
	return interval > 0 ? previous + (time - beats[previous].t) / interval : null;
}

function enclosingBeatPhase(
	beats: readonly Pick<AnlzBeat, 'n' | 't'>[],
	positionSec: number
): { beat_n: number; phase: number } | null {
	if (beats.length < 2 || !Number.isFinite(positionSec) || positionSec < 0) return null;
	let low = 0;
	let high = beats.length - 1;
	while (low < high) {
		const middle = (low + high) >>> 1;
		if (beats[middle].t < positionSec) low = middle + 1;
		else high = middle;
	}
	let i = low;
	if (i > 0 && beats[i].t > positionSec) i -= 1;
	if (i >= beats.length - 1) return null;
	const a = beats[i];
	const b = beats[i + 1];
	const dur = b.t - a.t;
	if (!(dur > 0)) return null;
	const phase = (positionSec - a.t) / dur;
	if (!Number.isFinite(phase)) return null;
	return { beat_n: a.n, phase: Math.min(1, Math.max(0, phase)) };
}

export function encodeBeatStamp(
	beats: readonly Pick<AnlzBeat, 'n' | 't'>[],
	positionMs: number
): RescueBeatStamp {
	const positionSec = positionMs / 1000;
	const beatIndex = beatCoordinate(beats, positionSec);
	const phase = enclosingBeatPhase(beats, positionSec);
	if (beatIndex === null || phase === null) {
		return { kind: 'sample', position_ms: Math.round(positionMs) };
	}
	return {
		kind: 'beatgrid',
		beat_index: beatIndex,
		beat_n: phase.beat_n,
		phase: phase.phase
	};
}

export function decodeBeatStamp(
	stamp: RescueBeatStamp,
	beats: readonly Pick<AnlzBeat, 't'>[]
): number | null {
	if (stamp.kind === 'sample') return stamp.position_ms;
	if (beats.length < 2) return null;
	const index = stamp.beat_index;
	if (!Number.isFinite(index) || index < 0 || index >= beats.length - 1) return null;
	const low = Math.floor(index);
	const high = low + 1;
	const interval = beats[high].t - beats[low].t;
	if (!(interval > 0)) return null;
	const fraction = index - low;
	const positionSec = beats[low].t + fraction * interval;
	return Math.round(positionSec * 1000);
}
