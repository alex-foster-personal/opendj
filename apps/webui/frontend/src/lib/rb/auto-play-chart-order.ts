/**
 * Charted AutoPlay order memo and refresh (extracted from auto-play.svelte.ts).
 */

import { DECK_IDS, deckStates, pitchRanges } from '$lib/rb/audio-engine.svelte';
import { autoPlaySongIdentity } from '$lib/rb/auto-play-chain';
import {
	chartedOrderKey,
	getAutoPlayPlaylist,
	getAutoPlayPlaylistRevision,
	pickFollowerDeck,
	pickNextStableId,
	simulateAutoPlayChain,
	tempoBoundsFromPitchRange,
	type AutoPlayDeckSnap
} from '$lib/rb/auto-play';
import { publishAutoPlayOrder } from '$lib/rb/autoplay-queue.svelte';
import { uiPrefs } from '$lib/rb/prefs.svelte';

export const CHARTED_ORDER_HORIZON = 64;

/** PLAY-16: the recordings on the decks now, for rows the playlist cannot name. */
export function deckSongIdentities(): Set<string> {
	const identities = new Set<string>();
	for (const id of DECK_IDS) {
		const deck = deckStates[id];
		if (deck.stable_id === null) continue;
		const identity = autoPlaySongIdentity(deck.title, deck.artist);
		if (identity !== null) identities.add(identity);
	}
	return identities;
}

export function clearChartedAutoPlayOrder(chartKey: { current: string | null }): void {
	chartKey.current = null;
	publishAutoPlayOrder([]);
}

export function refreshChartedAutoPlayOrder(input: {
	source: AutoPlayDeckSnap | null;
	snaps: readonly AutoPlayDeckSnap[];
	excludeIds: ReadonlySet<string>;
	playedFeedEpoch: number;
	playedIds: ReadonlySet<string>;
	chartKey: { current: string | null };
	syncPlayedSet: () => void;
}): void {
	if (!uiPrefs.auto_play_enabled || input.source === null || input.source.stable_id === null) {
		clearChartedAutoPlayOrder(input.chartKey);
		return;
	}
	input.syncPlayedSet();
	const follower = pickFollowerDeck(input.snaps, input.source.id);
	if (follower === null) {
		clearChartedAutoPlayOrder(input.chartKey);
		return;
	}
	const followerPitchRange = pitchRanges[follower];
	const bounds = tempoBoundsFromPitchRange(followerPitchRange);
	const key = chartedOrderKey({
		feed_epoch: input.playedFeedEpoch,
		playlist_revision: getAutoPlayPlaylistRevision(),
		source_stable_id: input.source.stable_id,
		source_key: deckStates[input.source.id].key,
		source_bpm: deckStates[input.source.id].bpm,
		enforce_play_order: uiPrefs.auto_play_enforce_order,
		maximize_reach: !uiPrefs.auto_play_enforce_order && uiPrefs.auto_play_maximize_reach,
		min_tempo_ratio: bounds.min,
		max_tempo_ratio: bounds.max,
		exclude_ids: input.excludeIds,
		played_ids: input.playedIds,
		follower_deck: follower,
		follower_pitch_range: followerPitchRange
	});
	if (key === input.chartKey.current) return;
	input.chartKey.current = key;
	const full = simulateAutoPlayChain({
		select_next: pickNextStableId,
		playlist: getAutoPlayPlaylist(),
		start_stable_id: input.source.stable_id,
		start_key: deckStates[input.source.id].key,
		start_bpm: deckStates[input.source.id].bpm,
		enforce_play_order: uiPrefs.auto_play_enforce_order,
		maximize_reach: !uiPrefs.auto_play_enforce_order && uiPrefs.auto_play_maximize_reach,
		min_tempo_ratio: bounds.min,
		max_tempo_ratio: bounds.max,
		exclude_ids: input.excludeIds,
		played_ids: input.playedIds,
		max_chain_length: CHARTED_ORDER_HORIZON + 1,
		exclude_identities: deckSongIdentities()
	});
	publishAutoPlayOrder(full.slice(1));
}
