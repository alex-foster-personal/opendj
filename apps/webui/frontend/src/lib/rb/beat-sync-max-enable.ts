/**
 * Beat Sync Max turns Beat Sync ON (DECKUX-37).
 *
 * the maintainer, Tue 6 Oct 2026, /performance: "beatsyncMax is on but each deck is not
 * turning on beatsync - why?" Max only forced BAR mode on decks whose own Beat
 * Sync was already on, so with every deck off it did nothing visible.
 *
 * The rules (CORE decision, logged for the maintainer's review):
 *   1. Max OFF -> ON enables Beat Sync on every deck with a track loaded, and
 *      on each deck as a track loads while Max is on.
 *   2. Max ON -> OFF changes no deck, so nothing unsyncs mid-mix.
 *   3. A deck's own SYNC button still turns that deck off while Max is on;
 *      Max re-enables it only on that deck's NEXT load.
 *   4. Every enable is the ordinary `beat_sync` command, the one the deck SYNC
 *      button sends, so an agent sees the same commands a press makes.
 *
 * This module is a leaf on purpose: prefs (first paint) announces the toggle,
 * performance-ipc (the /performance command session) installs the handler.
 */
import { DECK_IDS } from '$lib/player/constants';
import type { DeckId } from '$lib/player/constants';

export type BeatSyncMaxDeckView = { stable_id: string | null; beat_sync_enabled: boolean };
export type BeatSyncEnableCommand = { type: 'beat_sync'; deck: DeckId; enabled: true };
export type BeatSyncMaxEnabler = {
	readDecks: () => Readonly<Record<DeckId, BeatSyncMaxDeckView>>;
	readBeatSyncMax: () => boolean;
	dispatch: (command: BeatSyncEnableCommand) => Promise<unknown>;
};

function _enable(deck: DeckId): BeatSyncEnableCommand {
	return { type: 'beat_sync', deck, enabled: true };
}

function _needsEnable(view: BeatSyncMaxDeckView): boolean {
	return view.stable_id !== null && !view.beat_sync_enabled;
}

/** Rules 1 and 2: only the OFF -> ON edge enables anything; OFF never disables. */
export function beatSyncMaxToggleCommands(
	previous: boolean,
	next: boolean,
	decks: Readonly<Record<DeckId, BeatSyncMaxDeckView>>
): BeatSyncEnableCommand[] {
	if (typeof previous !== 'boolean' || typeof next !== 'boolean') {
		throw new TypeError('Beat Sync Max toggle flags must be boolean');
	}
	if (previous || !next) return [];
	return DECK_IDS.filter((deck) => _needsEnable(decks[deck])).map(_enable);
}

/** Rules 1 and 3: a load while Max is on enables that deck, once. */
export function beatSyncMaxLoadCommand(
	beatSyncMax: boolean,
	deck: DeckId,
	view: BeatSyncMaxDeckView
): BeatSyncEnableCommand | null {
	if (typeof beatSyncMax !== 'boolean') {
		throw new TypeError(`beatSyncMax must be boolean, got ${typeof beatSyncMax}`);
	}
	return beatSyncMax && _needsEnable(view) ? _enable(deck) : null;
}

let _enabler: BeatSyncMaxEnabler | null = null;

/** One /performance command session owns the enabler; returns its uninstall. */
export function installBeatSyncMaxEnabler(enabler: BeatSyncMaxEnabler): () => void {
	if (_enabler !== null) throw new Error('Beat Sync Max enabler is already installed');
	_enabler = enabler;
	return () => {
		if (_enabler === enabler) _enabler = null;
	};
}

/** Called by the prefs setter on every Beat Sync Max write. With no
 * /performance session there is no deck to enable, so nothing runs. */
export async function announceBeatSyncMaxChange(previous: boolean, next: boolean): Promise<void> {
	const enabler = _enabler;
	if (enabler === null) return;
	const commands = beatSyncMaxToggleCommands(previous, next, enabler.readDecks());
	await Promise.all(commands.map((command) => enabler.dispatch(command)));
}

/** Called after a deck load settles. */
export async function enableBeatSyncAfterLoad(deck: DeckId): Promise<void> {
	const enabler = _enabler;
	if (enabler === null) return;
	const command = beatSyncMaxLoadCommand(enabler.readBeatSyncMax(), deck, enabler.readDecks()[deck]);
	if (command !== null) await enabler.dispatch(command);
}
