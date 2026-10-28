/**
 * TOPVIEW-05: whether the top bar's Track information (i) overlay is open, and
 * for which deck and track, with the rules that open, close and report it.
 * The button and the performance dispatcher's `track_info_toggle` /
 * `track_info_close` share this one state (Codex P1 4300879440 on #6824).
 * Deck data is passed in, never imported, so this module stays below
 * audio-engine.svelte.ts and outside its import cycle; focus follows the
 * click selection in mixer-selection.svelte.ts (focused-deck.ts has the rule).
 */
import type { DeckId } from './deck-id';
import {
	openedTrackInfo,
	resolveFocusedDeck,
	type FocusCandidate,
	type OpenedTrackInfo
} from './focused-deck';
import { mixerSelection } from './mixer-selection.svelte';

export const trackInfo = $state<{ opened: OpenedTrackInfo | null }>({ opened: null });

export interface TrackInfoDeck extends FocusCandidate {
	stableId: string | null;
}

/** The deck the button opens: see focused-deck.ts. Neither the startup
 * default nor a deck load is a choice, so it follows the last clicked deck. */
export function focusedTrackInfoDeck(decks: readonly TrackInfoDeck[]): DeckId | null {
	return resolveFocusedDeck(mixerSelection.clickedDeck, decks);
}

/** What the open overlay shows, or null once its deck empties or loads another track. */
export function shownTrackInfo(decks: readonly TrackInfoDeck[]): OpenedTrackInfo | null {
	return openedTrackInfo(trackInfo.opened, (deck) => decks.find((d) => d.id === deck)?.stableId ?? null);
}

/** Close if open, else open for the focused deck; with no deck loaded, refuse. */
export function toggleTrackInfo(decks: readonly TrackInfoDeck[]): void {
	if (trackInfo.opened !== null) {
		closeTrackInfo();
		return;
	}
	const deckId = focusedTrackInfoDeck(decks);
	const stableId = deckId === null ? null : (decks.find((d) => d.id === deckId)?.stableId ?? null);
	if (deckId === null || stableId === null) {
		throw new Error('track_info_toggle: no deck holds a track, so there is no record to open');
	}
	trackInfo.opened = { deckId, stableId };
}

export function closeTrackInfo(): void {
	trackInfo.opened = null;
}

export function trackInfoSnapshot(decks: readonly TrackInfoDeck[]): {
	open: boolean;
	deck: DeckId | null;
	stable_id: string | null;
} {
	const shown = shownTrackInfo(decks);
	return { open: shown !== null, deck: shown?.deckId ?? null, stable_id: shown?.stableId ?? null };
}
