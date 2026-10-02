/**
 * Top bar "2-deck view" button: a real toggle between the LESS (2 decks) and
 * MORE (all 4 decks) performance layouts.
 *
 * It drives the SAME persisted `deck_layout` preference, through the same
 * `setDeckLayoutMode` setter, that Cmd/Ctrl+2 and Cmd/Ctrl+4
 * (deck-layout-hotkeys.ts) and the Mixer MORE/LESS buttons already use, so
 * the button, the hotkeys and the mixer can never disagree about which
 * layout is live. This module is the pure half (no prefs access) so the
 * pressed state, next mode and explanatory copy are unit testable.
 */
import {
	DECK_LAYOUT_LESS_CHORD,
	DECK_LAYOUT_MORE_CHORD
} from '$lib/components/rb/hotkeys/hotkeys-registry';
import type { DeckLayoutMode } from './deck-layout-prefs';

export interface TwoDeckToggleView {
	/** aria-pressed: true while the 2-deck (LESS) layout is showing. */
	pressed: boolean;
	/** The mode a click switches to. */
	next: DeckLayoutMode;
	/** Explanatory title naming the current state, the effect of a click,
	 * and both keyboard shortcuts. */
	title: string;
}

export function describeTwoDeckToggle(mode: DeckLayoutMode): TwoDeckToggleView {
	const shortcuts = `${DECK_LAYOUT_LESS_CHORD} for 2 decks, ${DECK_LAYOUT_MORE_CHORD} for 4 decks`;
	if (mode === 'less') {
		return {
			pressed: true,
			next: 'more',
			title: `2-deck view is on: decks 3 and 4 are hidden (still playing) and the library gets the room. Click to show all 4 decks (${shortcuts}).`
		};
	}
	return {
		pressed: false,
		next: 'less',
		title: `2-deck view is off: all 4 decks are showing. Click to hide decks 3 and 4 and give the library more room; they keep playing (${shortcuts}).`
	};
}
