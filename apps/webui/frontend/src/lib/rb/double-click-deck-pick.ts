/**
 * Pure double-click deck-target picker for the browser row load gesture.
 * Extracted from BrowserPanel.svelte so the race/replace regressions below
 * are unit-testable without a component harness.
 */
import type { DeckId } from './types';

export type DeckLoadSeq = Record<DeckId, number>;

export interface DeckPairState {
	stable_id: string | null;
	playing: boolean;
}

export interface DoubleClickPickInput {
	shift: boolean;
	replace: boolean;
	deckLoadSeq: DeckLoadSeq;
	lastDoubleClickDeck: DeckId | null;
	/** CH3/CH4 live state, only consulted when shift is true. */
	pair: Record<3 | 4, DeckPairState>;
}

export type DoubleClickPickResult =
	| { deck: DeckId; error?: undefined }
	| { deck: null; error: string | null };

/**
 * Smart-load target: CH1/CH2 by default (least-recent by deckLoadSeq).
 * Shift: CH3/CH4, empty preferred over stopped; null (with a toast message)
 * if both are playing. Replace (cmd/ctrl): reuse lastDoubleClickDeck instead
 * of advancing - falls through to the normal pick if nothing was double-
 * clicked yet this session.
 *
 * Callers MUST reserve the returned deck (bump deckLoadSeq) synchronously,
 * before starting the async load - see _reserveDeckSlot in BrowserPanel.
 * Regression Mon 17 Aug 2026: reserving only after the load resolved let two
 * fast double-clicks both read the same stale deckLoadSeq and pick the same
 * deck, so the second track silently replaced the first instead of landing
 * on the other deck.
 */
export function pickDoubleClickDeck(input: DoubleClickPickInput): DoubleClickPickResult {
	if (input.replace && input.lastDoubleClickDeck !== null) {
		return { deck: input.lastDoubleClickDeck };
	}
	if (!input.shift) {
		return { deck: input.deckLoadSeq[1] <= input.deckLoadSeq[2] ? 1 : 2 };
	}
	const pairIds: Array<3 | 4> = [3, 4];
	const empty = pairIds.filter((d) => input.pair[d].stable_id === null);
	const stopped = pairIds.filter((d) => input.pair[d].stable_id !== null && !input.pair[d].playing);
	const candidates = empty.length > 0 ? empty : stopped;
	if (candidates.length === 0) {
		return {
			deck: null,
			error: 'shift+dblclick: CH3 and CH4 are both playing - pause or unload one first'
		};
	}
	if (candidates.length === 1) return { deck: candidates[0] };
	return {
		deck:
			input.deckLoadSeq[candidates[0]] <= input.deckLoadSeq[candidates[1]]
				? candidates[0]
				: candidates[1]
	};
}
