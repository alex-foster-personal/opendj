/**
 * Click selection for mixer channels and deck panels (MIXUX-08 AC1, AC6).
 * Hover highlight stays in deck-hover.svelte.ts; this module owns click select.
 */
import type { DeckId } from './deck-slots';
import { singleSelectedDeck } from './focused-deck';
import { noteRecentDeck } from './recent-deck';

/** `clickedDeck` is the one deck the last click left selected, null before any
 * click or after a click that left several. Only `clickSelect` writes it: deck
 * loads rewrite `selected` (Space follows the loading deck) but are not a
 * choice, so Track information reads this instead (Sol P1 4350579790 and
 * 4351433888 on #6824: with no deck chosen it opens the master). */
export const mixerSelection = $state<{ selected: DeckId[]; clickedDeck: DeckId | null }>({
	selected: [1],
	clickedDeck: null
});

type MixerSelectionListener = (selected: readonly DeckId[]) => void;
const _selectionListeners = new Set<MixerSelectionListener>();

/** Called after every write to `mixerSelection.selected`. The /performance
 * listeners bind their non-passive window wheel only while a deck is selected
 * (PERF-UI-13, #10369), and `$effect` does not run in the node tests. */
export function onMixerSelectionChange(listener: MixerSelectionListener): () => void {
	_selectionListeners.add(listener);
	return () => {
		_selectionListeners.delete(listener);
	};
}

function _setSelected(next: DeckId[]): void {
	mixerSelection.selected = next;
	for (const listener of _selectionListeners) listener(next);
}

export function isSelected(deck: DeckId): boolean {
	return mixerSelection.selected.includes(deck);
}

export function getSelectedDecks(): readonly DeckId[] {
	return mixerSelection.selected;
}

/** A click that leaves `deck` selected also makes it the target of Space and
 * the other transport shortcuts (DECKUX-25, pin a675881be6c8). A shift-click
 * that deselects it does not. */
export function clickSelect(deck: DeckId, shift: boolean): void {
	if (shift) {
		const next = new Set(mixerSelection.selected);
		if (next.has(deck)) {
			next.delete(deck);
			if (next.size === 0) next.add(deck);
		} else {
			next.add(deck);
		}
		_setSelected([...next].sort((a, b) => a - b));
	} else {
		resetToSingle(deck);
	}
	mixerSelection.clickedDeck = singleSelectedDeck(mixerSelection.selected);
	if (mixerSelection.selected.includes(deck)) noteRecentDeck(deck);
}

export function resetToSingle(deck: DeckId | null): void {
	if (deck === null) {
		_setSelected([]);
		return;
	}
	_setSelected([deck]);
}

export function clearSelection(): void {
	_setSelected([]);
}

/** Deck load resets multi-select back to the loading deck only. */
export function onDeckLoadStart(deck: DeckId): void {
	resetToSingle(deck);
}

/** True when a click target is inside library, deck, or mixer performance chrome. */
export function isPerformanceSelectionTarget(target: EventTarget | null): boolean {
	if (!(target instanceof Element)) return false;
	return (
		target.closest('[data-library-root]') !== null ||
		target.closest('[data-mixer-channel]') !== null ||
		target.closest('[data-deck-hover]') !== null ||
		target.closest('.rb-mixer') !== null ||
		target.closest('.rb-deck') !== null
	);
}
