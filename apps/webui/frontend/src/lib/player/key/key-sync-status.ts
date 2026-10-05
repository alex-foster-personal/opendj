/**
 * KEY SYNC status (DECKUX-34): whether an armed deck is ACTUALLY following a
 * master right now, derived from published deck state and never stored.
 *
 * `key_sync_enabled` is the operator's arm. It can outlive the master it was
 * armed against (restore before any master exists, master handed off, master
 * reloaded, this deck elected master). The button must light only while the
 * follow really holds, so the UI and IPC read this status, not the arm.
 * Deriving it from the same fields every time means it cannot drift from what
 * is loaded and which deck is master.
 */
import { DECK_IDS } from '$lib/player/constants';
import { parseCamelotKey } from '$lib/player/key/camelot';
import type { DeckId } from '$lib/rb/deck-id';

export type KeySyncStatus =
	| 'off'
	| 'following'
	| 'waiting-no-master'
	| 'waiting-is-master'
	| 'waiting-no-key';

export interface KeySyncStatusDeck {
	stable_id: string | null;
	key: string | null;
	key_sync_enabled: boolean;
	is_master: boolean;
}

export function deriveKeySyncStatus(
	deck: DeckId,
	decks: Readonly<Record<DeckId, KeySyncStatusDeck>>
): KeySyncStatus {
	if (!DECK_IDS.includes(deck)) throw new RangeError(`KEY SYNC deck must be within 1..4, got ${deck}`);
	const source = decks[deck];
	if (!source.key_sync_enabled) return 'off';
	if (source.is_master) return 'waiting-is-master';
	const masterDeck = DECK_IDS.find((candidate) => decks[candidate].is_master);
	if (masterDeck === undefined || decks[masterDeck].stable_id === null) return 'waiting-no-master';
	const master = decks[masterDeck];
	if (source.stable_id === null || parseCamelotKey(source.key) === null || parseCamelotKey(master.key) === null) {
		return 'waiting-no-key';
	}
	return 'following';
}

/** Hover copy for each status, shared by the deck header and agents. */
export function keySyncStatusTitle(status: Exclude<KeySyncStatus, 'off' | 'following'>): string {
	switch (status) {
		case 'waiting-no-master':
			return 'KEY SYNC ARMED - not following: no loaded master deck yet. Applies when a master is selected.';
		case 'waiting-is-master':
			return 'KEY SYNC ARMED - not following: this deck is the master. Applies again when another deck is master.';
		case 'waiting-no-key':
			return 'KEY SYNC ARMED - not following: this deck or the master has no Camelot key.';
		default: {
			const _exhaustive: never = status;
			throw new Error(`Unhandled KEY SYNC status: ${String(_exhaustive)}`);
		}
	}
}
