/**
 * Click selection for mixer channels and deck panels (MIXUX-08 AC1, AC6).
 * Hover highlight stays in deck-hover.svelte.ts; this module owns click select.
 */
import type { DeckId } from './deck-slots';
import { noteRecentDeck } from './recent-deck';

export const mixerSelection = $state<{ selected: DeckId[] }>({ selected: [1] });

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
		mixerSelection.selected = [...next].sort((a, b) => a - b);
	} else {
		resetToSingle(deck);
	}
	if (mixerSelection.selected.includes(deck)) noteRecentDeck(deck);
}

export function resetToSingle(deck: DeckId | null): void {
	if (deck === null) {
		mixerSelection.selected = [];
		return;
	}
	mixerSelection.selected = [deck];
}

export function clearSelection(): void {
	mixerSelection.selected = [];
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
