/**
 * TOPVIEW-05: which deck the top bar's Track information (i) button opens.
 *
 * "Focused" is the deck the user selected by clicking its panel or mixer
 * channel (mixer-selection.svelte.ts, the same selection Space and the
 * transport shortcuts follow), when exactly one deck is selected and it still
 * holds a track. Otherwise the master deck, otherwise the
 * lowest-numbered loaded deck, otherwise none: the button is then disabled
 * and says why, never opening an empty record.
 */
import type { DeckId } from './deck-id';

export interface FocusCandidate {
	id: DeckId;
	loaded: boolean;
	isMaster: boolean;
}

export function resolveFocusedDeck(
	selected: DeckId | null,
	decks: readonly FocusCandidate[]
): DeckId | null {
	const chosen = decks.find((d) => d.id === selected);
	if (chosen?.loaded) return chosen.id;
	const master = decks.find((d) => d.isMaster && d.loaded);
	if (master !== undefined) return master.id;
	return decks.find((d) => d.loaded)?.id ?? null;
}

/** The one selected deck, or null for a multi-deck or empty selection. */
export function singleSelectedDeck(selected: readonly DeckId[]): DeckId | null {
	return selected.length === 1 ? selected[0] : null;
}

/** The deck and track the (i) overlay was opened for, captured at the open. */
export interface OpenedTrackInfo {
	/** Null when opened for a library row that is not tied to a loaded deck (#9451). */
	deckId: DeckId | null;
	stableId: string;
}

/**
 * What the open (i) overlay shows (Sol P1 4284394475; ported from #6228's
 * ce4bbd5dc and tightened): the deck and track captured when it opened, while
 * that deck still holds that same track. Null once that deck empties OR loads
 * a different track, so the overlay closes instead of falling through to
 * whichever deck is focused next or silently swapping to the new track.
 */
export function openedTrackInfo(
	opened: OpenedTrackInfo | null,
	stableIdOf: (deck: DeckId) => string | null
): OpenedTrackInfo | null {
	if (opened === null) return null;
	if (opened.deckId === null) return opened;
	return stableIdOf(opened.deckId) === opened.stableId ? opened : null;
}
