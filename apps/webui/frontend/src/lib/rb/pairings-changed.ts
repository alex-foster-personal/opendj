/** A pairing write made in this tab tells the pairing stores to reload.
 *
 * The engine's library.changed kind=pairings event covers writes from
 * elsewhere, but a server without that route (the legacy app) never sends it,
 * so every local create path announces itself here instead. */
const PAIRINGS_CHANGED_EVENT = 'mdt:pairings-changed';

export function announcePairingsChanged(): void {
	window.dispatchEvent(new Event(PAIRINGS_CHANGED_EVENT));
}

/** Runs `handler` on every pairing write; returns the unsubscribe. */
export function subscribePairingsChanged(handler: () => void): () => void {
	window.addEventListener(PAIRINGS_CHANGED_EVENT, handler);
	return () => window.removeEventListener(PAIRINGS_CHANGED_EVENT, handler);
}
