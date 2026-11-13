/**
 * Deck rating write and the engine addressing read model, split out of
 * audio-engine.svelte.ts (issue #8581 slices): both only read or write the
 * shared deck state and never touch the audio graph. Re-exported from the
 * engine barrel, so importers of $lib/rb/audio-engine.svelte are unchanged.
 */
import { pushToast } from '$lib/stores.svelte';
import { ConflictError, saveTrackRating } from '$lib/rb/api-rb';
import { normalizeRating } from '$lib/rating';
import { isShadowDeck, type EngineDeckId } from '$lib/rb/deck-id';
import type { DeckId } from '$lib/rb/deck-slots';
import { DECK_IDS, ENGINE_DECK_IDS } from '$lib/player/constants';
import { deckStates } from '$lib/player/state.svelte';

/** Set the loaded deck track's rating (deck-header pin 4de63478782c). Same
 * PATCH /tracks/{sid} + If-Match path as the library rating cell
 * (BrowserPanel._patchRating) - deck-header just has no ETag of its own to
 * carry, so it always fetches one fresh first. A race where a different
 * track loads onto this deck while the request is in flight is guarded by
 * re-checking stable_id before writing the result back. A USB stick track's
 * rating is a session edit with no request (USB Play spec decision 2). */
export async function rateDeckTrack(deck: DeckId, next: number): Promise<void> {
	const stable_id = deckStates[deck].stable_id;
	if (stable_id === null) return;
	try {
		const rating = await saveTrackRating(stable_id, next);
		// Every path awaits (a request, or the stick store's on-demand import).
		if (deckStates[deck].stable_id === stable_id) deckStates[deck].rating = normalizeRating(rating);
	} catch (exc) {
		if (exc instanceof ConflictError) {
			if (deckStates[deck].stable_id === stable_id) {
				deckStates[deck].rating = normalizeRating(exc.current.rating ?? null);
			}
			pushToast('rating conflict: track changed elsewhere - showing current value', 'error');
			return;
		}
		pushToast(`rating update failed: ${String(exc)}`, 'error');
	}
}

export interface EngineAddressingRow {
	deck: EngineDeckId;
	/** True when this slot is shown on the performance mixer (1-4 only). */
	visible: boolean;
	loaded: boolean;
	playing: boolean;
	position_ms: number;
}

/** Read model for agents and Rust mode: all engine slots, shadow rows stay inaudible on the UI. */
export function query(): {
	decks: EngineAddressingRow[];
	live_decks: readonly DeckId[];
	engine_decks: readonly EngineDeckId[];
} {
	const decks: EngineAddressingRow[] = ENGINE_DECK_IDS.map((deck) => {
		if (isShadowDeck(deck)) {
			return { deck, visible: false, loaded: false, playing: false, position_ms: 0 };
		}
		const st = deckStates[deck];
		return {
			deck,
			visible: true,
			loaded: st.stable_id !== null,
			playing: st.playing,
			position_ms: st.position_ms
		};
	});
	return { decks, live_decks: DECK_IDS, engine_decks: ENGINE_DECK_IDS };
}
