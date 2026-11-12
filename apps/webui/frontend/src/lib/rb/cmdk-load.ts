/**
 * CMDK-04: settle a picked row's pending disk truth before the bar loads it.
 *
 * A listing stats at most 16 paths per page (PERF-RB-01), so most rows of a
 * cold collection arrive AVAILABILITY_PENDING. The library table settles a
 * row once it scrolls into view (rb-meta hydration); a collection-only Cmd K
 * result never scrolls into view, and the load path refuses a pending row.
 * So the bar's load asks rb-meta for the one row picked, through the table's
 * own fetch (BrowserPanel `_fetchRbMetaWithRetry`: one retry, a 404 is final),
 * and settles it with the table's own rule (`applySettledAvailability`) before
 * loading. A failed ask leaves the row pending, and the load path then says
 * so: nothing is guessed.
 */

import { applySettledAvailability } from '$lib/components/rb/browser/browser-row-wire';
import type { FileAvailabilityStatus } from './file-availability';

/** The rb-meta fields the settle rule reads. */
export interface SettleMeta {
	file_exists: boolean;
	is_streaming: boolean;
	folder_path?: string | null;
}

/** The row fields the settle rule reads and writes. */
export interface SettleRow {
	stable_id: string;
	file_exists: boolean | null;
	file_availability?: FileAvailabilityStatus | null;
	file_path?: string | null;
}

/**
 * The row to load: the same row when its availability is already settled (or
 * it is a stick row, which has no rb-meta), else a copy settled from rb-meta.
 * The held index row is never mutated.
 */
export async function settleForLoad<R extends SettleRow>(
	row: R,
	fetchMeta: (stable_id: string) => Promise<SettleMeta>
): Promise<R> {
	if (row.file_availability !== 'AVAILABILITY_PENDING' || row.stable_id.startsWith('usb-')) return row;
	let meta: SettleMeta;
	try {
		meta = await fetchMeta(row.stable_id);
	} catch (exc) {
		console.error(`Cmd K: rb-meta for ${row.stable_id} failed; the row stays pending`, exc);
		return row;
	}
	return applySettledAvailability({ ...row, rb_meta: meta });
}

/**
 * The bar's load: settle the pick, then load it onto the deck, unless the deck
 * moved on while this pick waited on rb-meta. Settling can take a retry, and
 * the bar is closed (so it can be reopened) the moment a row is picked; without
 * this check a newer load would dispatch first and this older one would then
 * replace it. "Moved on" is a newer Cmd K pick for the same deck, or any change
 * in `deckMark`, which the bar builds with `deckLoadMark` from the engine's own
 * deck state, which every load path moves, and the browser's reservations. Picks for different decks never cancel each
 * other. Resolves true only when the load committed: `loadOnto` itself reports
 * that, because the load path can refuse (the live master, Library mode) and
 * still return normally. A dropped or refused pick resolves false, so a
 * follow-up (the vocals-only solo) never acts for it. `deckPending` says whether a deck has
 * queued commands; one queued during the settle wait refuses the pick too.
 */
export function createPickLoader<R extends SettleRow, D>(
	fetchMeta: (stable_id: string) => Promise<SettleMeta>,
	loadOnto: (row: R, deck: D, stillNewest: LoadGuard) => Promise<boolean>,
	deckMark: (deck: D) => string,
	queue: DeckQueuePort<D>
): PickLoader<R, D> {
	const latest = new Map<D, number>();
	const begin = (deck: D): PickStart => {
		const pick = (latest.get(deck) ?? 0) + 1;
		latest.set(deck, pick);
		// The deck's own commands as the pick begins: a cue, seek or play anyone sends from here
		// on, during the rb-meta wait included, is newer than this pick (Codex r4277285037).
		return { pick, mark: deckMark(deck), sent: queue.loadsDispatched(deck), used: queue.commandsDispatched(deck) };
	};
	const load = async (row: R, deck: D, start?: PickStart): Promise<boolean> => {
		const begun = start ?? begin(deck);
		const { pick, sent, used, fits } = begun;
		let mark = begun.mark;
		// Whatever the deck already had queued is older than this pick: it drains alongside the
		// rb-meta wait, so an older load that commits meanwhile moves the deck before this pick
		// rather than dropping it (Sol r4278366185).
		const older = queue.pending(deck) > 0;
		const [settled, drained] = await Promise.all([
			settleForLoad(row, fetchMeta),
			older ? queue.idle(deck) : Promise.resolve(true)
		]);
		if (latest.get(deck) !== pick || !drained) return false;
		// CMDK-16: an IPC or MIDI load dispatched during the wait sits in the
		// dispatcher's deck scope and has not moved the mark yet. Let the deck's
		// queue drain first: a load that commits there moves the mark and stays;
		// one that fails leaves it, and this pick goes ahead. A queue that never
		// drains drops the pick rather than racing it. Loads dispatched BEFORE
		// the pick are older than it and do not hold it back.
		const newer = queue.loadsDispatched(deck) !== sent;
		if (newer && !(await queue.idle(deck))) return false;
		// With only older loads in between, the deck they left is the one this pick replaces; its
		// play state stays the one the pick began on, so a deck that started playing still drops it.
		if (older && !newer) mark = sameLoadAs(deckMark(deck), mark);
		if (latest.get(deck) !== pick || deckMark(deck) !== mark) return false;
		// A play from MIDI or IPC is not a load, so the count above misses it, and while it is
		// still queued the mark reads paused. Drain whatever the deck still holds, then drop the
		// pick if the deck started playing: only a confirmed pick interrupts play (Codex r4276598137).
		// An older load still queued is waited on but does not cancel the pick.
		if (!newer && queue.pending(deck) > 0) {
			if (!(await queue.idle(deck))) return false;
			// A load sent while the drain ran is newer than this pick and wins it, even when it lands paused
			// (Codex P1 BLOCKING, #6689).
			if (queue.loadsDispatched(deck) !== sent || latest.get(deck) !== pick || startedPlaying(mark, deckMark(deck))) return false;
		}
		// The load path can itself await (a replacement unloads first): it asks again right
		// before sending the unload and the load, and a load or unload anyone else sent
		// meanwhile wins. So does any other command sent to the deck's own queue (a play, cue
		// or seek from MIDI or IPC): the deck is in use, and a replacement would remove the
		// track it acts on without anyone confirming (Codex r4277133041). Only until this pick's own
		// unload has gone out: after it the track that command acted on is already gone, and standing
		// down then would leave the deck empty, so only a newer load or unload wins (Codex r4278596582).
		const base = queue.loadsDispatched(deck);
		// What the guard last answered: a load path that stood down did not send this pick's load,
		// even when a rival then put the same track on the deck (Sol r4278366181).
		let stood = false;
		let cleared = -1;
		const committed = await loadOnto(settled, deck, (own) => {
			const holds =
				latest.get(deck) === pick &&
				queue.loadsDispatched(deck) === base + own &&
				(own > 0 || queue.commandsDispatched(deck) === used) &&
				(fits === undefined || fits());
			if (holds) cleared = own;
			else stood = true;
			return holds;
		});
		if (!committed || stood) return false;
		// The load the guard cleared is the (base + cleared + 1)th: any load or unload sent after it
		// is another one's, so the deck's state is not this pick's to report or follow up on.
		return cleared < 0 || queue.loadsDispatched(deck) === base + cleared + 1;
	};
	// After a wait of the caller's own (the aligned pair drains the deck's queue before it reads
	// it), the pick keeps its place and counts while nothing newer reached the deck: only what was
	// already queued ran, and the mark it left is the one to hold (Codex r4277480917).
	const resume = (deck: D, start: PickStart): PickStart | null =>
		latest.get(deck) === start.pick && queue.loadsDispatched(deck) === start.sent && queue.commandsDispatched(deck) === start.used
			? { ...start, mark: deckMark(deck) }
			: null;
	return Object.assign(load, { begin, resume });
}

/** Where a pick starts: its place among picks for the deck, the deck's mark, and its dispatched-load and deck-command counts then. */
export interface PickStart {
	pick: number;
	mark: string;
	sent: number;
	/** The deck's own commands dispatched so far (CMDK-16). */
	used: number;
	/**
	 * Asked with the rest of the guard right before the load is sent: false holds the load back
	 * and keeps what the deck has (the aligned pair's captured rate no longer fits the deck's
	 * pitch range against the master's tempo now, Codex r4277873847).
	 */
	fits?: () => boolean;
}

/**
 * The bar's load, plus `begin`: a caller with work to do before the load (the
 * aligned pair lowers the fader first) takes the pick's start with `begin` the
 * moment the DJ picks and passes it in, so a load dispatched during that work
 * counts as newer than the pick rather than older.
 */
export type PickLoader<R, D> = ((row: R, deck: D, start?: PickStart) => Promise<boolean>) & {
	begin: (deck: D) => PickStart;
	/** The start again after the caller's own wait, with the deck's mark now, or null once a newer pick, load or deck command arrived. */
	resume: (deck: D, start: PickStart) => PickStart | null;
};

/**
 * Asked by the load path right before it sends the load, with the loads and
 * unloads it sent itself so far: true while this pick is still the newest for
 * the deck and nobody else sent the deck a load or unload since it was cleared.
 */
export type LoadGuard = (ownDispatches: number) => boolean;

/** The dispatcher, as a pick sees it: loads dispatched per deck so far, commands still queued, and a wait for the deck's queue to drain. */
export interface DeckQueuePort<D> {
	loadsDispatched: (deck: D) => number;
	/** Deck-queue commands other than loads and beat_sync dispatched per deck so far. */
	commandsDispatched: (deck: D) => number;
	pending: (deck: D) => number;
	idle: (deck: D) => Promise<boolean>;
}

/** How long a pick waits for its deck's queued commands before it gives up (ms). */
export const DECK_IDLE_TIMEOUT_MS = 10_000;

/**
 * Resolves true once `pending()` reads 0, checking every `stepMs`, or false
 * after `timeoutMs` with commands still pending. The dispatcher's per-deck
 * pending count is the source: it counts a command from dispatch to settle.
 */
export async function waitDeckIdle(
	pending: () => number,
	sleep: (ms: number) => Promise<void>,
	timeoutMs = DECK_IDLE_TIMEOUT_MS,
	stepMs = 16,
	now: () => number = () => performance.now()
): Promise<boolean> {
	// A monotonic deadline: browsers clamp timers, so a count of requested sleeps can run far past the bound.
	const deadline = now() + timeoutMs;
	while (pending() > 0) {
		if (now() >= deadline) return false;
		await sleep(stepMs);
	}
	// A clamped timer can wake past the deadline just after the last command settled. That is still a timeout,
	// so the pick or eject stops rather than running on a queue it waited too long for (Sol P1, #6689).
	return now() < deadline;
}

/** The engine deck-state fields a load moves, plus whether the deck is playing. */
export interface DeckLoadState {
	stable_id: string | null;
	load_generation: number;
	playing: boolean;
}

/**
 * What every load path moves on a deck: the loaded track and the engine's
 * committed-load generation (browser, IPC and MIDI loads all go through the
 * engine; an unload clears the track), plus the browser's own reservation
 * count, which moves before a browser load commits. Playing is in it too: a
 * pick approved over a paused deck must not land on that deck once it is
 * playing, since only a confirmed pick may interrupt play.
 */
export function deckLoadMark(state: DeckLoadState, reservation: number): string {
	// A missing play state would read as paused, and an unconfirmed pick could then replace a playing deck.
	if (typeof state.playing !== 'boolean') throw new Error('deck state has no boolean playing field; refusing to mark it paused');
	return `${state.stable_id ?? ''}|${state.load_generation}|${reservation}|${state.playing ? 'playing' : 'paused'}`;
}

/** Whether a deck paused at mark `before` is playing at mark `now` (`deckLoadMark` ends with its play state). */
/**
 * `now`'s loaded track and generation, with `before`'s reservation and play state. A browser load reserved
 * during the drain is newer than the pick's start, so its reservation must not become the pick's baseline
 * (Sol P1, #6689).
 */
export function sameLoadAs(now: string, before: string): string {
	const loaded = now.split('|').slice(0, -2).join('|');
	const [reservation, play] = before.split('|').slice(-2);
	return `${loaded}|${reservation}|${play}`;
}

export function startedPlaying(before: string, now: string): boolean {
	return !before.endsWith('|playing') && now.endsWith('|playing');
}

/**
 * Run a load and report whether it committed this track. The load path can
 * return normally after a refusal (the live master, Library mode, a missing
 * file), so "returned" is not "loaded": only a moved generation with this
 * track on the deck is.
 */
export async function loadCommitted(
	state: () => DeckLoadState,
	stable_id: string,
	run: () => Promise<void>
): Promise<boolean> {
	const before = state().load_generation;
	await run();
	const after = state();
	return after.load_generation !== before && after.stable_id === stable_id;
}
