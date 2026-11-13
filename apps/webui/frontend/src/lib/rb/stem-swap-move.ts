/**
 * Stem swap planner (#10329, #14553): hand one stem from deck A to deck B as
 * two stem_gain ramps on the master's next downbeat, over whole bars.
 *
 * Pure: it reads the two decks' stem-set state it is given and returns the
 * ramp orders the existing agent ramp executor runs, or a named refusal.
 */
import type { Duration } from './agent-duration';
import { STEM_LAYOUT_CONTROLS } from './stem-graph';
import type { StemControl, StemLayout } from './stem-types';

/** Near-hard swap length for `bars: 0`, the research spec default (mimicked). */
export const SWAP_RAMP_MS = 30;
/** Unity on the 0..1 stem gain knob. */
export const STEM_UNITY_GAIN = 0.5;

export interface StemSwapDeckState {
	layout: StemLayout | null;
	stems_ready: boolean;
	playing: boolean;
}

export interface StemSwapRequest {
	stem: StemControl;
	from: number;
	to: number;
	bars?: number;
	fromDeck: StemSwapDeckState;
	toDeck: StemSwapDeckState;
}

export interface StemSwapRamp {
	command: { type: 'stem_gain'; deck: number; stem: StemControl; value: number };
	to: number;
	over: Duration;
}

export type StemSwapPlan =
	{ ok: true; ramps: [StemSwapRamp, StemSwapRamp] } | { ok: false; reason: string };

/** Wall time of a swap over `bars` 4-beat bars at `bpm` (bars 0 is the near-hard ramp). */
export function stemSwapDurationMs(bars: number, bpm: number): number {
	if (!(bpm > 0) || !Number.isFinite(bpm))
		throw new RangeError('bpm must be a positive finite number');
	return bars === 0 ? SWAP_RAMP_MS : (bars * 4 * 60_000) / bpm;
}

function hasControl(deck: StemSwapDeckState, stem: StemControl): boolean {
	return deck.layout !== null && STEM_LAYOUT_CONTROLS[deck.layout].includes(stem);
}

/** Plan a stem swap as two stem_gain ramps, or refuse by name. */
export function stemSwapPlan(request: StemSwapRequest): StemSwapPlan {
	const { stem, from, to, fromDeck, toDeck } = request;
	const bars = request.bars ?? 1;
	if (!Number.isInteger(bars) || bars < 0)
		throw new RangeError('stem swap bars must be a whole number >= 0');
	if (from === to) return { ok: false, reason: 'same_deck' };
	if (!fromDeck.stems_ready || !toDeck.stems_ready) return { ok: false, reason: 'stems_not_ready' };
	for (const [deck, state] of [
		[from, fromDeck],
		[to, toDeck]
	] as const) {
		if (!hasControl(state, stem))
			return { ok: false, reason: `deck ${deck} has no ${stem} control` };
	}
	if (!toDeck.playing) return { ok: false, reason: 'to_deck_not_playing' };
	const over: Duration =
		bars === 0
			? { unit: 'ms', n: SWAP_RAMP_MS }
			: { unit: 'bars', n: bars, anchor: 'next_downbeat', clock: 'master' };
	const ramp = (deck: number, target: number): StemSwapRamp => ({
		command: { type: 'stem_gain', deck, stem, value: target },
		to: target,
		over: { ...over }
	});
	return { ok: true, ramps: [ramp(from, 0), ramp(to, STEM_UNITY_GAIN)] };
}
