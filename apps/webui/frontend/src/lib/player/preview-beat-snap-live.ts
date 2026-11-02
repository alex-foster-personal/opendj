/**
 * The live read behind the CUEOUT-6060 preview snap: the playing master's grid,
 * audio-clock position and pitch, observed at one context instant. It
 * also carries the master BPM the CUEOUT-15 R6 tempo match reads, which used
 * to live in `preview-beat-sync-live.ts`.
 *
 * Kept apart from `preview-beat-snap.ts` for the same reason
 * the CUEOUT-15 R6 read was kept apart from its pure half: everything
 * that can be decided is decided there, under unit test; this file only
 * observes.
 */

import { rustMode } from '$lib/audio-engine/rust-mode.svelte';
import { DECK_IDS } from '$lib/player/constants';
import { PREVIEW_SNAP_LEAD_SEC, type PreviewSnapMaster } from '$lib/player/preview-beat-snap';
import { deckEffectiveBpm, getDeckState } from '$lib/player/state.svelte';
import { validateBeatGrid } from '$lib/rb/beat-sync-math';
import { audioContextNowSec, deckMasterAtContextSec } from '$lib/rb/audio-engine.svelte';

/**
 * The deck that is BOTH master and playing, read off the audio clock, or a
 * reason there is nothing to snap to.
 *
 * The position is read from the master's transport schedule at the preview's
 * start instant, on the same AudioContext the preview starts on, not from the
 * rAF-published mirror.
 */
export interface PreviewMasterRead {
	/** Effective BPM of the master, for the CUEOUT-15 R6 tempo match. Both
	 * halves matter: a stopped master has no pulse to join, and a playing
	 * follower is not the tempo the room hears. */
	masterBpm: number | null;
	/** What to snap to, or null with `refusal` saying why. */
	master: PreviewSnapMaster | null;
	refusal: string | undefined;
}

export function previewMasterRead(): PreviewMasterRead {
	for (const deck of DECK_IDS) {
		const st = getDeckState(deck);
		if (!(st.is_master && st.playing)) continue;
		const masterBpm = deckEffectiveBpm(deck);
		// The Rust engine's decks run on the engine's own clock, which this
		// page's AudioContext cannot schedule against.
		if (rustMode.enabled) {
			return { masterBpm, master: null, refusal: 'the Rust engine mode decks run on another clock' };
		}
		const grid = st.anlz?.beatgrid.beats ?? [];
		try {
			validateBeatGrid(grid);
		} catch (error) {
			const refusal = `the master deck ${deck} has no usable beat grid (${String(error)})`;
			return { masterBpm, master: null, refusal };
		}
		const nowSec = audioContextNowSec();
		if (nowSec === null) {
			return { masterBpm, master: null, refusal: 'the audio graph is not running' };
		}
		// The start instant is the one the preview will be scheduled at, so the
		// master is read there, through its own schedule.
		const at = deckMasterAtContextSec(deck, nowSec + PREVIEW_SNAP_LEAD_SEC);
		return {
			masterBpm,
			master: { grid, positionAtStartSec: at.positionSec, tempoRatio: at.tempoRatio },
			refusal: undefined
		};
	}
	return { masterBpm: null, master: null, refusal: 'no master deck is playing' };
}
