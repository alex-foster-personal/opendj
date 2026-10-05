/**
 * Pure rescue beat-stamp math (RESCUE-02). No DOM or audio imports.
 */

import type { DeckId } from '$lib/rb/deck-id';
import type { RescueBeatStamp } from '$lib/rb/rescue-beat-stamp';
import type { RescueSnapshot } from '$lib/rb/rescue-snapshot';

export const RESCUE_DECODE_CEILING_MS = 20_000;
export const RESCUE_PLAYBACK_ELIGIBLE_MS = 10 * 60 * 1000;

export interface RescueResumeTarget {
	deck: DeckId;
	target_position_ms: number;
	target_beat: number;
}

export interface RescueResumeException {
	deck: DeckId;
	reason: string;
}

export function playbackEligible(snapshot: RescueSnapshot, nowMs: number): boolean {
	return nowMs - snapshot.captured_at_ms <= RESCUE_PLAYBACK_ELIGIBLE_MS;
}

/** Continuous beat index from track start using PQTZ beat times in ms. */
export function msToBeatPosition(beatgridMs: readonly number[], positionMs: number): number | null {
	if (beatgridMs.length < 2) return null;
	if (!Number.isFinite(positionMs) || positionMs < 0) return null;
	const positionSec = positionMs / 1000;
	const beatTimesSec = beatgridMs.map((ms) => ms / 1000);
	const first = beatTimesSec[0];
	const last = beatTimesSec[beatTimesSec.length - 1];
	if (positionSec < first) return 0;
	if (positionSec >= last) return beatTimesSec.length - 1;

	let lo = 0;
	let hi = beatTimesSec.length - 1;
	while (lo < hi - 1) {
		const mid = (lo + hi) >> 1;
		if (beatTimesSec[mid] <= positionSec) lo = mid;
		else hi = mid;
	}
	const startSec = beatTimesSec[lo];
	const endSec = beatTimesSec[hi];
	const fraction = endSec > startSec ? (positionSec - startSec) / (endSec - startSec) : 0;
	return lo + fraction;
}

/** Invert {@link msToBeatPosition} with linear interpolation between beats. */
export function beatPositionToMs(beatgridMs: readonly number[], beat: number): number | null {
	if (beatgridMs.length < 2) return null;
	if (!Number.isFinite(beat) || beat < 0) return null;
	const whole = Math.floor(beat);
	const fraction = beat - whole;
	if (whole >= beatgridMs.length - 1) {
		const last = beatgridMs[beatgridMs.length - 1];
		return Number.isFinite(last) ? last : null;
	}
	const startMs = beatgridMs[whole];
	const endMs = beatgridMs[whole + 1];
	if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || endMs < startMs) return null;
	return startMs + fraction * (endMs - startMs);
}

function _effectiveBpmFromGrid(beatgridMs: readonly number[]): number | null {
	if (beatgridMs.length < 2) return null;
	const intervalMs = beatgridMs[1] - beatgridMs[0];
	if (!(intervalMs > 0)) return null;
	return 60_000 / intervalMs;
}

export function advanceBeatStamp(
	stamp: RescueBeatStamp,
	beatgridMs: readonly number[],
	pitch: number,
	elapsedWallMs: number
): number {
	if (stamp.kind === 'sample') {
		return stamp.position_ms + elapsedWallMs * pitch;
	}
	const elapsedSec = elapsedWallMs / 1000;
	const bpm = _effectiveBpmFromGrid(beatgridMs) ?? 128;
	const elapsedBeats = elapsedSec * (bpm / 60) * pitch;
	return stamp.beat_index + elapsedBeats;
}

export function computeResumeTarget(
	deck: DeckId,
	stamp: RescueBeatStamp,
	beatgridMs: readonly number[],
	pitch: number,
	elapsedWallMs: number
): RescueResumeTarget | RescueResumeException {
	if (stamp.kind === 'sample') {
		const targetMs = Math.round(stamp.position_ms + elapsedWallMs * pitch);
		return { deck, target_position_ms: targetMs, target_beat: 0 };
	}
	if (beatgridMs.length === 0) {
		return { deck, reason: 'no beatgrid' };
	}
	const targetBeat = advanceBeatStamp(stamp, beatgridMs, pitch, elapsedWallMs);
	const targetMs = beatPositionToMs(beatgridMs, targetBeat);
	if (targetMs === null || !Number.isFinite(targetMs)) {
		return { deck, reason: 'beatgrid conversion failed' };
	}
	return { deck, target_position_ms: Math.round(targetMs), target_beat: targetBeat };
}

export function gigPlaybackEligible(snapshot: RescueSnapshot, nowMs: number): boolean {
	return snapshot.app_posture === 'gig' && playbackEligible(snapshot, nowMs);
}

export function formatElapsedAgo(elapsedMs: number): string {
	const totalSec = Math.max(0, Math.floor(elapsedMs / 1000));
	const minutes = Math.floor(totalSec / 60);
	const seconds = totalSec % 60;
	return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
}

export function deckDecodedForRescue(input: {
	stable_id: string | null;
	expected_stable_id: string;
	duration_ms: number | null;
	processor_error: string | null;
}): boolean {
	if (input.stable_id !== input.expected_stable_id) return false;
	if (input.duration_ms === null || !Number.isFinite(input.duration_ms) || input.duration_ms <= 0) {
		return false;
	}
	return input.processor_error === null;
}

/**
 * RESCUE-06: the deck that is master once a rescue resume has started.
 *
 * The rescued decks are started by one shared schedule, which bypasses the
 * play-claim election a plain play runs, so without this the reloaded page
 * plays with NO master. AutoPlay only ever arms off the playing master, so on
 * Mon 5 Oct 2026 (silver preview, 19:19Z) both rescued decks played to their
 * ends with AutoPlay on and nothing was queued after them.
 *
 * The snapshot's own master wins when it was resumed; otherwise the first
 * resumed deck, so a rescued set always has a master.
 */
export function rescueResumeMasterDeck(
	snapshotMaster: DeckId | null,
	resumedDecks: readonly DeckId[]
): DeckId {
	const first = resumedDecks[0];
	if (first === undefined) {
		throw new RangeError('rescueResumeMasterDeck requires at least one resumed deck');
	}
	if (snapshotMaster !== null && resumedDecks.includes(snapshotMaster)) return snapshotMaster;
	return first;
}
