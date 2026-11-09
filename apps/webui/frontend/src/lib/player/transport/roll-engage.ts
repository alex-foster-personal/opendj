/**
 * FXROLL (#9841): roll engage planner.
 *
 * Quantize on: the roll starts on the first slot of min(beats, 1) beats at or
 * after the earliest safe schedule instant (now plus the transport lead), with
 * the playhead at loop start, so the first repeat is a whole slice on the grid.
 * Quantize off: today's phase-folded engage at the next safe slot.
 *
 * Pure math only: no DOM, no AudioContext, no engine imports.
 */

import type { AnlzBeat } from '$lib/rb/anlz-types';
import { validateBeatGrid } from '$lib/rb/beat-sync-math';
import { rollLoopRangeMs } from '$lib/player/transport/loops';

export const ROLL_ENGAGE = 'roll engage';
export const ROLL_BEAT_LENGTHS = [1 / 32, 1 / 16, 1 / 8, 1 / 4, 1 / 2, 1, 2, 4, 8, 16, 32] as const;

type LoopRangeMs = { in_ms: number; out_ms: number };

export type RollEngagePlan =
	| {
			kind: 'quantized';
			engageAtContextSec: number;
			playheadSec: number;
			loop: LoopRangeMs;
			slotSec: number;
	  }
	| { kind: 'phase_folded'; loop: LoopRangeMs };

export interface RollEngageInput {
	quantize: boolean;
	beats: readonly AnlzBeat[];
	rollBeats: number;
	earliestWhenContextSec: number;
	positionAtEarliestSec: number;
	tempoRatio: number;
	currentPositionMs: number;
	engagedLoop: LoopRangeMs | null;
}

const EPS = 1e-9;

//----- slot search

/** First slot of min(rollBeats, 1) beats at or after `fromSec`, or null past the last beat. */
export function nextRollSlotSec(
	beats: readonly AnlzBeat[],
	fromSec: number,
	rollBeats: number
): number | null {
	const slotBeats = Math.min(rollBeats, 1);
	const perBeat = Math.round(1 / slotBeats);
	for (let k = 0; k < beats.length - 1; k++) {
		const width = (beats[k + 1].t - beats[k].t) / perBeat;
		for (let j = 0; j < perBeat; j++) {
			const slot = beats[k].t + j * width;
			if (slot >= fromSec - EPS) return slot;
		}
	}
	return null;
}

function _requireRollLength(rollBeats: number): void {
	if (!(ROLL_BEAT_LENGTHS as readonly number[]).includes(rollBeats)) {
		throw new RangeError(
			`${ROLL_ENGAGE}: rollBeats must be one of 1/32..32 powers of two, got ${rollBeats}`
		);
	}
}

function _requireFinite(name: string, value: number, positive: boolean): void {
	if (!Number.isFinite(value) || (positive ? value <= 0 : value < 0)) {
		throw new RangeError(
			`${ROLL_ENGAGE}: ${name} must be finite and ${positive ? 'positive' : 'non-negative'}, got ${value}`
		);
	}
}

//----- planner

export function planRollEngage(input: RollEngageInput): RollEngagePlan {
	validateBeatGrid(input.beats);
	_requireRollLength(input.rollBeats);
	if (input.quantize === false) {
		return {
			kind: 'phase_folded',
			loop: rollLoopRangeMs(input.beats, input.currentPositionMs, input.rollBeats)
		};
	} else if (input.quantize === true) {
		_requireFinite('earliestWhenContextSec', input.earliestWhenContextSec, false);
		_requireFinite('positionAtEarliestSec', input.positionAtEarliestSec, false);
		_requireFinite('tempoRatio', input.tempoRatio, true);
		const p0 = input.positionAtEarliestSec;
		let slot = nextRollSlotSec(input.beats, p0, input.rollBeats);
		let trackSecToSlot: number;
		const loop = input.engagedLoop;
		if (loop !== null && (slot === null || slot >= loop.out_ms / 1000 - EPS)) {
			const inSec = loop.in_ms / 1000;
			const outSec = loop.out_ms / 1000;
			const wrapped = nextRollSlotSec(input.beats, inSec, input.rollBeats);
			if (wrapped === null || wrapped >= outSec - EPS) {
				throw new RangeError(
					`${ROLL_ENGAGE}: no ${input.rollBeats}-beat slot inside engaged loop ${loop.in_ms}..${loop.out_ms}ms`
				);
			}
			slot = wrapped;
			trackSecToSlot = outSec - p0 + (wrapped - inSec);
		} else if (slot === null) {
			throw new RangeError(`${ROLL_ENGAGE}: no grid slot at or after ${p0}s (track end)`);
		} else {
			trackSecToSlot = Math.max(0, slot - p0);
		}
		const range = rollLoopRangeMs(input.beats, slot * 1000, input.rollBeats);
		if (Math.abs(range.in_ms / 1000 - slot) > EPS) {
			throw new Error(`${ROLL_ENGAGE}: loop start ${range.in_ms}ms disagrees with slot ${slot}s`);
		}
		return {
			kind: 'quantized',
			engageAtContextSec: input.earliestWhenContextSec + trackSecToSlot / input.tempoRatio,
			playheadSec: slot,
			loop: range,
			slotSec: slot
		};
	}
	throw new TypeError(`${ROLL_ENGAGE}: quantize must be boolean, got ${String(input.quantize)}`);
}
