import { PITCH_RANGES, type DeckId, type PitchRange } from '$lib/player/constants';
import type { TempoWrite } from './tempo-edit';

const FINE_STEP_PCT = 0.1;
const COARSE_STEP_PCT = 1.0;

export type TempoKeyAction =
	| { kind: 'tempo-step'; direction: 1 | -1; coarse: boolean }
	| { kind: 'tempo-reset' }
	| { kind: 'pitch-range-cycle' };

export interface TempoKeyDeck {
	deck: DeckId;
	loaded: boolean;
	pitch: number;
	range: PitchRange;
}

/** Percent off the original tempo for a ratio, float noise trimmed (1e-6). */
export function tempoPctFromRatio(ratio: number): number {
	const pct = (ratio - 1) * 100;
	return Math.round(pct * 1_000_000) / 1_000_000;
}

/** Next tempo percent for one key step, clamped to ±range; null when the clamp leaves it
 *  where it is (so a held or repeated key at the limit sends nothing). */
export function nextTempoPct(
	currentPct: number,
	direction: 1 | -1,
	coarse: boolean,
	rangePct: number
): number | null {
	const step = coarse ? COARSE_STEP_PCT : FINE_STEP_PCT;
	const raw = Math.round((currentPct + direction * step) * 1000) / 1000;
	const clamped = Math.max(-rangePct, Math.min(rangePct, raw));
	if (Math.abs(clamped - currentPct) < 1e-9) return null;
	return clamped;
}

/** The next range after `current` in `ranges` (wrapping) that can hold |tempoPct|;
 *  null when no other range fits. */
export function nextPitchRange(
	current: PitchRange,
	tempoPct: number,
	ranges: readonly PitchRange[] = PITCH_RANGES
): PitchRange | null {
	const n = ranges.length;
	if (n <= 1) return null;
	const startIdx = ranges.indexOf(current);
	const base = startIdx >= 0 ? startIdx : -1;
	const absPct = Math.abs(tempoPct);
	for (let offset = 1; offset < n; offset++) {
		const r = ranges[(base + offset) % n]!;
		if (absPct <= r + 1e-9) return r;
	}
	return null;
}

/** The one command a tempo key sends for this deck, or null (no deck, empty deck,
 *  clamped, already at 0, no other range fits). */
export function tempoKeyCommand(
	action: TempoKeyAction,
	deck: TempoKeyDeck | null
): TempoWrite | null {
	if (deck === null || !deck.loaded) return null;
	const pct = tempoPctFromRatio(deck.pitch);
	switch (action.kind) {
		case 'tempo-step': {
			const next = nextTempoPct(pct, action.direction, action.coarse, deck.range);
			if (next === null) return null;
			return { type: 'tempo', deck: deck.deck, ratio: 1 + next / 100 };
		}
		case 'tempo-reset':
			if (Math.abs(deck.pitch - 1) < 1e-9) return null;
			return { type: 'tempo', deck: deck.deck, ratio: 1 };
		case 'pitch-range-cycle': {
			const next = nextPitchRange(deck.range, pct);
			if (next === null) return null;
			return { type: 'pitch_range', deck: deck.deck, range: next };
		}
		default: {
			const _exhaustive: never = action;
			return _exhaustive;
		}
	}
}
