/**
 * Per-deck auto gain state (AUTOGAIN-02): what each deck is applying, for the
 * mixer readout, plus the hook the audio engine registers so a change (a new
 * load, a user offset, a pref change from Settings or the CLI) reaches the
 * trim stage at once. Pure logic and HTTP live in `auto-gain.ts`.
 */
import { DECK_IDS } from '$lib/player/constants';
import {
	AUTO_GAIN_DEFAULT_ENABLED,
	AUTO_GAIN_DEFAULT_TARGET_LUFS,
	AUTO_GAIN_OFFSET_MAX_DB,
	AUTO_GAIN_OFFSET_MIN_DB,
	deckAutoGainLinear,
	fetchAutoGainPrefs,
	fetchTrackAutoGain,
	putAutoGainPrefs,
	putTrackAutoGainUserOffset,
	type AutoGainPrefs,
	type AutoGainResolved,
	type DeckAutoGain
} from '$lib/rb/auto-gain';
import { subscribeKind, subscribeResync } from '$lib/api/events-bus';

/** A deck slot, 1..4 (`DECK_IDS`). */
export type DeckId = (typeof DECK_IDS)[number];

export const deckAutoGain = $state<Record<DeckId, DeckAutoGain>>(
	Object.fromEntries(DECK_IDS.map((deck) => [deck, { state: 'empty' }])) as Record<DeckId, DeckAutoGain>
);

const _appliers = new Set<(deck: DeckId) => void | Promise<void>>();
/** Per deck, settles once every engine has applied (or refused) the last publish. */
const _applied = new Map<DeckId, Promise<void>>();

/** Each audio engine (Web Audio, and the Rust engine when that mode is on)
 * registers once; every publish below re-applies that deck's trim stage. An
 * engine that applies asynchronously returns a promise that never rejects:
 * it publishes the deck's `error` state itself when the apply fails. */
export function registerAutoGainApplier(apply: (deck: DeckId) => void | Promise<void>): void {
	_appliers.add(apply);
}

/** Resolves once the deck's last published gain reached every engine. */
export function deckAutoGainApplied(deck: DeckId): Promise<void> {
	return _applied.get(deck) ?? Promise.resolve();
}

/** The linear factor the deck's trim stage multiplies in (1 unless ready). */
export function autoGainLinear(deck: DeckId): number {
	return deckAutoGainLinear(deckAutoGain[deck]);
}

/** Bumped by every publish and every refresh start, per deck: a refresh
 * answer applies only if nothing newer happened on that deck meanwhile. */
const _epoch = Object.fromEntries(DECK_IDS.map((deck) => [deck, 0])) as Record<DeckId, number>;

/** Bumped when the auto_gain pref changes value or the whole library is
 * invalidated (resync, CloudSync pull); `_trackGeneration` per track id. */
let _prefsGeneration = 0;
const _trackGeneration = new Map<string, number>();
/** Per-track count of user-offset writes started on this page. */
const _writeSeq = new Map<string, number>();
/** What each settled answer was read under, so a load that finishes after a
 * pref or user-offset change re-reads instead of playing the old gain. */
const _readUnder = new WeakMap<DeckAutoGain, string>();

function _stamp(stableId: string): string {
	return `${_prefsGeneration}:${_trackGeneration.get(stableId) ?? 0}`;
}

/** A change made elsewhere: mark answers already read for these tracks (or
 * every track, for an empty or missing list) stale. Only the event
 * subscribers call this, never a refresh, so re-reads cannot chase each other. */
export function invalidateAutoGainReads(ids?: readonly string[]): void {
	if (ids === undefined || ids.length === 0) _prefsGeneration += 1;
	else for (const id of ids) _trackGeneration.set(id, (_trackGeneration.get(id) ?? 0) + 1);
}

/**
 * Who may change live gain outside the dispatcher right now. performance-ipc
 * installs it: while a preset owns the lifecycle lock it answers why, and a
 * Settings change or an event-driven refresh is refused or held until a
 * later publish, so nothing changes a deck's gain during the preset's
 * exclusive transaction.
 */
let _liveGuard: () => string | null = () => null;
let _refreshOwed = false;

/** Install the guard; the returned function is called when the lock is
 * released, and runs any refresh the guard held back. */
export function setAutoGainLiveGuard(guard: () => string | null): () => void {
	_liveGuard = guard;
	return () => {
		if (!_refreshOwed || _liveGuard() !== null) return;
		_refreshOwed = false;
		void refreshLoadedDecks();
	};
}

export function publishDeckAutoGain(deck: DeckId, next: DeckAutoGain): void {
	_epoch[deck] += 1;
	deckAutoGain[deck] = next;
	const pending: Promise<void>[] = [];
	for (const apply of _appliers) {
		const result = apply(deck);
		if (result !== undefined) pending.push(result);
	}
	_applied.set(deck, Promise.all(pending).then(() => undefined));
	const read = next.state === 'ready' ? _readUnder.get(next) : undefined;
	const stale = read !== undefined && next.state === 'ready' && read !== _stamp(next.value.stable_id);
	if (stale || (_refreshOwed && _liveGuard() === null)) {
		// A load that read its answer before a pref change, or a refresh held
		// back by a preset: re-read now rather than play the old gain.
		_refreshOwed = false;
		void refreshLoadedDecks();
	}
}

/**
 * Resolve a track's auto gain for a deck load. Never rejects: a failed read
 * is the visible `error` state at 0 dB, so a server hiccup cannot block
 * playback, and it is never shown as if the track had been matched.
 */
export async function settleTrackAutoGain(stableId: string): Promise<DeckAutoGain> {
	const stamp = _stamp(stableId);
	try {
		const settled: DeckAutoGain = { state: 'ready', value: await fetchTrackAutoGain(stableId) };
		_readUnder.set(settled, stamp);
		return settled;
	} catch (error) {
		// Not logged here: a failed read is the readout's `AG ERR` with this
		// message, and a load that failed for the same cause already logs once.
		return { state: 'error', stable_id: stableId, message: `could not be read for this track, playing at 0 dB: ${error instanceof Error ? error.message : String(error)}` };
	}
}

/** A load's answer, re-read if a pref or this track's offset changed while
 * the load was decoding, so a superseded gain is never what the deck plays. */
export async function freshTrackAutoGain(candidate: DeckAutoGain): Promise<DeckAutoGain> {
	// No retry cap: each pass is caused by a real change made elsewhere, and a
	// capped loop would commit the very gain that change superseded.
	let current = candidate;
	while (current.state === 'ready' && _readUnder.get(current) !== _stamp(current.value.stable_id)) {
		current = await settleTrackAutoGain(current.value.stable_id);
	}
	return current;
}

function _loadedId(deck: DeckId): string | null {
	const current = deckAutoGain[deck];
	if (current.state === 'ready') return current.value.stable_id;
	if (current.state === 'error') return current.stable_id;
	return null;
}

/** Re-read every loaded deck (a pref or a user value changed elsewhere). */
export async function refreshLoadedDecks(onlyIds?: readonly string[]): Promise<void> {
	if (_liveGuard() !== null) {
		_refreshOwed = true;
		return;
	}
	await Promise.all(
		DECK_IDS.map(async (deck) => {
			const id = _loadedId(deck);
			// An empty id list is a whole-library invalidation (CloudSync pulls).
			if (id === null || (onlyIds !== undefined && onlyIds.length > 0 && !onlyIds.includes(id))) return;
			const epoch = ++_epoch[deck];
			const next = await settleTrackAutoGain(id);
			if (_epoch[deck] !== epoch || _loadedId(deck) !== id) return;
			// A preset may have taken the lock while this read was in flight:
			// hold the answer back and re-read once the lock is released.
			if (_liveGuard() !== null) {
				_refreshOwed = true;
				return;
			}
			publishDeckAutoGain(deck, next);
		})
	);
}

/** Set or clear the user's own offset for the track on `deck` and apply it. */
export async function setDeckAutoGainUserOffset(deck: DeckId, offsetDb: number | null, expectedId: string): Promise<AutoGainResolved> {
	const id = _loadedId(deck);
	if (id === null) throw new Error(`deck ${deck} has no track loaded`);
	// The write names the track it was meant for: a load that landed while it
	// queued must not receive another track's offset.
	if (id !== expectedId) throw new Error(`deck ${deck} now holds ${id}, not ${expectedId}; offset not written`);
	if (offsetDb !== null && !(offsetDb >= AUTO_GAIN_OFFSET_MIN_DB && offsetDb <= AUTO_GAIN_OFFSET_MAX_DB)) {
		throw new RangeError(`auto gain offset must be ${AUTO_GAIN_OFFSET_MIN_DB}..${AUTO_GAIN_OFFSET_MAX_DB} dB, got ${offsetDb}`);
	}
	// Version this write: a later write or a change made elsewhere while it is
	// in flight supersedes its answer, which is then re-read, never applied.
	const write = (_writeSeq.get(id) ?? 0) + 1;
	_writeSeq.set(id, write);
	invalidateAutoGainReads([id]);
	const stamp = _stamp(id);
	const value = await putTrackAutoGainUserOffset(id, offsetDb);
	if (_writeSeq.get(id) !== write || _stamp(id) !== stamp || _liveGuard() !== null) {
		// Superseded, or a preset took the lock meanwhile: re-read (held until
		// the lock is released) rather than change live gain now.
		void refreshLoadedDecks([id]);
		return value;
	}
	// The PUT answers with the resolved offset: publish it to every deck
	// holding that track instead of reading it back again.
	const holders = DECK_IDS.filter((holder) => _loadedId(holder) === id);
	for (const holder of holders) publishDeckAutoGain(holder, { state: 'ready', value });
	// Report success only once every engine applied it (the Rust engine
	// acknowledges over its socket); a refusal is the deck's error state.
	await Promise.all(holders.map((holder) => deckAutoGainApplied(holder)));
	for (const holder of holders) {
		const now = deckAutoGain[holder];
		if (now.state === 'error') throw new Error(`deck ${holder}: ${now.message}`);
	}
	return value;
}

/**
 * The `auto_gain` pref as the server holds it (AUTOGAIN-05). The server is the
 * only store: the deck's offset is resolved there, so a local copy could only
 * disagree with it. `loaded` is false until the first read lands; until then
 * the Settings row shows the server's documented defaults.
 */
export const autoGainPrefs = $state<AutoGainPrefs & { loaded: boolean; error: string | null }>({
	enabled: AUTO_GAIN_DEFAULT_ENABLED,
	target_lufs: AUTO_GAIN_DEFAULT_TARGET_LUFS,
	loaded: false,
	error: null
});

function _adopt(prefs: AutoGainPrefs): void {
	if (prefs.enabled !== autoGainPrefs.enabled || prefs.target_lufs !== autoGainPrefs.target_lufs) _prefsGeneration += 1;
	autoGainPrefs.enabled = prefs.enabled;
	autoGainPrefs.target_lufs = prefs.target_lufs;
	autoGainPrefs.loaded = true;
	autoGainPrefs.error = null;
}

export async function hydrateAutoGainPrefs(): Promise<void> {
	try {
		_adopt(await fetchAutoGainPrefs());
	} catch (error) {
		// What the page holds is no longer known to match the server: say so
		// rather than show the defaults or a stale value as if they were read.
		autoGainPrefs.loaded = false;
		autoGainPrefs.error = error instanceof Error ? error.message : String(error);
		console.error(`[auto-gain] could not read the auto_gain pref: ${autoGainPrefs.error}`);
	}
}

/** Why the auto gain settings cannot be shown or changed right now, or null. */
export function autoGainPrefsUnavailable(): string | null {
	// A refused or failed save leaves the read values in place but must not
	// pass silently: its error is shown until a later read or save succeeds.
	if (autoGainPrefs.loaded) return autoGainPrefs.error === null ? null : `were not saved: ${autoGainPrefs.error}`;
	return autoGainPrefs.error === null
		? 'not read from the engine yet'
		: `could not be read from the engine (${autoGainPrefs.error}); the values shown are not the saved ones`;
}

/** Write the pref, then re-resolve every loaded deck. A refused write restores
 * what the server holds and says why in the console and `error`. */
export async function saveAutoGainPrefs(patch: Partial<AutoGainPrefs>): Promise<void> {
	if (!autoGainPrefs.loaded) {
		// Never write a change made against values that were not read back.
		await hydrateAutoGainPrefs();
		const unread = autoGainPrefsUnavailable();
		if (unread !== null) {
			console.error(`[auto-gain] not saving ${JSON.stringify(patch)}: auto gain settings ${unread}`);
			return;
		}
	}
	const refused = _liveGuard();
	if (refused !== null) {
		await hydrateAutoGainPrefs();
		autoGainPrefs.error = `auto gain settings are locked: ${refused}`;
		console.error(`[auto-gain] ${autoGainPrefs.error}`);
		return;
	}
	try {
		_adopt(await putAutoGainPrefs(patch));
	} catch (error) {
		const message = error instanceof Error ? error.message : String(error);
		await hydrateAutoGainPrefs();
		autoGainPrefs.error = message;
		console.error(`[auto-gain] saving ${JSON.stringify(patch)} failed: ${message}`);
		return;
	}
	await refreshLoadedDecks();
}

let _synced = false;

/** Follow changes made elsewhere (CLI, another window): prefs re-read every
 * loaded deck, a track change re-reads the decks holding that track. */
export function startAutoGainSync(): void {
	if (_synced) return;
	_synced = true;
	void hydrateAutoGainPrefs();
	subscribeKind('ui_prefs', () => {
		void hydrateAutoGainPrefs().then(() => refreshLoadedDecks());
	});
	subscribeKind('tracks', (ids) => {
		invalidateAutoGainReads(ids);
		void refreshLoadedDecks(ids);
	});
	subscribeResync(() => {
		invalidateAutoGainReads();
		void hydrateAutoGainPrefs().then(() => refreshLoadedDecks());
	});
}
