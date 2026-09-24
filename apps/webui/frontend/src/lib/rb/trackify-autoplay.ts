/**
 * Pure Trackify single-deck autoplay algebra (PERFMODE-15).
 *
 * Reuses PLAY-04 snapshot semantics via trackify-feed and auto-play-chain pick
 * logic without the two-deck handoff controller in auto-play.svelte.ts.
 */
import type { AutoPlayTrackRow } from '$lib/rb/auto-play-chain';
import { pickNextStableId } from '$lib/rb/auto-play-chain';
import {
	remainingMs,
	tempoBoundsFromPitchRange,
	type AutoPlayDeckSnap
} from '$lib/rb/auto-play';

/** Failed load must quarantine and advance within this wall-time budget. */
export const TRACKIFY_LOAD_SKIP_DEADLINE_MS = 2000;

export const TRACKIFY_DECK_ID = 1 as const;

export type TrackifyDeckSnap = Pick<
	AutoPlayDeckSnap,
	'stable_id' | 'playing' | 'position_ms' | 'duration_ms'
>;

export function pickNextTrackifyCandidate(input: {
	feed: readonly AutoPlayTrackRow[];
	played_ids: ReadonlySet<string>;
	quarantined_ids: ReadonlySet<string>;
	current_id: string | null;
	current_key: string | null;
	current_bpm: number | null;
	enforce_play_order: boolean;
	min_tempo_ratio: number;
	max_tempo_ratio: number;
	maximize_reach: boolean;
}): string | null {
	const anchor = input.current_id ?? '';
	if (anchor === '' && input.feed.length > 0) {
		for (const row of input.feed) {
			if (!row.file_exists) continue;
			if (input.quarantined_ids.has(row.stable_id) || input.played_ids.has(row.stable_id)) continue;
			return row.stable_id;
		}
		return null;
	}
	return pickNextStableId({
		playlist: input.feed,
		current_stable_id: anchor,
		current_key: input.current_key,
		current_bpm: input.current_bpm,
		exclude_ids: input.quarantined_ids,
		played_ids: input.played_ids,
		enforce_play_order: input.enforce_play_order,
		min_tempo_ratio: input.min_tempo_ratio,
		max_tempo_ratio: input.max_tempo_ratio,
		maximize_reach: input.maximize_reach
	});
}

/**
 * How close to the decoded end the transport must be before Trackify treats
 * the track as ended. A float-rounding tolerance, NOT a lead: Trackify has one
 * deck, so advancing unloads the track that is playing, and any lead here
 * would cut that much audible audio off the end of every song.
 */
export const TRACKIFY_END_OF_TRACK_TOLERANCE_MS = 1;

/**
 * True once per track, when that track has actually ended.
 *
 * Deliberately NOT the two-deck AutoPlay trigger window
 * (`effectiveAutoPlayThresholdMs`, up to 16 s before the end): that window
 * exists to PREPARE a second deck while the first keeps playing. Trackify's
 * advance replaces the only deck, so it may fire only at the end:
 * - playing: the audio clock has reached the decoded end. Everything left is
 *   already rendered and in the output path, and the engine's unload waits
 *   for the deck to go inaudible before it replaces it;
 * - stopped: the transport is parked AT the decoded end, which is where the
 *   engine's natural-end stop leaves it (and where a background tab's audio
 *   clock stops projecting).
 * A paused deck anywhere before its end never advances, so Pause keeps
 * playback stopped however close to the end the operator pressed it.
 */
export function shouldAdvanceTrackify(input: {
	enabled: boolean;
	deck: TrackifyDeckSnap;
	already_triggered_for: string | null;
	in_flight: boolean;
}): boolean {
	if (!input.enabled || input.in_flight) return false;
	if (input.deck.stable_id === null) return false;
	if (input.already_triggered_for === input.deck.stable_id) return false;
	const remaining = remainingMs(input.deck.position_ms, input.deck.duration_ms);
	if (remaining === null) return false;
	return remaining <= TRACKIFY_END_OF_TRACK_TOLERANCE_MS;
}

export function tempoBoundsForTrackify(pitchRangePct: number): { min: number; max: number } {
	return tempoBoundsFromPitchRange(pitchRangePct);
}
