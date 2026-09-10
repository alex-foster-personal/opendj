import { DECK_IDS } from '$lib/player/constants';
import type { DeckId } from '$lib/player/constants';
import type { SyncMode } from '$lib/rb/beat-sync-math';

export function seekSyncMaster(
	deck: DeckId,
	playing: boolean,
	beatSyncEnabled: boolean,
	master: DeckId | null
): DeckId | null {
	return playing && beatSyncEnabled && master !== null && master !== deck ? master : null;
}

export function beatSyncMaxFollowers(
	deck: DeckId,
	beatSyncMax: boolean,
	master: DeckId | null,
	candidates: readonly { id: DeckId; playing: boolean; beatSyncEnabled: boolean }[]
): DeckId[] {
	if (!beatSyncMax || master === null || master !== deck) return [];
	return candidates.filter((candidate) => candidate.id !== deck && candidate.playing && candidate.beatSyncEnabled).map((candidate) => candidate.id);
}

export type SeekSyncPlan =
	| { kind: 'follower'; master: DeckId }
	| { kind: 'master-max'; followers: DeckId[] }
	| { kind: 'free' };

export function planSeekSync(args: {
	deck: DeckId;
	transportActive: boolean;
	beatSyncEnabled: boolean;
	activeMaster: DeckId | null;
	beatSyncMax: boolean;
	candidates: readonly { id: DeckId; playing: boolean; beatSyncEnabled: boolean }[];
}): SeekSyncPlan {
	const master = seekSyncMaster(args.deck, args.transportActive, args.beatSyncEnabled, args.activeMaster);
	if (master !== null) return { kind: 'follower', master };
	const followers = beatSyncMaxFollowers(args.deck, args.beatSyncMax, args.activeMaster, args.candidates);
	if (followers.length > 0) return { kind: 'master-max', followers };
	return { kind: 'free' };
}

export function syncChangeRequiresReschedule(
	deck: DeckId,
	desiredActive: boolean,
	beatSyncEnabled: boolean,
	master: DeckId | null
): boolean {
	if (typeof desiredActive !== 'boolean' || typeof beatSyncEnabled !== 'boolean') {
		throw new TypeError('sync desired-active and Beat Sync flags must be boolean');
	}
	return syncMayWriteTempo(deck, beatSyncEnabled, master) && desiredActive;
}

/** The exclusive tempo-write authority for Beat Sync followers. This pure
 * decision belongs outside the audio graph so every command path shares it. */
export function syncMayWriteTempo(
	deck: DeckId,
	beatSyncEnabled: boolean,
	master: DeckId | null
): boolean {
	if (typeof beatSyncEnabled !== 'boolean') {
		throw new TypeError('sync tempo-write Beat Sync flag must be boolean');
	}
	if (!DECK_IDS.includes(deck) || (master !== null && !DECK_IDS.includes(master))) {
		throw new RangeError(`sync deck ids must be within 1..4, got deck=${deck}, master=${master}`);
	}
	return beatSyncEnabled && master !== null && master !== deck;
}

/** Beat Sync Max advertises BAR downbeat lock. A follower left in BEAT would
 * otherwise nearest-beat lock, so master's n=1 can sit on follower n=2/3/4. */
export function syncModeForBeatSyncMax(
	beatSyncMax: boolean,
	deckSyncMode: SyncMode
): SyncMode {
	if (typeof beatSyncMax !== 'boolean') {
		throw new TypeError(`beatSyncMax must be boolean, got ${typeof beatSyncMax}`);
	}
	if (deckSyncMode !== 'beat' && deckSyncMode !== 'bar') {
		throw new TypeError(`deckSyncMode must be "beat" or "bar", got ${String(deckSyncMode)}`);
	}
	return beatSyncMax ? 'bar' : deckSyncMode;
}
