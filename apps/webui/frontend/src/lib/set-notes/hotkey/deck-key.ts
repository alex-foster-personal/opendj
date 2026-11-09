/**
 * SETNOTE-6340i / SETNOTE-9940: the hover + 1-4 deck-load shortcut counts only an
 * unmodified, non-repeated key. Shift is a modifier like the rest: on layouts
 * where Shift+digit types a symbol whose `key` maps to a deck, it stays typing.
 */

export interface DeckKeyEvent {
	repeat: boolean;
	metaKey: boolean;
	ctrlKey: boolean;
	altKey: boolean;
	shiftKey: boolean;
}

export function isPlainDeckKey(e: DeckKeyEvent): boolean {
	return !e.repeat && !e.metaKey && !e.ctrlKey && !e.altKey && !e.shiftKey;
}
