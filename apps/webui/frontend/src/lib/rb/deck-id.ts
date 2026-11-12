/** Physical deck slot 1-4. Layout: 1 top-left, 2 top-right, 3 bottom-left, 4 bottom-right. */
export type DeckId = 1 | 2 | 3 | 4;

/** Shadow stem slots 5-8 beside live decks (parent #313, issue #11153). */
export type ShadowDeckId = 5 | 6 | 7 | 8;

/** Every deck the Rust engine can address (live plus shadow). */
export type EngineDeckId = DeckId | ShadowDeckId;

export const ENGINE_DECK_IDS = [1, 2, 3, 4, 5, 6, 7, 8] as const satisfies readonly EngineDeckId[];

export const SHADOW_DECK_IDS = [5, 6, 7, 8] as const satisfies readonly ShadowDeckId[];

export function isShadowDeck(deck: EngineDeckId): deck is ShadowDeckId {
	return (SHADOW_DECK_IDS as readonly number[]).includes(deck);
}

export function isLiveDeck(deck: EngineDeckId): deck is DeckId {
	return deck === 1 || deck === 2 || deck === 3 || deck === 4;
}
