/**
 * The state one Rust-mode Beat Sync join leaves behind for the continuous
 * phase lock in `rust-transport.ts`. Types only.
 */
import type { TempoNormalization } from '$lib/rb/beat-sync-math';
import { DECKS, type DeckId } from './rust-link';

/**
 * What a join leaves for the continuous phase lock (`phaseLockTick`): the
 * BASE tempo every trim is relative to, and what the join assumed about the
 * master. A lock whose assumptions no longer hold is dropped, never trimmed.
 */
export interface PhaseLock {
	master: DeckId;
	masterTempo: number;
	stableId: string | null;
	base: number;
	normalization: TempoNormalization;
	/** The tempo the engine was last sent for this follower. */
	sent: number;
	/** A trim or re-seek is in flight; the next tick waits for it. */
	busy: boolean;
	/** Page-clock time of the join: a re-join is never sooner than
	 * PHASE_LOCK_REJOIN_MIN_INTERVAL_SEC after it. */
	joinedAtSec: number;
	/** Consecutive ticks the error has been past the re-join line. */
	overLineTicks: number;
	/** The phase offset the DJ dialed in since the join, wall-clock ms. No Rust
	 * mode control moves a locked follower yet, so this stays 0. */
	userOffsetMs: number;
}

export const phaseLocks: Partial<Record<DeckId, PhaseLock>> = {};

/** A replacement load owns the engine head immediately, even for the same track. */
export function invalidateRustPhaseLocks(deck: DeckId): void {
	for (const follower of DECKS) {
		if (follower === deck || phaseLocks[follower]?.master === deck) delete phaseLocks[follower];
	}
}

/** The locks in force, for tests. */
export function phaseLocksForTest(): Readonly<Partial<Record<DeckId, Readonly<PhaseLock>>>> {
	return phaseLocks;
}
