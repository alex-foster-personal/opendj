/**
 * SECONDSCREEN-11713: the library-only second screen loads decks through the same
 * order bus the CLI and agents use (`POST /api/v1/commands`), never through an
 * in-page engine. The window that owns the audio (the /performance page) claims
 * and runs the order; this screen only reads the ui-mirror and posts orders.
 *
 * Requirements:
 * - ✔︎ a load order is exactly `{single: {type: 'load', deck, stable_id}}`, the
 *   shape the performance dispatcher accepts, with no replace_playing, so a
 *   playing deck refuses the load instead of being replaced.
 * - ✔︎ "next free deck" is the lowest-numbered deck with no track; with none
 *   free it is null and the caller refuses instead of guessing.
 * - ✔︎ a response is described honestly: 409 means no performance page is open,
 *   a failed step names its error, and only all-succeeded steps read as loaded.
 *
 * Acceptance tests (tests/unit/second-screen-load.test.mjs):
 * - [if] deck 2 and id "abc" [then] the order is {single:{type:'load',deck:2,stable_id:'abc'}}, [else ⛔️]
 * - [if] decks 1 and 2 hold tracks and 3 is empty [then] next free is 3, [else ⛔️]
 * - [if] every deck holds a track [then] next free is null, [else ⛔️]
 * - [if] the engine answers 409 [then] the message says the main window is not open, [else ⛔️]
 */

export type DeckNumber = 1 | 2 | 3 | 4;

export const DECK_NUMBERS: readonly DeckNumber[] = [1, 2, 3, 4];

export interface MirrorDeck {
	title?: string | null;
	artist?: string | null;
	stable_id?: string | null;
	playing?: boolean;
	effective_bpm?: number | null;
	key?: string | null;
	position?: { ms?: number | null } | null;
	duration_ms?: number | null;
}

export type MirrorDecks = Partial<Record<string, MirrorDeck | null>>;

export interface LoadOrder {
	single: { type: 'load'; deck: DeckNumber; stable_id: string };
}

export interface LoadOutcome {
	ok: boolean;
	message: string;
}

export function buildLoadOrder(deck: DeckNumber, stableId: string): LoadOrder {
	if (stableId.trim() === '') throw new TypeError('stable_id must be a non-empty string');
	return { single: { type: 'load', deck, stable_id: stableId } };
}

export function deckIsEmpty(deck: MirrorDeck | null | undefined): boolean {
	return deck === null || deck === undefined || !deck.stable_id;
}

export function nextFreeDeck(decks: MirrorDecks): DeckNumber | null {
	return DECK_NUMBERS.find((n) => deckIsEmpty(decks[String(n)])) ?? null;
}

interface ResultStep {
	status?: unknown;
	error?: unknown;
}

export function describeLoadResult(deck: DeckNumber, status: number, body: unknown): LoadOutcome {
	if (status === 409) {
		return {
			ok: false,
			message: 'The main Open DJ window is not open, so nothing can load. Open it first.'
		};
	}
	if (status === 503) {
		return { ok: false, message: `Deck ${deck}: the main window did not answer in time.` };
	}
	if (status !== 200) {
		return { ok: false, message: `Deck ${deck}: the engine refused the load (HTTP ${status}).` };
	}
	const steps = (body as { steps?: unknown } | null)?.steps;
	if (!Array.isArray(steps) || steps.length === 0) {
		return { ok: false, message: `Deck ${deck}: the engine returned no result for the load.` };
	}
	const failed = (steps as ResultStep[]).find((step) => step.status !== 'succeeded');
	if (failed !== undefined) {
		const why =
			typeof failed.error === 'string' && failed.error !== ''
				? failed.error
				: String(failed.status);
		return { ok: false, message: `Deck ${deck}: ${why}` };
	}
	return { ok: true, message: `Loaded on deck ${deck}` };
}

/** Remaining time as m:ss, or null when the mirror lacks position or duration. */
export function remainingLabel(deck: MirrorDeck | null | undefined): string | null {
	const pos = deck?.position?.ms;
	const dur = deck?.duration_ms;
	if (
		typeof pos !== 'number' ||
		typeof dur !== 'number' ||
		!Number.isFinite(pos) ||
		!Number.isFinite(dur)
	)
		return null;
	const left = Math.max(0, Math.round((dur - pos) / 1000));
	return `-${Math.floor(left / 60)}:${String(left % 60).padStart(2, '0')}`;
}
