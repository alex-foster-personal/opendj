/** Synthesized engine-state dependency only; refresh/cache/sink code stays real. */
import type { AnlzWithVocals } from '$lib/rb/api-rb';
import type { DeckId } from '$lib/rb/deck-slots';

interface VocalDeck {
	stable_id: string | null;
	anlz: AnlzWithVocals | null;
	loadToken: number;
}

export const DECK_IDS: readonly DeckId[] = [1, 2, 3, 4];
export const deckStates: Record<DeckId, VocalDeck> = {
	1: { stable_id: null, anlz: null, loadToken: 0 },
	2: { stable_id: null, anlz: null, loadToken: 0 },
	3: { stable_id: null, anlz: null, loadToken: 0 },
	4: { stable_id: null, anlz: null, loadToken: 0 }
};
