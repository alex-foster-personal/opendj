/**
 * PLAY-05 user-viewable state for the frozen AutoPlay handoff plan.
 *
 * This module owns only reactive presentation state. The source of truth for
 * candidates remains auto-play.ts's activation snapshot, and the controller
 * supplies its exact simulated handoff chain on every material plan change.
 */
import {
	getAutoPlayPlaylist,
	queueEntriesForChain,
	type AutoPlayQueueEntry
} from '$lib/rb/auto-play';

/** Read-only charted AutoPlay order for the open playlist (library column). */
export const autoPlayOrder = $state<{
	chain: readonly string[];
	rankOf: ReadonlyMap<string, number>;
}>({ chain: [], rankOf: new Map() });

export const autoPlayQueue = $state<{
	active: boolean;
	entries: readonly AutoPlayQueueEntry[];
}>({ active: false, entries: [] });

export function activateAutoPlayQueue(): void {
	autoPlayQueue.active = true;
	autoPlayQueue.entries = [];
}

function _publishQueue(chain: readonly string[]): void {
	autoPlayQueue.entries = queueEntriesForChain(chain, getAutoPlayPlaylist());
}

/** Publish a new charted order. An unchanged chain is a no-op, so a 250 ms
 * poll that re-derives the same plan does not churn reactive readers. */
export function publishAutoPlayOrder(chain: readonly string[]): void {
	if (
		chain.length === autoPlayOrder.chain.length &&
		chain.every((id, i) => id === autoPlayOrder.chain[i])
	) {
		return;
	}
	autoPlayOrder.chain = chain;
	autoPlayOrder.rankOf = new Map(chain.map((id, i) => [id, i + 1]));
	_publishQueue(chain);
}

/** Clear presentation state without making an arm effect depend on queue writes. */
export function clearAutoPlayOrder(): void {
	autoPlayOrder.chain = [];
	autoPlayOrder.rankOf = new Map();
	_publishQueue([]);
}

export function clearAutoPlayQueue(): void {
	autoPlayQueue.active = false;
	autoPlayQueue.entries = [];
}
