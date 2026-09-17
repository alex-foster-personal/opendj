/**
 * The live read behind the CUEOUT-15 R6 tempo match: pref plus deck states.
 *
 * Deliberately separate from `preview-beat-sync.ts`, which stays pure and
 * therefore testable with no stores, no audio engine and no DOM. Everything
 * that can be decided rather than observed lives there; this file only
 * observes.
 */

import { DECK_IDS } from '$lib/player/constants';
import { previewSyncRate } from '$lib/player/preview-beat-sync';
import { deckEffectiveBpm, getDeckState } from '$lib/player/state.svelte';
import { uiPrefs } from '$lib/rb/prefs.svelte';

/**
 * Effective BPM of the deck that is BOTH master and playing, else null.
 *
 * Both halves matter: a master deck that is stopped has no pulse for a preview
 * to join, and a playing follower is not the tempo anyone in the room hears.
 */
export function playingMasterBpm(): number | null {
	for (const deck of DECK_IDS) {
		const st = getDeckState(deck);
		if (st.is_master && st.playing) return deckEffectiveBpm(deck);
	}
	return null;
}

/** The rate a preview starting NOW should run at. */
export function previewRateFor(trackBpm: number | null): number {
	return previewSyncRate({
		mode: uiPrefs.preview_beat_sync,
		masterBpm: playingMasterBpm(),
		trackBpm
	}).rate;
}
