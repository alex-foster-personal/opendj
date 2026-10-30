/**
 * Which track time a right-click on a deck waveform points at.
 *
 * The waveform surfaces mark themselves `data-wave-surface="row"` (the
 * scrolling wave, centered on the playhead) or `"strip"` (the whole-track
 * overview) inside a `data-deck` element, so the click position resolves
 * from the DOM plus the deck state with no hook in the wave component.
 * Shared by the vocal-lane Fix / add comment item (FB-12) and
 * "Transition in around here" (AGENT-51).
 */
import { DECK_IDS, type DeckId } from '$lib/player/constants';
import { getDeckState } from '$lib/rb/audio-engine.svelte';
import { stripTimeS, waveRowTimeS } from '$lib/rb/vocal-correction';

export type WaveClick = { deck: DeckId; stableId: string; timeS: number };

function _deckOf(el: Element): DeckId | null {
	const n = Number(el.closest('[data-deck]')?.getAttribute('data-deck'));
	return DECK_IDS.includes(n as DeckId) ? (n as DeckId) : null;
}

/** The loaded track and time under the pointer, or null off a wave surface
 * or on a deck with nothing (or nothing measured) loaded. */
export function waveClickAt(event: MouseEvent, target: EventTarget | null): WaveClick | null {
	const surface = target instanceof Element ? target.closest('[data-wave-surface="row"], [data-wave-surface="strip"]') : null;
	if (!(surface instanceof HTMLElement)) return null;
	const deckId = _deckOf(surface);
	if (deckId === null) return null;
	const deck = getDeckState(deckId);
	if (deck.stable_id === null || deck.duration_ms === null || deck.duration_ms <= 0) return null;
	const rect = surface.getBoundingClientRect();
	if (rect.width <= 0) return null;
	const pointerX = event.clientX - rect.left;
	const timeS =
		surface.getAttribute('data-wave-surface') === 'row'
			? waveRowTimeS({
					pointerX,
					widthPx: rect.width,
					centerPositionMs: deck.position_ms,
					durationMs: deck.duration_ms,
					pitch: deck.pitch
				})
			: stripTimeS({ pointerX, widthPx: rect.width, durationMs: deck.duration_ms });
	return { deck: deckId, stableId: deck.stable_id, timeS };
}
