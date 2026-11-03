import { untrack } from 'svelte';
import type { DeckId } from '$lib/player/constants';

/** Which decks have a zoom glide running now. Reactive, so a stopped follower
 * row repaints while the reference deck glides (WAVEZOOM-6802c). */
const running = $state<Partial<Record<DeckId, boolean>>>({});

export function waveZoomGlideRunning(deckId: DeckId): boolean {
	return running[deckId] === true;
}

export function setWaveZoomGlideRunning(deckId: DeckId, on: boolean): void {
	running[deckId] = on;
}

/** The beats each deck's row paints, published by that row, so every row sizes
 * the reference's bars from the same analysis the reference row draws. Reactive
 * and replaced whole on each write, so a follower's view re-derives when the
 * reference's analysis changes (WAVEZOOM-6802). */
let rowBeats = $state.raw<Partial<Record<DeckId, readonly { t: number; bpm: number }[] | null>>>({});

export function waveZoomRowBeats(deckId: DeckId): readonly { t: number; bpm: number }[] | null {
	return rowBeats[deckId] ?? null;
}

/** The scrub position each deck's row is painting during a drag or click, or null.
 * Followers place the shared playhead from it, so the reference's column moves
 * with the gesture rather than after the seek lands. Replaced whole on each write. */
let scrubPreview = $state.raw<Partial<Record<DeckId, number | null>>>({});

export function waveZoomScrubPreviewMs(deckId: DeckId): number | null {
	return scrubPreview[deckId] ?? null;
}

/** The window scale a row holds for its drag, or null. Linked follower rows take
 * the reference's held scale, so the beat spacing stays shared across a variable
 * tempo boundary for the whole gesture. Replaced whole on each write. */
let scrubWindow = $state.raw<Partial<Record<DeckId, number | null>>>({});

export function waveZoomScrubWindowS(deckId: DeckId): number | null {
	return scrubWindow[deckId] ?? null;
}

export function setWaveZoomScrubWindowS(deckId: DeckId, windowS: number | null): void {
	scrubWindow = { ...untrack(() => scrubWindow), [deckId]: windowS };
}

export function setWaveZoomScrubPreviewMs(deckId: DeckId, ms: number | null): void {
	scrubPreview = { ...untrack(() => scrubPreview), [deckId]: ms };
}

/** Called from each row's publisher effect, so the previous record is read
 * untracked: otherwise the effect would subscribe to the registry it writes and
 * every publish would re-run every publisher. */
export function setWaveZoomRowBeats(deckId: DeckId, beats: readonly { t: number; bpm: number }[] | null): void {
	rowBeats = { ...untrack(() => rowBeats), [deckId]: beats };
}
