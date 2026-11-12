/**
 * Beat jump size, one entry per deck (DECKUX-10470).
 *
 * rekordbox and every CDJ since the 2000NXS2 offer ONE jump size per deck,
 * stepped with < >, and two buttons that jump back or forward by it. The
 * size is view state rather than engine state, and it must survive track
 * loads, so it lives here instead of in `DeckState` (rebuilt on every load).
 * Every UI control still needs a matching typed command, so `performance-ipc`
 * drives it through `beat_jump_size` and `BeatJump.svelte` only reads it.
 *
 * Persistence: this module imports nothing from the prefs hub. `prefs.svelte.ts`
 * restores the saved sizes on boot and subscribes with `onBeatJumpSizeChange`
 * to write them back, which keeps the deck/IPC side free of the prefs graph.
 */
/** Inline for the same fan-in reason `loop-interval-view.svelte.ts` gives. */
type DeckId = 1 | 2 | 3 | 4;

/** rekordbox 7 / CDJ-3000 BEAT JUMP sizes, smallest first (mimicked). */
export const BEAT_JUMP_SIZES = [0.5, 1, 2, 4, 8, 16, 32, 64] as const;
export type BeatJumpSize = (typeof BEAT_JUMP_SIZES)[number];
export const BEAT_JUMP_DEFAULT_SIZE: BeatJumpSize = 4;

/** The ui-prefs wire shape. */
export interface BeatJumpSizesByDeck {
	deck_1: BeatJumpSize;
	deck_2: BeatJumpSize;
	deck_3: BeatJumpSize;
	deck_4: BeatJumpSize;
}

const DECK_IDS: readonly DeckId[] = [1, 2, 3, 4];

export const beatJumpSizes = $state<Record<DeckId, BeatJumpSize>>({
	1: BEAT_JUMP_DEFAULT_SIZE,
	2: BEAT_JUMP_DEFAULT_SIZE,
	3: BEAT_JUMP_DEFAULT_SIZE,
	4: BEAT_JUMP_DEFAULT_SIZE
});

type BeatJumpSizeListener = (deck: DeckId, size: BeatJumpSize) => void;
const _listeners = new Set<BeatJumpSizeListener>();

// --------------------------------------------------------------- validation

/** A size outside the eight is a caller bug: refuse it, never round it. */
export function assertBeatJumpSize(beats: unknown): asserts beats is BeatJumpSize {
	if (!(BEAT_JUMP_SIZES as readonly unknown[]).includes(beats)) {
		throw new RangeError(
			`beat jump size must be one of 1/2|1|2|4|8|16|32|64; got ${String(beats)}`
		);
	}
}

// ------------------------------------------------------------ stepper math

/** The next size down (-1) or up (+1); unchanged at either end. */
export function steppedBeatJumpSize(size: BeatJumpSize, direction: -1 | 1): BeatJumpSize {
	assertBeatJumpSize(size);
	const index = BEAT_JUMP_SIZES.indexOf(size) + direction;
	return BEAT_JUMP_SIZES[Math.min(Math.max(index, 0), BEAT_JUMP_SIZES.length - 1)];
}

export { beatJumpSizeLabel } from '$lib/rb/format-beat-jump';

// ---------------------------------------------------------------- setters

/** A user or agent change: set it and tell the persistence listener. */
export function setBeatJumpSize(deck: DeckId, beats: BeatJumpSize): void {
	assertBeatJumpSize(beats);
	beatJumpSizes[deck] = beats;
	for (const listener of _listeners) listener(deck, beats);
}

/** Boot / disk hydrate: set all four without echoing back to persistence. */
export function restoreBeatJumpSizes(sizes: BeatJumpSizesByDeck): void {
	const parsed = beatJumpSizesFromPrefs(sizes);
	for (const deck of DECK_IDS) beatJumpSizes[deck] = parsed[`deck_${deck}`];
}

export function onBeatJumpSizeChange(listener: BeatJumpSizeListener): () => void {
	_listeners.add(listener);
	return () => _listeners.delete(listener);
}

// ------------------------------------------------------------- prefs wire

export function beatJumpSizesToPrefs(): BeatJumpSizesByDeck {
	return {
		deck_1: beatJumpSizes[1],
		deck_2: beatJumpSizes[2],
		deck_3: beatJumpSizes[3],
		deck_4: beatJumpSizes[4]
	};
}

export function beatJumpSizesFromPrefs(prefs: unknown): BeatJumpSizesByDeck {
	if (prefs === null || typeof prefs !== 'object' || Array.isArray(prefs)) {
		throw new TypeError(`beat_jump_sizes must be an object; got ${JSON.stringify(prefs)}`);
	}
	const record = prefs as Record<string, unknown>;
	for (const key of Object.keys(record)) {
		if (!/^deck_[1-4]$/.test(key)) throw new TypeError(`beat_jump_sizes has unknown key ${key}`);
	}
	const out = {} as BeatJumpSizesByDeck;
	for (const deck of DECK_IDS) {
		const value = record[`deck_${deck}`];
		assertBeatJumpSize(value);
		out[`deck_${deck}`] = value;
	}
	return out;
}
