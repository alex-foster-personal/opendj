/** Analysis-source switch cache and deck refresh transaction. */
import { bumpAnlzFetchGeneration } from '$lib/rb/anlz-fetch-generation';
import { fetchAnlzBypassingHttpCache } from '$lib/rb/api-rb';
import type { AnlzData } from '$lib/rb/anlz-types';
import {
	invalidateAllAnlzCacheEntries,
	invalidateAnlzCacheEntry,
	refreshAnlzCacheEntry
} from '$lib/components/rb/wave/anlz-cache.svelte';

type DeckId = 1 | 2 | 3 | 4;

export interface AnalysisSourceRefreshDeck {
	stable_id: string | null;
	anlz: AnlzData | null;
}

/** Replaces loaded decks without allowing an in-flight stale response to win. */
export async function refreshAnalysisSourceDecks(
	deckIds: readonly DeckId[],
	decks: Record<DeckId, AnalysisSourceRefreshDeck>
): Promise<void> {
	bumpAnlzFetchGeneration();
	invalidateAllAnlzCacheEntries();
	await Promise.all(
		deckIds.map(async (deck) => {
			const stableId = decks[deck].stable_id;
			if (stableId === null) return;
			const fresh = await fetchAnlzBypassingHttpCache(stableId).catch((error: unknown) => {
				invalidateAnlzCacheEntry(stableId);
				throw error;
			});
			if (decks[deck].stable_id !== stableId) return;
			refreshAnlzCacheEntry(stableId, fresh);
			decks[deck].anlz = fresh;
		})
	);
}
