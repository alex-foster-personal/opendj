/**
 * Create pairing preconditions (DECKUX-12): what the top-bar button may
 * promise before it is clicked, and the beat number the snapshot records.
 * Plain module so the unit harness can load it without the audio engine.
 */
import type { AnlzBeat } from '$lib/rb/anlz-types';

type PairingBeat = Pick<AnlzBeat, 'n' | 'bpm' | 't'>;

const BEATS_PER_BAR = 4;

/**
 * The beat-in-bar number (1..4) the deck is on at `positionMs`: the last grid
 * beat at or before it. A playhead in the lead-in BEFORE the grid's first beat
 * (every freshly loaded deck parked at 0:00 whose first beat is not exactly at
 * 0) is on the beat before that one, counted back at the first beat's tempo.
 * Null only when the grid has no beats at all.
 */
export function pairingBeatAt(beats: readonly PairingBeat[], positionMs: number): number | null {
	if (beats.length === 0) return null;
	for (let i = beats.length - 1; i >= 0; i -= 1) {
		if (beats[i].t * 1000 <= positionMs) return beats[i].n;
	}
	const first = beats[0];
	const periodMs = first.bpm > 0 ? 60_000 / first.bpm : Number.POSITIVE_INFINITY;
	const beatsBack = Math.max(1, Math.ceil((first.t * 1000 - positionMs) / periodMs));
	const zeroBased = (((first.n - 1 - beatsBack) % BEATS_PER_BAR) + BEATS_PER_BAR) % BEATS_PER_BAR;
	return zeroBased + 1;
}
