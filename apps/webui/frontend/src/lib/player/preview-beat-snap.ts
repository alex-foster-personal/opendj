/**
 * Phase snap for the library preview voice (PVSNAP spike, Thu 8 Oct 2026).
 *
 * the maintainer, Thu 8 Oct 2026: "Preview waveform clicks (scrubbing to listen in
 * headphone cue) automatically snapping to correct place in 4-beat with same
 * tempo, MT, downbeat matched, as if played on deck - this can 'catch up' if
 * it can't be processed fast enough."
 *
 * `preview-beat-sync.ts` (CUEOUT-15 R6) already matches the TEMPO. This is the
 * phase half: where in the clicked track to start, and at which audio-clock
 * instant, so the preview's beats land on the master's beats and its bar on
 * the master's bar. It is the same answer a Beat Sync follower gets when the
 * operator clicks its waveform (`computeFollowerSyncPlan` with
 * `anchorOnBeat`), so a preview and a deck can never disagree about where
 * "in phase" is.
 *
 * Catch-up is free in this shape: the plan is a function of the instant the
 * preview will START, not the instant it was clicked. A decode that finishes
 * late is planned for the later instant, so it still lands on the clicked
 * bar, in phase; it simply starts later.
 *
 * Mini-PRD:
 * - R1 A click lands on the beat nearest the click that carries the master's
 *   CURRENT beat number (BAR), at the master's beat fraction, so the preview's
 *   downbeats sound on the master's downbeats.
 *   - [if] master is on beat 3 at 40% through it and the click is near a
 *     preview downbeat [then ⛔️] the preview starts 40% into the nearest
 *     beat-3 to the click
 * - R2 The tempo is the master's local grid tempo, inside the preview pitch
 *   range, half and double time included (the deck rules, unchanged).
 *   - [if] the only fit is outside the range [then ⛔️] a refusal naming why,
 *     and the caller plays the click unsnapped
 * - R3 A late start is planned for its own instant, never for the click's.
 *   - [if] the same click is planned 0.7 s later [then ⛔️] the preview is
 *     still in phase at its start and still on the clicked bar
 * - R4 A click before the first beat or after the last snaps to the grid's
 *   edge rather than refusing, because an intro click means "from the top".
 * - R5 No grid, a bad grid, or an extrapolated anchor under BAR is a refusal
 *   with the planner's own reason, never a guess.
 *
 * Wiring (CUEOUT-6060, Tue 13 Oct 2026): `previewStartFor` below is the whole
 * decision the click path makes, so `preview-cue.svelte.ts` only gathers its
 * inputs (`preview-beat-snap-live.ts`) and obeys the answer.
 * - R6 With `preview_beat_sync` 'phase' and a playing master, the click
 *   starts snapped; any refusal falls back to the CUEOUT-15 R6 tempo match at
 *   the click point, so a preview is never withheld for want of a grid.
 *   - [if] the preview track has no grid [then ⛔️] it plays from the click at
 *     the tempo-match rate, with the refusal reason on hand
 * - R7 With 'tempo' or 'off', nothing moves: the start is the click and the
 *   instant is now.
 *
 * Pure. No audio nodes, no stores, no DOM.
 */

import type { AnlzBeat } from '$lib/rb/anlz-types';
import { tempoBoundsFromPitchRange } from '$lib/rb/auto-play';
import {
	computeFollowerSyncPlan,
	type BeatNumber,
	type SyncMode,
	type TempoNormalization
} from '$lib/rb/beat-sync-math';
import { previewSyncRate, type PreviewBeatSync } from '$lib/player/preview-beat-sync';

/**
 * Lead between "decided" and "sounding" for a preview with its PCM in hand.
 *
 * Enough for the main thread to create and start a source node before the
 * render quantum it was scheduled for has gone (128 frames is 2.9 ms at
 * 44.1 kHz; a busy main thread is the real cost). Measured by the harness,
 * not assumed: see `tests/live/preview-beat-snap-harness.mjs`.
 */
export const PREVIEW_SNAP_LEAD_SEC = 0.05;

export interface PreviewSnapRequest {
	masterGrid: readonly AnlzBeat[];
	previewGrid: readonly AnlzBeat[];
	/** Master track position projected to `startAtContextSec`. */
	masterPositionAtStartSec: number;
	/** Master playbackRate (deck pitch) at that instant. */
	masterTempoRatio: number;
	/** Track second the operator clicked on the preview's strip. */
	clickSec: number;
	nowContextSec: number;
	/** Audio-clock instant the preview will start. Must be after now. */
	startAtContextSec: number;
	/** Preview pitch range, percent (16 matches the deck default). */
	pitchRangePct: number;
	/** 'bar' keeps downbeats on downbeats; 'beat' takes the nearest beat. */
	mode: SyncMode;
}

export type PreviewSnapPlan =
	| {
			ok: true;
			startAtContextSec: number;
			/** Where the source starts reading, in preview track seconds. */
			offsetSec: number;
			/** `playbackRate` for the source. */
			rate: number;
			masterBeatNumber: BeatNumber;
			/** Fraction through the master's beat at the start, [0, 1). */
			beatPhase: number;
			/** Preview grid index the start sits in. */
			previewBeatIndex: number;
			/** True when the click was outside the grid and snapped to its edge. */
			clamped: boolean;
			/** 1 when the bars match one to one. 0.5 or 2 when the master and the
			 * preview lock only at half or double tempo, so the bar counts differ
			 * and the start is phase-aligned, not downbeat-aligned. */
			tempoNormalization: TempoNormalization;
	  }
	| { ok: false; reason: string };

/**
 * Keep a click inside the span the planner accepts: [first beat, last beat).
 * A click in a pickup or intro means "from the top", so it snaps to the
 * first beat rather than refusing.
 */
function _clampToGrid(grid: readonly AnlzBeat[], sec: number): { sec: number; clamped: boolean } {
	if (grid.length < 2) return { sec, clamped: false };
	const first = grid[0].t;
	// Just inside the last interval: the planner needs an enclosing interval.
	const last = grid[grid.length - 2].t;
	if (sec < first) return { sec: first, clamped: true };
	if (sec > last) return { sec: last, clamped: true };
	return { sec, clamped: false };
}

/**
 * Where and when a preview click should start so it sounds as if a synced deck
 * had been sent there. Returns a refusal (never throws) so the click path can
 * fall back to a plain preview with the reason on hand.
 */
export function planPreviewSnap(request: PreviewSnapRequest): PreviewSnapPlan {
	const { sec: clickSec, clamped } = _clampToGrid(request.previewGrid, request.clickSec);
	try {
		const bounds = tempoBoundsFromPitchRange(request.pitchRangePct);
		const plan = computeFollowerSyncPlan({
			masterGrid: request.masterGrid,
			followerGrid: request.previewGrid,
			masterPositionAtSyncSec: request.masterPositionAtStartSec,
			masterTempoRatio: request.masterTempoRatio,
			followerPositionSec: clickSec,
			currentContextTimeSec: request.nowContextSec,
			syncAtContextTimeSec: request.startAtContextSec,
			minFollowerTempoRatio: bounds.min,
			maxFollowerTempoRatio: bounds.max,
			mode: request.mode,
			anchorOnBeat: true
		});
		return {
			ok: true,
			startAtContextSec: request.startAtContextSec,
			offsetSec: plan.followerPositionSec,
			rate: plan.followerTempoRatio,
			masterBeatNumber: plan.masterBeatNumber,
			beatPhase: plan.beatPhase,
			previewBeatIndex: plan.followerBeatIndex,
			clamped,
			tempoNormalization: plan.tempoNormalization
		};
	} catch (error: unknown) {
		return { ok: false, reason: error instanceof Error ? error.message : String(error) };
	}
}

/** The playing master as the click path sees it at `nowContextSec`. */
export interface PreviewSnapMaster {
	grid: readonly AnlzBeat[];
	/** Master track position at the preview's start instant, read from the
	 * master's own transport schedule (`deckMasterAtContextSec`). */
	positionAtStartSec: number;
	/** Master playbackRate (deck pitch) at the start instant. */
	tempoRatio: number;
}

export interface PreviewStartRequest {
	mode: PreviewBeatSync;
	/** Effective BPM of the playing master, for the tempo match; null when
	 * none plays. Separate from `master`, which also needs a usable grid. */
	masterBpm: number | null;
	/** Null when there is nothing to snap to. */
	master: PreviewSnapMaster | null;
	/** Why there is no master to snap to, when the caller knows better than
	 * "none playing" (the Rust engine mode runs on another clock). */
	masterRefusal?: string | undefined;
	/** Null when the clicked track's grid could not be read. */
	previewGrid: readonly AnlzBeat[] | null;
	/** The library row's BPM, for the tempo-match fallback. */
	trackBpm: number | null;
	clickSec: number;
	nowContextSec: number;
	pitchRangePct: number;
}

export interface PreviewStart {
	/** Audio-clock instant for `source.start`; 0 means "now". */
	startAtContextSec: number;
	offsetSec: number;
	rate: number;
	/** The phase plan when the start snapped, else null. */
	snap: Extract<PreviewSnapPlan, { ok: true }> | null;
	/** Why a 'phase' preview did not snap; null when it did, or was not asked to. */
	snapRefusal: string | null;
}

/**
 * Where, when and how fast a preview click starts. Never throws and never
 * refuses: a snap that cannot be planned degrades to the tempo match.
 */
export function previewStartFor(request: PreviewStartRequest): PreviewStart {
	const fallbackRate = previewSyncRate({
		mode: request.mode,
		masterBpm: request.masterBpm,
		trackBpm: request.trackBpm
	}).rate;
	const plain = (snapRefusal: string | null): PreviewStart => ({
		startAtContextSec: 0,
		offsetSec: request.clickSec,
		rate: fallbackRate,
		snap: null,
		snapRefusal
	});
	if (request.mode !== 'phase') return plain(null);
	if (request.master === null) {
		return plain(request.masterRefusal ?? 'no master deck is playing');
	}
	if (request.previewGrid === null) return plain('the previewed track has no beat grid to snap with');
	const startAt = request.nowContextSec + PREVIEW_SNAP_LEAD_SEC;
	const plan = planPreviewSnap({
		masterGrid: request.master.grid,
		previewGrid: request.previewGrid,
		masterPositionAtStartSec: request.master.positionAtStartSec,
		masterTempoRatio: request.master.tempoRatio,
		clickSec: request.clickSec,
		nowContextSec: request.nowContextSec,
		startAtContextSec: startAt,
		pitchRangePct: request.pitchRangePct,
		mode: 'bar'
	});
	if (!plan.ok) return plain(plan.reason);
	return {
		startAtContextSec: plan.startAtContextSec,
		offsetSec: plan.offsetSec,
		rate: plan.rate,
		snap: plan,
		snapRefusal: null
	};
}
