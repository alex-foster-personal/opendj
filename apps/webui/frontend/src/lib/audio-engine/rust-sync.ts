/**
 * Beat sync, master election and quantize for Rust engine mode (NAE-13).
 *
 * `odj-audio` plays exactly what it is told: tempo ratios, seeks, play and
 * pause. The DECISIONS stay where they already live, in the page's pure
 * modules, so both engines sync, quantize and elect a master by the same
 * rules on the same beatgrids:
 *
 * - phase and tempo: `computeFollowerSyncPlan` (beat-sync-math.ts)
 * - master election: `electMaster` (master-election.ts)
 * - snapping: `quantizedSeekDecisionMs` / `quantizedPositionMs` (loops.ts)
 * - grid trust: `effectiveBeatSync` / `effectiveQuantize` (grid-features.ts)
 *
 * What the Web Audio engine does with a sample-accurate AudioContext schedule,
 * this mode does by sending the planned commands together so the engine
 * applies them in one block, planned `leadSec` ahead to cover the trip.
 *
 * No `$lib` state is read here: callers pass views, so the unit tests drive
 * it with real captured grids and no engine.
 */
import type { AnlzBeat } from '$lib/rb/anlz-types';
import {
	computeFollowerSyncPlan,
	quantizeToNearestBeat,
	type FollowerSyncPlan,
	type SyncMode
} from '$lib/rb/beat-sync-math';
import type { MasterElectionInput } from '$lib/rb/master-election';
import { effectiveBeatSync, effectiveQuantize } from '$lib/player/grid-features';
import { quantizedPositionMs, quantizedSeekDecisionMs } from '$lib/player/transport/loops';
import type { DeckId } from '$lib/rb/deck-slots';
import type { DeckState } from '$lib/rb/deck-state-types';
import type { CrossfaderAssign } from '$lib/rb/mixer-types';

/** How far ahead a join is planned: the socket trip plus one engine block. */
export const SYNC_LEAD_SEC = 0.02;

/**
 * A re-anchor of a follower that is already locked only seeks when its phase
 * is off by more than this. The playhead the page reads is extrapolated from
 * a 30 Hz state feed, so a smaller correction would be a jump chasing the
 * feed's own jitter, audible as a click on every master tempo move.
 */
export const RESEEK_TOLERANCE_MS = 5;

/** One deck as the planner needs it. Positions are the live playhead. */
export interface SyncDeckView {
	beats: readonly AnlzBeat[];
	playing: boolean;
	positionSec: number;
	/** The deck's tempo ratio (engine `tempo`). */
	tempo: number;
}

export interface SyncJoin {
	tempo: number;
	positionMs: number;
	plan: FollowerSyncPlan;
}

/**
 * Tempo and position for a follower so its beat phase matches the master's
 * `leadSec` from now, when the commands land. A stopped follower anchors on
 * its nearest beat, as the Web Audio engine does, so a join starts on a beat.
 */
export function planRustFollowerJoin(
	master: SyncDeckView,
	follower: SyncDeckView,
	options: {
		leadSec: number;
		mode: SyncMode;
		pitchRangePct: number;
		/** Anchor near this follower position instead of its playhead (a seek). */
		followerAtSec?: number;
		/** A user seek: anchor on the beat nearest `followerAtSec` (`anchorOnBeat`). */
		anchorOnBeat?: boolean;
	}
): SyncJoin {
	const lead = options.leadSec;
	const masterAt = master.positionSec + (master.playing ? master.tempo * lead : 0);
	const followerAt =
		options.followerAtSec ??
		(follower.playing
			? follower.positionSec + follower.tempo * lead
			: quantizeToNearestBeat(follower.beats, follower.positionSec));
	const range = options.pitchRangePct / 100;
	const plan = computeFollowerSyncPlan({
		masterGrid: master.beats,
		followerGrid: follower.beats,
		masterPositionAtSyncSec: masterAt,
		masterTempoRatio: master.tempo,
		followerPositionSec: followerAt,
		currentContextTimeSec: 0,
		syncAtContextTimeSec: lead,
		minFollowerTempoRatio: Math.max(0.01, 1 - range),
		maxFollowerTempoRatio: 1 + range,
		mode: options.mode,
		anchorOnBeat: options.anchorOnBeat === true && options.followerAtSec !== undefined
	});
	return { tempo: plan.followerTempoRatio, positionMs: plan.followerPositionSec * 1000, plan };
}

type DeckStateView = Pick<
	DeckState,
	'stable_id' | 'playing' | 'anlz' | 'beat_sync_enabled' | 'quantize_enabled' | 'quantize_grid_beats'
>;

/** `electMaster` input from the page's stores, as the Web Audio engine builds it. */
export function electionInputFrom(
	decks: Record<DeckId, DeckStateView>,
	mixer: {
		crossfader: number;
		master: number;
		channels: Record<DeckId, { fader: number; trim: number; assign: CrossfaderAssign }>;
	}
): MasterElectionInput {
	return {
		crossfader: mixer.crossfader,
		master: mixer.master,
		decks: ([1, 2, 3, 4] as DeckId[]).map((id) => {
			const st = decks[id];
			const ch = mixer.channels[id];
			return {
				id,
				loaded: st.stable_id !== null,
				playing: st.playing,
				beat_sync_enabled: effectiveBeatSync(st),
				fader: ch.fader,
				trim: ch.trim,
				assign: ch.assign
			};
		})
	};
}

function _gridBeats(st: DeckStateView): 1 | 4 | 8 {
	const g = st.quantize_grid_beats;
	if (g === 'phase') {
		throw new Error('quantize grid phase is not implemented and must never reach a snap');
	}
	return g;
}

/** Where a seek to `ms` lands on this deck, and whether it leaves the loop. */
export function rustSeekTarget(
	st: DeckStateView & Pick<DeckState, 'loop'>,
	ms: number
): { targetMs: number; exitLoop: boolean } {
	const beats = effectiveQuantize(st) ? (st.anlz?.beatgrid.beats ?? null) : null;
	return quantizedSeekDecisionMs(beats, ms, beats === null ? 1 : _gridBeats(st), false, st.loop);
}

/** The memory cue a pause or an empty CUE press stores at `positionMs`. */
export function rustCuePoint(st: DeckStateView, positionMs: number): number {
	const on = effectiveQuantize(st);
	const beats = on ? (st.anlz?.beatgrid.beats ?? []) : [];
	return quantizedPositionMs(beats, positionMs, on, on ? _gridBeats(st) : 1);
}

/**
 * The seek position that lands an ARMED jump on its beat although the page
 * timer fired `lateSec` after the downbeat: the target moves on by the music
 * that already played, so the phase is right even when the moment is not.
 */
export function lateJumpPositionMs(targetMs: number, lateSec: number, tempo: number): number {
	return targetMs + Math.max(0, lateSec) * tempo * 1000;
}

/**
 * Whether a re-anchor of an already locked, playing follower must seek, or
 * only needs the new tempo: it compares the planned position with where the
 * follower lands on its own when the commands arrive, at its current tempo.
 */
export function reanchorNeedsSeek(join: SyncJoin, follower: SyncDeckView, leadSec: number): boolean {
	const landsAtMs = (follower.positionSec + follower.tempo * leadSec) * 1000;
	return Math.abs(join.positionMs - landsAtMs) > RESEEK_TOLERANCE_MS;
}
