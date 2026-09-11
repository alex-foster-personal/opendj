/**
 * Deferred beatgrid upgrade entry point.
 *
 * The player route needs this only after a deck starts loading. Keeping the
 * public call signature here lets that first use split the upgrade module out
 * of the route's initial bundle without changing its callers.
 */
import type { DeckState } from '$lib/rb/deck-state-types';

type DeckId = DeckState['deck_id'];
type OnSettled = (deck: DeckId, landed: boolean, publish: () => void) => void | Promise<void>;

export async function upgradeDeckBeatgrid(
	deck: DeckId,
	stableId: string,
	st: DeckState,
	isStale: () => boolean,
	onSettled?: OnSettled
): Promise<void> {
	const { upgradeDeckBeatgrid: upgrade } = await import('$lib/player/beatgrid-upgrade');
	return upgrade(deck, stableId, st, isStale, onSettled);
}
