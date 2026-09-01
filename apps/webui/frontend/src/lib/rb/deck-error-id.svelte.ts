/**
 * The id the deck error banner shows, and the log row that carries the same id.
 *
 * WHAT WAS BROKEN. `DeckErrorBanner` rendered a raw error string and nothing
 * else. Unlike a toast it never fades, so it can sit on a deck for a whole set,
 * and unlike a toast it minted no id, so the words on screen tied to nothing:
 * "Beat Sync partial failure" on screen and "Beat Sync partial failure" in the
 * ring were not knowably the same raising, and two of them an hour apart were
 * indistinguishable. `pushToast` had already solved this. This module applies
 * the same rule to the banner using the same counter (`error-id.ts`), rather
 * than inventing a second id scheme that a reader would have to disambiguate.
 *
 * THE RULE, same as the toast one. The id is minted ONCE, BEFORE the log write,
 * and the identical string is what the banner renders and what the log row
 * carries. An id minted at render time that never reaches a log, or minted
 * fresh each time the banner re-renders, is worse than showing no id at all: it
 * looks like a correlation key and costs the reader a search before they learn
 * it was never going to work.
 *
 * ONE ID PER RAISING, NOT PER RENDER. `controlError` in Deck.svelte is a
 * `$derived` over three independent sources, so it re-evaluates whenever any
 * deck state changes while an error is standing. Re-minting on each of those
 * would churn the id under the reader's cursor and write a log row per frame.
 * The remembered message per deck is what makes the id stable for as long as
 * the banner is showing that message, and what makes a DIFFERENT message, or
 * the same message raised again after a dismissal, a genuinely new incident
 * with a new id.
 *
 * WHERE THE ID LANDS, and where it deliberately does not. It goes into the
 * `kind` of a perf-event row, which puts it in both the console line and the
 * durable localStorage ring that `__mdtPerfLog()` reads. It does NOT fire a
 * second `reportClientError`: the paths that raise a banner error via
 * `_persistCommandError` and `_recordProcessorFailure` already push a toast,
 * and that toast already writes the server JSONL row. `reportClientError`
 * dedupes on `source`, so a second report per failure would land as two rows
 * with the diagnosis on neither, which `deck-load-failure-context.ts` documents
 * as the failure mode to avoid. The banner's id is therefore findable in the
 * console and the ring, and it shares a counter with the toast id that reached
 * the server.
 *
 * Requirements (mini-PRD):
 *   ✔︎ ✅ 🎯 noteDeckError: an error message gets an id, and the SAME id is
 *     written to the log and exposed for the banner to render.
 *     [if] the banner id is absent from every log line [then ⛔️] broken
 *     [if] the logged id differs from the exposed one [then ⛔️] broken
 *   ✔︎ ✅ 🎯 The id is stable while one message stands, and unique per raising.
 *     [if] re-noting the same message mints a second id or writes a second row
 *          [then ⛔️] broken
 *     [if] a different message reuses the previous id [then ⛔️] broken
 *     [if] the same message raised again after a clear reuses its old id
 *          [then ⛔️] broken
 *   ✔︎ ✅ 🎯 Clearing the error clears the id, so no stale id outlives its banner.
 *     [if] a cleared deck still exposes an id [then ⛔️] broken
 *   ✔︎ ✅ 🎯 Decks do not share ids or evict each other's.
 *     [if] noting deck 2 changes deck 1's id [then ⛔️] broken
 */
import { formatErrorId, mintErrorId } from './error-id';
import { recordPerfEvent } from './perf-event-log';

/**
 * The deck id union, declared inline rather than imported from `types.ts`.
 *
 * Same reason `perf-event-log.ts` states beside its own copy: `types.ts` is the
 * most-imported module in the frontend and the quality ratchet caps its fan-in,
 * which is already at its recorded ceiling. A real `DeckId` satisfies this, so
 * call sites still type-check against the shared union.
 */
export type DeckErrorDeckId = 1 | 2 | 3 | 4;

/**
 * The id currently on screen for each deck, or null when that deck has no
 * banner. Read by Deck.svelte; never written from outside this module.
 */
export const deckErrorIds = $state<Record<DeckErrorDeckId, string | null>>({
	1: null,
	2: null,
	3: null,
	4: null
});

/**
 * The message each id was minted for.
 *
 * Plain module state rather than a rune: nothing renders it, and making it
 * reactive would invite a component to read it and re-render on a value that is
 * bookkeeping rather than UI.
 */
const _notedMessages: Record<DeckErrorDeckId, string | null> = { 1: null, 2: null, 3: null, 4: null };

/**
 * The perf-event kind a banner row is written under.
 *
 * The id rides the KIND rather than the message so that it survives into the
 * ring row's own field and onto the console line ahead of the message text,
 * where a reader scanning `[perf-event]` lines sees it without reading the
 * whole failure. Prefix-stable so `perf-event-log`'s bucketing keeps treating
 * these as low-volume 'other' rows.
 */
export const DECK_ERROR_KIND = 'deck-error';

/**
 * Tell the id tracker what this deck's banner is showing, and get the log row
 * written the first time it shows anything new.
 *
 * Idempotent for an unchanged message, which is what lets Deck.svelte call it
 * from an `$effect` that re-runs on unrelated deck state.
 */
export function noteDeckError(deck: DeckErrorDeckId, message: string | null): void {
	if (message === null) {
		_notedMessages[deck] = null;
		deckErrorIds[deck] = null;
		return;
	}
	if (_notedMessages[deck] === message) return;
	// Minted BEFORE the write, so the string the banner renders and the string
	// the log carries cannot drift apart.
	const id = formatErrorId(mintErrorId());
	_notedMessages[deck] = message;
	deckErrorIds[deck] = id;
	recordPerfEvent(`${DECK_ERROR_KIND} ${id}`, message, deck, 'error');
}
