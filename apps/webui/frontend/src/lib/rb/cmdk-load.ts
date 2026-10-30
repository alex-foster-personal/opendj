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
	const copy = { ...row, rb_meta: meta };
	applySettledAvailability(copy);
	return copy;
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
	loadOnto: (row: R, deck: D) => Promise<boolean>,
	deckMark: (deck: D) => string,
	deckPending: (deck: D) => boolean
): (row: R, deck: D) => Promise<boolean> {
	const latest = new Map<D, number>();
	return async (row, deck) => {
		const pick = (latest.get(deck) ?? 0) + 1;
		latest.set(deck, pick);
		const mark = deckMark(deck);
		const settled = await settleForLoad(row, fetchMeta);
		// A play queued during the settle wait moves no mark until it runs, so a queued command also refuses (Codex P1, #6687).
		if (latest.get(deck) !== pick || deckMark(deck) !== mark || deckPending(deck)) return false;
		return await loadOnto(settled, deck);
	};
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
	// A state without a boolean play field is a contract breach, not a paused deck: a caller that
	// omitted it would otherwise let a pending load interrupt playback without confirmation.
	if (typeof state.playing !== 'boolean') {
		throw new Error('deck state has no boolean playing field; refusing to mark it paused');
	}
	return `${state.stable_id ?? ''}|${state.load_generation}|${reservation}|${state.playing ? 'playing' : 'paused'}`;
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
