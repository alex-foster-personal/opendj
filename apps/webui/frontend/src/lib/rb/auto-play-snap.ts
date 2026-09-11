import { autoPlayExcludedIds, type AutoPlayDeckSnap } from '$lib/rb/auto-play';

/** The deck fields a snapshot copies verbatim from the engine's deck state. */
export interface AutoPlayDeckRead {
	stable_id: string | null;
	playing: boolean;
	duration_ms: number | null;
	is_master: boolean;
	beat_sync_enabled: boolean;
}

/**
 * Build the per-tick deck snapshots AutoPlay reasons over.
 *
 * The engine reads are injected rather than imported so this stays a pure
 * function of its inputs: the controller owns the engine, this owns the shape.
 */
export function autoPlayDeckSnaps(
	ids: readonly AutoPlayDeckSnap['id'][],
	readDeck: (id: AutoPlayDeckSnap['id']) => AutoPlayDeckRead,
	readClockPositionMs: (id: AutoPlayDeckSnap['id']) => number
): AutoPlayDeckSnap[] {
	return ids.map((id) => {
		const d = readDeck(id);
		return {
			id,
			stable_id: d.stable_id,
			playing: d.playing,
			// Audio clock, NOT d.position_ms: that mirror is published from
			// requestAnimationFrame, which the browser stops in a background tab,
			// and a frozen position never reaches AUTO_PLAY_THRESHOLD_MS. Reading
			// the clock is what lets a set keep mixing while the user is on
			// another tab. This poll is a setInterval, which a tab playing audio
			// keeps running (throttled to ~1s), leaving ~16 chances inside the
			// 16s window.
			position_ms: readClockPositionMs(id),
			duration_ms: d.duration_ms,
			is_master: d.is_master,
			beat_sync_enabled: d.beat_sync_enabled
		};
	});
}

/**
 * Ids the source deck must not hand off to: everything already claimed by a
 * deck, plus the quarantine of candidates that failed to load or play.
 */
export function autoPlayExcludeIds(
	sourceId: AutoPlayDeckSnap['id'],
	snaps: readonly AutoPlayDeckSnap[],
	claimedIds: ReadonlySet<string>,
	unplayableIds: ReadonlySet<string>
): Set<string> {
	return autoPlayExcludedIds({
		decks: snaps,
		source_deck: sourceId,
		claimed_ids: claimedIds,
		unplayable_ids: unplayableIds
	});
}
