/**
 * Pure Trackify single-deck autoplay algebra (PERFMODE-15).
 *
 * Reuses PLAY-04 snapshot semantics via trackify-feed and auto-play-chain pick
 * logic without the two-deck handoff controller in auto-play.svelte.ts.
 */
import type { AutoPlayTrackRow } from '$lib/rb/auto-play-chain';
import { pickNextStableId } from '$lib/rb/auto-play-chain';
import {
	AUTO_PLAY_THRESHOLD_MS,
	effectiveAutoPlayThresholdMs,
	remainingMs,
	shouldTriggerAutoPlay,
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

export function shouldAdvanceTrackify(input: {
	enabled: boolean;
	deck: TrackifyDeckSnap;
	already_triggered_for: string | null;
	in_flight: boolean;
}): boolean {
	const remaining = remainingMs(input.deck.position_ms, input.deck.duration_ms);
	const threshold = effectiveAutoPlayThresholdMs(input.deck.duration_ms);
	return shouldTriggerAutoPlay({
		enabled: input.enabled,
		remaining_ms: remaining,
		threshold_ms: threshold ?? AUTO_PLAY_THRESHOLD_MS,
		source_stable_id: input.deck.stable_id,
		already_triggered_for: input.already_triggered_for,
		in_flight: input.in_flight
	});
}

export function tempoBoundsForTrackify(pitchRangePct: number): { min: number; max: number } {
	return tempoBoundsFromPitchRange(pitchRangePct);
}
