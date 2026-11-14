/**
 * Edge sampler: read a deck's engaged loop and mix state into a capture request
 * ((private path) section 3.2; sampler_capture on the bus, the loop drag later).
 * Thin state reader; the mapping is pure in sampler-capture-vector.ts.
 *
 * Requirements (mini-PRD):
 *   ✔︎ a deck with no engaged loop or no track throws instead of capturing nothing
 *     [if] deck 2 has no loop [then ⛔️ "Deck 2 has no engaged loop"]
 */

import { getDeckState, mixerState } from '$lib/player/state.svelte';
import type { DeckId } from '$lib/rb/deck-id';
import { loopBeatCount, vectorFromDeckMix } from './sampler-capture-vector';
import type { CaptureRequest } from './sampler-types';

/** The capture request for a deck's engaged loop, its EQ, filter, stems and key as the vector. */
export function captureDeckLoop(deck: number): Omit<CaptureRequest, 'set_id' | 'index'> {
	if (deck !== 1 && deck !== 2 && deck !== 3 && deck !== 4)
		throw new Error(`Deck ${deck} does not exist`);
	const id = deck as DeckId;
	const st = getDeckState(id);
	const loop = st.loop;
	if (loop === null || !loop.engaged) throw new Error(`Deck ${deck} has no engaged loop`);
	if (st.stable_id === null) throw new Error(`Deck ${deck} has no track loaded`);
	const ch = mixerState.channels[id];
	const stems =
		st.stems.status === 'ready'
			? {
					controls: st.stems.controls,
					hasInstrumental: st.stems.available_controls.includes('instrumental')
				}
			: null;
	return {
		stable_id: st.stable_id,
		in_ms: Math.round(loop.in_ms),
		out_ms: Math.round(loop.out_ms),
		beats: loopBeatCount(loop.in_ms, loop.out_ms, loop.beat_length, st.bpm),
		vector: vectorFromDeckMix({
			trimKnob: ch.trim,
			eqKnobs: { low: ch.eq_low, mid: ch.eq_mid, high: ch.eq_high },
			filterKnob: ch.filter,
			stems,
			keyShiftSt: st.key_shift_semitones,
			masterTempo: st.master_tempo_enabled
		})
	};
}
