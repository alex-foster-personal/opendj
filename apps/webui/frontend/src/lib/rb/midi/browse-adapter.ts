/**
 * Browser selection ownership shared with hardware navigation.
 * Supersedes: the registry embedded in action-glue.svelte.ts. Keeping this
 * boundary independent avoids loading MIDI transport and pad actions merely
 * to register the library panel's selection callbacks.
 *
 * IOPIN-12629: the browse wheel and keyboard/WASD share one navigation policy
 * (`browser-navigation.ts`); this module is the MIDI-facing entry for that
 * policy plus the registered BrowserPanel adapter.
 */
import type { DeckId } from '$lib/rb/deck-id';
import { browserNavigationMayHandle } from '$lib/rb/browser-navigation';

export {
	browserNavigationMayHandle,
	browserSelectionDelta,
	moveBrowserFocus,
	type BrowserFocusZone
} from '$lib/rb/browser-navigation';

/** True when the library browse wheel may advance track selection (IOPIN-01). */
export function browseWheelMayAdvance(): boolean {
	const contextMenuOpen =
		typeof document !== 'undefined' &&
		document.querySelector('[data-testid="context-menu"]') !== null;
	return browserNavigationMayHandle({ editable: false, contextMenuOpen });
}

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
