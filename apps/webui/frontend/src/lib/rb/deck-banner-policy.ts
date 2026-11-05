/**
 * Which in-deck banners stay until dismissed (DECKUX-LABEL-02).
 *
 * The deck banner never fades, which is right for a real fault: it has to be
 * on screen when the DJ looks back at the deck. It was wrong for an expected,
 * self-resolving condition. A stem control pressed while the deck's stems are
 * still loading threw "deck N stems are loading: no aligned artifact", and the
 * banner kept saying so on all four decks long after every stem bundle was
 * ready (live, silver, Tue 3 Nov 2026).
 *
 * Pure: no stores, no timers. Deck.svelte owns the timer.
 */

export type DeckStemsStatus = 'unavailable' | 'loading' | 'ready' | 'error';
export type DeckBannerTone = 'fault' | 'expected';

/** How long an expected-condition banner stays up; matches the tray's default toast. */
export const DECK_EXPECTED_BANNER_MS = 5000;

const STEMS_LOADING = /^(?:Error: )?deck [1-4] stems are loading: /i;

/** 'expected' for a known self-resolving condition, 'fault' for everything else. */
export function deckBannerTone(message: string): DeckBannerTone {
	return STEMS_LOADING.test(message) ? 'expected' : 'fault';
}

/**
 * Milliseconds until the banner should clear itself, or null to stay until
 * dismissed. An expected banner whose condition has already resolved (the
 * stems are no longer loading) is stale and clears at once.
 */
export function deckBannerDismissMs(message: string, stemsStatus: DeckStemsStatus): number | null {
	if (deckBannerTone(message) === 'fault') return null;
	if (stemsStatus === 'loading') return DECK_EXPECTED_BANNER_MS;
	return 0;
}
