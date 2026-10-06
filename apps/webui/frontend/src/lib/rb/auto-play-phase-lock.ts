/**
 * AutoPlay's Beat Sync preflight: can these two decks actually phase-lock?
 *
 * Extracted from auto-play.svelte.ts Thu 10 Sep 2026. It is a read-only probe
 * over live deck state with no controller state of its own, so it was the one
 * self-contained block the controller could give up without moving any of its
 * scheduling. The controller was one line under the 600-line file gate, which
 * is a bad place to leave the module that owns the handoff.
 *
 * `phaseLockOk` never mutates transport: it builds the SAME plan the decision
 * below would ask for and reports whether it threw, so a handoff can decline
 * Beat Sync and continue free-tempo instead of failing. The decision itself
 * moved here Thu 10 Sep 2026 to sit beside its own probe, which is also what
 * gave the controller room for PLAY-08's master-handover branch.
 */
import { deckStates, pitchRanges } from '$lib/rb/audio-engine.svelte';
import {
	decideAutoPlayBeatSync,
	formatAutoPlaySyncSkipToast,
	tempoBoundsFromPitchRange,
	type AutoPlayDeckSnap
} from '$lib/rb/auto-play';
import { syncModeForBeatSyncMax } from '$lib/rb/beat-sync-decisions';
import { dispatchPerformanceCommand } from '$lib/rb/performance-ipc.svelte';
import { uiPrefs } from '$lib/rb/prefs.svelte';
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
	const mode = syncModeForBeatSyncMax(uiPrefs.beat_sync_max, deckStates[follower].sync_mode);
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

/**
 * Apply the Beat Sync decision; RETURN the skip notice rather than raising it.
 *
 * The caller pushes the toast. `$lib/stores.svelte` sits at the frontend's
 * measured fan-in ceiling with zero headroom, so a new importer of it reds the
 * quality ratchet for every lane at once - and this module has no other reason
 * to know what a toast is. Returning the message keeps the notice at the call
 * site that already had it.
 */
export async function applyAutoPlayBeatSyncDecision(
	source: AutoPlayDeckSnap,
	follower: DeckId
): Promise<string | null> {
	const probe = source.beat_sync_enabled
		? phaseLockOk(source.id, follower)
		: { ok: false as const, error: 'source Beat Sync off' };
	const decision = decideAutoPlayBeatSync({
		source_beat_sync_enabled: source.beat_sync_enabled,
		phase_lock_ok: probe.ok
	});
	const currentlyOn = deckStates[follower].beat_sync_enabled;
	if (decision === 'enable' && !currentlyOn) {
		await dispatchPerformanceCommand({ type: 'beat_sync', deck: follower, enabled: true }, undefined, 'autoplay-handoff');
	} else if (decision === 'disable' && currentlyOn) {
		await dispatchPerformanceCommand({ type: 'beat_sync', deck: follower, enabled: false }, undefined, 'autoplay-handoff');
	}
	if (!source.beat_sync_enabled || probe.ok) return null;
	const bounds = tempoBoundsFromPitchRange(pitchRanges[follower]);
	return formatAutoPlaySyncSkipToast({
		follower_deck: follower,
		mode: deckStates[follower].sync_mode,
		plan_error: probe.error,
		min_ratio: bounds.min,
		max_ratio: bounds.max
	});
}
