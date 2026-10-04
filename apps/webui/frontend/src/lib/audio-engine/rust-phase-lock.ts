/** Supersedes phase-lock ownership declarations embedded in rust-transport.ts. */
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

