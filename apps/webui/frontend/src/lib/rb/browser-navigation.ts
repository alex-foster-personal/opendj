/**
 * IOPIN-01 browser ownership policy.  Keep this independent of Svelte and
 * MIDI so keyboard and controller browsing use the same direction and focus
 * vocabulary instead of whichever DOM node happened to receive focus first.
 */
export type BrowserFocusZone = 'playlist' | 'tracks' | 'deck';

/** A selection step, or null for a key that is not browser vertical movement. */
export function browserSelectionDelta(key: string): -1 | 1 | null {
	if (key === 'ArrowDown' || key === 's' || key === 'S') return 1;
	if (key === 'ArrowUp' || key === 'w' || key === 'W') return -1;
	return null;
}

/** Horizontal browser traversal never escapes the three documented zones. */
export function moveBrowserFocus(
	focus: BrowserFocusZone,
	key: 'ArrowLeft' | 'ArrowRight'
): BrowserFocusZone {
	if (key === 'ArrowRight') {
		if (focus === 'playlist') return 'tracks';
		if (focus === 'tracks') return 'deck';
		return 'deck';
	}
	if (focus === 'deck') return 'tracks';
	if (focus === 'tracks') return 'playlist';
	return 'playlist';
}

/** Editable controls and an open context menu own their own keystrokes. */
export function browserNavigationMayHandle({
	editable,
	contextMenuOpen
}: {
	editable: boolean;
	contextMenuOpen: boolean;
}): boolean {
	return !editable && !contextMenuOpen;
}
