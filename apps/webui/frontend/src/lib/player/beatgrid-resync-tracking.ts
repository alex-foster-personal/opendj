import type { DeckState } from '$lib/rb/deck-state-types';

type DeckId = DeckState['deck_id'];

export interface BeatgridResyncTracking {
	hasSettledGridless(deck: DeckId): boolean;
	markSettledGridless(deck: DeckId): void;
	markPending(follower: DeckId, master: DeckId): void;
	takePending(master: DeckId): DeckId[];
	hasPendingFollowers(master: DeckId): boolean;
	clearForDeck(deck: DeckId): void;
	clearPendingMembership(follower: DeckId): void;
}

export function createBeatgridResyncTracking(): BeatgridResyncTracking {
	const gridlessSettled = new Set<DeckId>();
	const pendingByMaster = new Map<DeckId, Set<DeckId>>();
	return {
		hasSettledGridless: (deck) => gridlessSettled.has(deck),
		markSettledGridless: (deck) => void gridlessSettled.add(deck),
		markPending: (follower, master) => {
			for (const [key, set] of pendingByMaster) if (key !== master) set.delete(follower);
			const set = pendingByMaster.get(master);
			if (set) set.add(follower);
			else pendingByMaster.set(master, new Set([follower]));
		},
		takePending: (master) => {
			const set = pendingByMaster.get(master);
			pendingByMaster.delete(master);
			return set ? [...set] : [];
		},
		hasPendingFollowers: (master) => (pendingByMaster.get(master)?.size ?? 0) > 0,
		clearForDeck: (deck) => {
			gridlessSettled.delete(deck);
			pendingByMaster.delete(deck);
			for (const set of pendingByMaster.values()) set.delete(deck);
		},
		clearPendingMembership: (follower) => {
			for (const set of pendingByMaster.values()) set.delete(follower);
		}
	};
}
