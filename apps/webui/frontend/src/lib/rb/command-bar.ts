/**
 * CMDK-01..03: the Cmd-K command bar's pure parts (matching, deck targeting).
 *
 * Kept free of Svelte and of the audio engine so the unit suite can load it
 * directly. The component (components/rb/CommandBar.svelte) owns the keyboard,
 * the whole-library search request and the load hand-off to the browser.
 */
import type { DeckId } from './deck-id';
import type { FileAvailabilityStatus } from './api-rb';

/** The fields the bar shows and the browser's deck-load path needs. */
export interface CommandBarRow {
	stable_id: string;
	title: string | null;
	artist: string | null;
	genre: string | null;
	key: string | null;
	bpm: number | null;
	file_exists: boolean | null;
	is_streaming: boolean | null;
	file_availability?: FileAvailabilityStatus;
}

export type CommandBarScope = 'playlist' | 'library';

/** Rows the list draws at once; a longer match list is cut, and the count says so. */
export const COMMAND_BAR_LIMIT = 50;

/** Lower-cased "title artist genre key" for one row, built once per row set. */
export function rowHaystack(row: CommandBarRow): string {
	return [row.title, row.artist, row.genre, row.key]
		.filter((part): part is string => typeof part === 'string' && part !== '')
		.join(' ')
		.toLowerCase();
}

/**
 * Every whitespace-separated token must appear somewhere in the row, in any
 * order ("daft one" finds "One More Time - Daft Punk"). An empty query matches
 * every row, so opening the bar lists the playlist as it stands.
 */
export function matchRows(
	rows: readonly CommandBarRow[],
	haystacks: readonly string[],
	query: string,
	limit: number = COMMAND_BAR_LIMIT
): { rows: CommandBarRow[]; total: number } {
	if (haystacks.length !== rows.length) {
		throw new Error(`matchRows: ${haystacks.length} haystacks for ${rows.length} rows`);
	}
	const tokens = query.toLowerCase().split(/\s+/).filter((t) => t !== '');
	const out: CommandBarRow[] = [];
	let total = 0;
	for (let i = 0; i < rows.length; i += 1) {
		const hay = haystacks[i];
		if (tokens.every((t) => hay.includes(t))) {
			total += 1;
			if (out.length < limit) out.push(rows[i]);
		}
	}
	return { rows: out, total };
}

/** Where Enter loads when the bar opens: the lowest empty deck, else the
 * lowest deck that is not the live master, else deck 1 (the load path then
 * refuses it with its own reason). */
export function defaultTargetDeck(
	decks: readonly { id: DeckId; loaded: boolean; is_master: boolean }[]
): DeckId {
	const free = decks.find((d) => !d.loaded);
	if (free !== undefined) return free.id;
	const notMaster = decks.find((d) => !d.is_master);
	return notMaster?.id ?? decks[0]?.id ?? 1;
}

/** Left/Right step through the decks and stop at the ends, so holding an
 * arrow lands on deck 1 or the last deck rather than wrapping past it. */
export function stepDeck(ids: readonly DeckId[], current: DeckId, step: -1 | 1): DeckId {
	const at = ids.indexOf(current);
	if (at < 0) return ids[0] ?? current;
	return ids[Math.min(ids.length - 1, Math.max(0, at + step))];
}

/** Up/Down move the highlight and stop at the ends of the list. */
export function stepSelection(index: number, count: number, step: -1 | 1): number {
	if (count <= 0) return 0;
	return Math.min(count - 1, Math.max(0, index + step));
}

/** How long vocals-only waits for the deck's stems before giving up. Stems
 * decode after the mix, and a bundle the hub is still fetching from R2 can
 * take minutes (STEM_HYDRATE_MAX_WAIT_MS allows ten). */
export const VOCALS_ONLY_MAX_WAIT_MS = 10 * 60 * 1000;
const VOCALS_ONLY_POLL_MS = 150;

export type StemsSettled = 'ready' | 'unavailable' | 'error' | 'stale' | 'timeout';

/**
 * CMDK-03: the vocal solo can only land once the deck's stems have settled.
 * Soloing while they are still `loading` is refused by the engine ("stems are
 * loading: no aligned artifact"), so poll the deck until its stem status
 * leaves `loading`. `stale` means the deck moved on to another track, and the
 * caller must not touch it.
 */
export async function waitForStemsSettled(
	read: () => { stable_id: string | null; status: 'unavailable' | 'loading' | 'ready' | 'error' },
	stableId: string,
	options: { maxWaitMs?: number; sleep?: (ms: number) => Promise<void>; now?: () => number } = {}
): Promise<StemsSettled> {
	const sleep = options.sleep ?? ((ms: number) => new Promise<void>((r) => setTimeout(r, ms)));
	const now = options.now ?? (() => Date.now());
	const deadline = now() + (options.maxWaitMs ?? VOCALS_ONLY_MAX_WAIT_MS);
	for (;;) {
		const deck = read();
		if (deck.stable_id !== stableId) return 'stale';
		if (deck.status !== 'loading') return deck.status;
		if (now() >= deadline) return 'timeout';
		await sleep(VOCALS_ONLY_POLL_MS);
	}
}
