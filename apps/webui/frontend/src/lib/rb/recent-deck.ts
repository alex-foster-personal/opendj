import { announce } from '$lib/a11y/announce';
import type { DeckId } from '$lib/rb/deck-slots';

/** Load and play presence of one deck, enough to pick the keyboard deck. */
export interface DeckPresence {
	id: DeckId;
	loaded: boolean;
	playing: boolean;
}

/** Most recently loaded / played / loop-targeted deck for Space play/pause.
 * Mixer click select and MIDI deck-pair select both update this via
 * mixer-selection.svelte.ts. */
let lastRecentDeck: DeckId | null = null;
const listeners = new Set<(deck: DeckId | null) => void>();

export function noteRecentDeck(deck: DeckId): void {
	if (deck === lastRecentDeck) return;
	lastRecentDeck = deck;
	announce(`Deck ${deck} selected`);
	for (const listener of listeners) listener(deck);
}

/** Unit-test seam: clears remembered deck. */
export function _resetRecentDeckForTests(): void {
	lastRecentDeck = null;
}

export function getRecentDeck(): DeckId | null {
	return lastRecentDeck;
}

/** KEYB-11383: lets the deck header redraw its keyboard-deck marker. */
export function subscribeRecentDeck(listener: (deck: DeckId | null) => void): () => void {
	listeners.add(listener);
	return () => {
		listeners.delete(listener);
	};
}

/** The deck the keyboard acts on: the recent deck if loaded, else the first
 * playing loaded deck, else the first loaded deck, else null. */
export function resolveKeyboardDeck(
	recent: DeckId | null,
	decks: readonly DeckPresence[]
): DeckId | null {
	if (recent !== null && decks.some((d) => d.id === recent && d.loaded)) return recent;
	const playing = decks.find((d) => d.loaded && d.playing);
	if (playing !== undefined) return playing.id;
	return decks.find((d) => d.loaded)?.id ?? null;
}

/** KEYB-11383: next loaded deck after `current` in deck order, wrapping
 * (direction -1 walks back). No loaded deck returns `current` unchanged. */
export function cycleRecentDeck(
	current: DeckId | null,
	direction: 1 | -1,
	loadedDecks: readonly DeckId[]
): DeckId | null {
	if (loadedDecks.length === 0) return current;
	const sorted = [...loadedDecks].sort((a, b) => a - b);
	if (direction === 1) {
		if (current === null) return sorted[0];
		return sorted.find((d) => d > current) ?? sorted[0];
	}
	if (current === null) return sorted[sorted.length - 1];
	return [...sorted].reverse().find((d) => d < current) ?? sorted[sorted.length - 1];
}
