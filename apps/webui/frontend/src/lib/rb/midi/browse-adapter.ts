/**
 * Browser selection ownership shared with hardware navigation.
 * Supersedes: the registry embedded in action-glue.svelte.ts. Keeping this
 * boundary independent avoids loading MIDI transport and pad actions merely
 * to register the library panel's selection callbacks.
 */
import type { DeckId } from '$lib/rb/deck-id';

export interface BrowseAdapter {
	moveSelection(delta: number): void;
	loadSelected(deck: DeckId): void;
}

let current: BrowseAdapter | null = null;

export function getBrowseAdapter(): BrowseAdapter | null {
	return current;
}

export function registerBrowseAdapter(adapter: BrowseAdapter): () => void {
	if (current !== null) {
		throw new Error('registerBrowseAdapter: an adapter is already registered');
	}
	current = adapter;
	return () => {
		if (current !== adapter) {
			throw new Error('registerBrowseAdapter: adapter ownership changed before cleanup');
		}
		current = null;
	};
}
