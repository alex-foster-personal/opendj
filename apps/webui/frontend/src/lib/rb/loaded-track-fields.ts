/**
 * Track-scoped DeckState fields a load or unload resets, split from audio-engine.svelte.ts
 * `_clearLoadedTrackState` (#8581 size ratchet). Engine-runtime (`_rt`) fields, `stems` and `has_rb_mapping`
 * stay in the engine (hot-cue-mapping-gate.test.mjs pins the last one there). Type-only imports.
 */
import type { DeckState } from './deck-state-types';

export function clearLoadedTrackFields(st: DeckState): void {
	st.stable_id = null;
	st.edit_id = null;
	st.edit_in_ms = null;
	st.edit_out_ms = null;
	st.source_path = null;
	st.title = null;
	st.artist = null;
	st.rating = null;
	st.bpm = null;
	st.key = null;
	st.key_guess = null;
	st.key_source = null;
	st.key_shift_semitones = 0;
	st.key_sync_enabled = false;
	st.duration_ms = null;
	st.position_ms = 0;
	st.transport_pending = false;
	st.cue_ms = null;
	st.pitch = 1;
	st.slip_enabled = false;
	st.slip_active = false;
	st.slip_position_ms = null;
	st.loop = null;
	st.pending_loop_in_ms = null; // DECKUX-10478
	st.last_loop = null; // DECKUX-10478
	st.hot_cues = [];
	st.anlz = null;
	st.warp_map = null;
	st.anlz_error = null;
}
