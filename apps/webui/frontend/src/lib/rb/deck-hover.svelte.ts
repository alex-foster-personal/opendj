/** Cross-panel deck hover focus (deck / mixer / wavestack / library). */
import type { DeckId } from '$lib/rb/deck-slots';

export const deckHoverUi = $state({
	deckId: null as DeckId | null
});

export function setHoveredDeck(deckId: DeckId | null): void {
	deckHoverUi.deckId = deckId;
}

// -------------------------------------------------------------- enter/leave
// RECOVERED, not designed. `Deck.svelte` has imported these three since
// cfbfe55 ("wip(spike)", Fri 24 Jul 2026 01:19) but they were never committed
// -- not on the branch, not in the Cursor backup 718cc81 -- so /performance
// could not hydrate. Reconstructed from the call sites and the existing
// `setHoveredDeck` above; semantics are inferred, so treat as provisional.

/** Deck id from a `data-deck-hover` element, or null when it carries none. */
export function deckIdFromHoverEl(el: EventTarget | null): DeckId | null {
	const node = el instanceof Element ? el.closest('[data-deck-hover]') : null;
	if (node === null) return null;
	const raw = node.getAttribute('data-deck-hover');
	if (raw === null || raw === '') return null;
	const n = Number(raw);
	return Number.isFinite(n) ? (n as DeckId) : null;
}

export function deckHoverEnter(deckId: DeckId | null): void {
	if (deckId === null) return;
	setHoveredDeck(deckId);
}

/**
 * Clear hover, but only when the pointer actually left the deck's subtree.
 * Without the relatedTarget check, moving between a deck's own children
 * fires leave and the highlight flickers.
 */
export function deckHoverLeave(deckId: DeckId | null, e?: PointerEvent): void {
	if (deckId === null) return;
	const next = e?.relatedTarget;
	if (next instanceof Node && e?.currentTarget instanceof Node) {
		if (e.currentTarget.contains(next)) return;
	}
	if (deckHoverUi.deckId === deckId) setHoveredDeck(null);
}
