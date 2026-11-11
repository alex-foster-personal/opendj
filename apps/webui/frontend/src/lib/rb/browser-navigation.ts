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

export type LibraryChordAction =
	{ kind: 'load'; deck: 1 | 2 } | { kind: 'double'; from: 1 | 2; to: 1 | 2 } | { kind: 'locate' };

/** Library chords (KEYB-11643): Shift+Left/Right load deck 1/2, Cmd/Ctrl+Shift+Left/Right
 * double from the other deck, Cmd/Ctrl+L locates the recent deck's track. Alt never matches. */
export function libraryChordAction(e: {
	key: string;
	shiftKey: boolean;
	metaKey: boolean;
	ctrlKey: boolean;
	altKey: boolean;
}): LibraryChordAction | null {
	if (e.altKey) return null;
	const mod = e.metaKey || e.ctrlKey;
	if (!mod && e.shiftKey) {
		if (e.key === 'ArrowLeft') return { kind: 'load', deck: 1 };
		if (e.key === 'ArrowRight') return { kind: 'load', deck: 2 };
	}
	if (mod && e.shiftKey) {
		if (e.key === 'ArrowLeft') return { kind: 'double', from: 2, to: 1 };
		if (e.key === 'ArrowRight') return { kind: 'double', from: 1, to: 2 };
	}
	if (mod && !e.shiftKey && (e.key === 'l' || e.key === 'L')) return { kind: 'locate' };
	return null;
}

export type LocateDecision =
	| { kind: 'none-loaded' }
	| { kind: 'reveal'; deck: 1 | 2 | 3 | 4; stableId: string }
	| { kind: 'all-tracks'; deck: 1 | 2 | 3 | 4; stableId: string }
	| { kind: 'absent'; deck: 1 | 2 | 3 | 4; stableId: string };

const LOCATE_DECK_ORDER = [1, 2, 3, 4] as const;

export function locateDecision(p: {
	recentDeck: 1 | 2 | 3 | 4 | null;
	deckStableIds: Readonly<Record<1 | 2 | 3 | 4, string | null>>;
	paneHas: (stableId: string) => boolean;
	paneIsAllTracks: boolean;
}): LocateDecision {
	let deck: (1 | 2 | 3 | 4) | null = null;
	if (p.recentDeck !== null && p.deckStableIds[p.recentDeck] !== null) {
		deck = p.recentDeck;
	} else {
		for (const d of LOCATE_DECK_ORDER) {
			if (p.deckStableIds[d] !== null) {
				deck = d;
				break;
			}
		}
	}
	if (deck === null) return { kind: 'none-loaded' };
	const stableId = p.deckStableIds[deck];
	if (stableId === null) return { kind: 'none-loaded' };
	if (p.paneHas(stableId)) return { kind: 'reveal', deck, stableId };
	if (p.paneIsAllTracks) return { kind: 'absent', deck, stableId };
	return { kind: 'all-tracks', deck, stableId };
}

export function instantDoubleCommand(
	from: 1 | 2,
	to: 1 | 2,
	sourceStableId: string | null
): { type: 'deck_move'; from: 1 | 2; to: 1 | 2; mode: 'copy'; crossfade_beats: 0 } | null {
	if (sourceStableId === null) return null;
	return { type: 'deck_move', from, to, mode: 'copy', crossfade_beats: 0 };
}
