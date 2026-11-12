/**
 * Refresh client ANLZ vocals for loaded decks when vocal-cache changes,
 * without waiting on the playback-gated library row refresh (issue #1031).
 */
import { subscribeKind, subscribeResync } from '$lib/api/events-bus';
import type { DeckId } from '$lib/rb/deck-slots';
import { DECK_IDS, deckStates } from '$lib/rb/audio-engine.svelte';
import { getVocalStatus, type Vocals } from '$lib/rb/api-rb';
import { refreshAnlzVocalsIfNeeded } from '$lib/components/rb/wave/anlz-cache.svelte';

function _loadedStableIds(): string[] {
	const seen = new Set<string>();
	const out: string[] = [];
	for (const deck of DECK_IDS) {
		const stable_id = deckStates[deck].stable_id;
		if (stable_id === null || seen.has(stable_id)) continue;
		seen.add(stable_id);
		out.push(stable_id);
	}
	return out;
}

/** Ingest completion publishes `kind: 'tracks'` with an empty `ids` list;
 * metadata edits publish concrete stable_ids. React only when every loaded
 * deck may need a refresh (empty ids) or a named id is on a deck now. */
function _tracksEventTouchesLoadedDeck(ids: string[]): boolean {
	if (ids.length === 0) return true;
	return _loadedStableIds().some((stable_id) => ids.includes(stable_id));
}

/** Fire-and-forget: read the authoritative vocal status and bypass-fetch only
 * when it disagrees with the shared ANLZ cache. */
export function refreshLoadedDeckVocalsIfNeeded(
	stable_id: string,
	observedStatus: Vocals['status']
): void {
	refreshAnlzVocalsIfNeeded(stable_id, observedStatus);
}

/** Same as above but fetches `GET /vocals/{id}/status` first so ingest
 * completion can refresh a loaded deck even before pane rows reload. */
export async function refreshLoadedDeckVocalsFromServer(
	stable_ids: readonly string[]
): Promise<void> {
	for (const stable_id of stable_ids) {
		const status = await getVocalStatus(stable_id);
		// The track may have left every deck while its status request was in flight.
		if (!isStableIdOnLoadedDeck(stable_id)) continue;
		refreshAnlzVocalsIfNeeded(stable_id, status.status);
	}
}

function _refreshLoadedDecksForTracksEvent(ids: string[]): void {
	const loaded = _loadedStableIds();
	if (loaded.length === 0) return;
	const targets = ids.length === 0 ? loaded : loaded.filter((stable_id) => ids.includes(stable_id));
	if (targets.length === 0) return;
	void refreshLoadedDeckVocalsFromServer(targets);
}

/** Subscribe to library ingest completion and resync without the playing gate.
 * Returns unsubscribe for BrowserPanel's onMount cleanup. */
export function subscribeLoadedDeckVocalsRefresh(): () => void {
	const unsubscribeTracks = subscribeKind('tracks', (ids) => {
		if (_tracksEventTouchesLoadedDeck(ids)) _refreshLoadedDecksForTracksEvent(ids);
	});
	const unsubscribeResync = subscribeResync(() => {
		const loaded = _loadedStableIds();
		if (loaded.length > 0) void refreshLoadedDeckVocalsFromServer(loaded);
	});
	return () => {
		unsubscribeTracks();
		unsubscribeResync();
	};
}

/** True when `stable_id` is currently loaded on any deck. */
export function isStableIdOnLoadedDeck(stable_id: string): boolean {
	return DECK_IDS.some((deck: DeckId) => deckStates[deck].stable_id === stable_id);
}
