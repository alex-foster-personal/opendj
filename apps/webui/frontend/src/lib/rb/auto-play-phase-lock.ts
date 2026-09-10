/**
 * AutoPlay's Beat Sync preflight: can these two decks actually phase-lock?
 *
 * Extracted from auto-play.svelte.ts Thu 10 Sep 2026. It is a read-only probe
 * over live deck state with no controller state of its own, so it was the one
 * self-contained block the controller could give up without moving any of its
 * scheduling. The controller was one line under the 600-line file gate, which
 * is a bad place to leave the module that owns the handoff.
 *
 * Never mutates transport: it builds the SAME plan `_applyBeatSyncDecision`
 * would ask for and reports whether it threw, so a handoff can decline Beat
 * Sync and continue free-tempo instead of failing.
 */
import { deckStates, pitchRanges } from '$lib/rb/audio-engine.svelte';
import { tempoBoundsFromPitchRange } from '$lib/rb/auto-play';
import {
	computeFollowerSyncPlan,
	quantizeToNearestBeat,
	validateBeatGrid
} from '$lib/rb/beat-sync-math';
import type { AnlzBeat } from '$lib/rb/anlz-types';
import type { DeckId } from '$lib/rb/deck-slots';

/** Synthetic schedule horizon for preflight only (plan needs syncAt > now). */
const PREFLIGHT_SYNC_AHEAD_SEC = 0.05;

export type PhaseLockProbe = { ok: true } | { ok: false; error: string };

function _gridOrNull(deck: DeckId): readonly AnlzBeat[] | null {
	const beats = deckStates[deck].anlz?.beatgrid.beats;
	try {
		validateBeatGrid(beats ?? []);
	} catch {
		return null;
	}
	return beats ?? null;
}

/** Pure plan probe using live decks; never mutates transport. */
export function phaseLockOk(sourceId: DeckId, follower: DeckId): PhaseLockProbe {
	const masterGrid = _gridOrNull(sourceId);
	const followerGrid = _gridOrNull(follower);
	if (masterGrid === null) {
		return { ok: false, error: `source deck ${sourceId} has no valid real PQTZ beat grid` };
	}
	if (followerGrid === null) {
		return { ok: false, error: `follower deck ${follower} has no valid real PQTZ beat grid` };
	}
	const bounds = tempoBoundsFromPitchRange(pitchRanges[follower]);
	const masterPosSec = Math.max(0, deckStates[sourceId].position_ms / 1000);
	const rawFollowerSec = Math.max(0, deckStates[follower].position_ms / 1000);
	const followerPositionSec = quantizeToNearestBeat(followerGrid, rawFollowerSec);
	const masterTempoRatio = deckStates[sourceId].pitch;
	const mode = deckStates[follower].sync_mode;
	try {
		computeFollowerSyncPlan({
			masterGrid,
			followerGrid,
			masterPositionAtSyncSec: masterPosSec,
			masterTempoRatio,
			followerPositionSec,
			currentContextTimeSec: 0,
			syncAtContextTimeSec: PREFLIGHT_SYNC_AHEAD_SEC,
			minFollowerTempoRatio: bounds.min,
			maxFollowerTempoRatio: bounds.max,
			mode
		});
		return { ok: true };
	} catch (error: unknown) {
		const message = error instanceof Error ? error.message : String(error);
		return { ok: false, error: message };
	}
}
