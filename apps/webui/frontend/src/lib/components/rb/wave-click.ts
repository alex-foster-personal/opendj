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

/** A waveform row publishes its live zoom view; a missing or malformed one is
 * a broken DOM contract, so it throws rather than mapping the click to the
 * default 24-second window (WAVEZOOM-6802c). */
function _rowViewAttr(el: Element, name: string, valid: (value: number) => boolean): number {
	const raw = el.getAttribute(name);
	const value = raw === null || raw.trim() === '' ? NaN : Number(raw);
	if (!Number.isFinite(value) || !valid(value)) {
		throw new Error(`wave click: ${name} is missing or malformed on a waveform row (got ${raw ?? 'null'})`);
	}
	return value;
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
					pitch: deck.pitch,
					// WaveRow publishes its live zoom view on the row element.
					windowS: _rowViewAttr(surface, 'data-wave-window-s', (v) => v > 0),
					playheadFrac: _rowViewAttr(surface, 'data-wave-playhead-frac', (v) => v >= 0 && v <= 1)
				})
			: stripTimeS({ pointerX, widthPx: rect.width, durationMs: deck.duration_ms });
	return { deck: deckId, stableId: deck.stable_id, timeS };
}
