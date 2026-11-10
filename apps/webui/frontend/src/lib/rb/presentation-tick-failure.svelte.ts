/**
 * The latest presentation-tick failure, as ONE reactive cell.
 *
 * Why its own module: `Deck.svelte`'s banner derived used to read this value
 * through `queryPerformanceState()`, which builds the whole app snapshot, so
 * each deck's derived depended on every reactive signal and re-ran on every
 * bus command (about 8 ms main-thread per command, profiled Tue 10 Nov). That
 * broad dependency was also the only thing that made the banner update, since
 * the value lived in a plain module variable. A rune cell lets the banner
 * depend on exactly this value. It lives beside `presentation-clock-report.ts`
 * because a `.ts` module cannot hold `$state`, and it imports nothing, so it
 * adds no importer of `deck-slots.ts` and no lines to `audio-engine.svelte.ts`.
 *
 * Requirements (mini-PRD):
 *   ✔︎ ✅ 🎯 PERF-GRID-12026: the failure message is reactive.
 *     [if] a `$derived` reading it does not re-run on a note [then ⛔️] broken
 *     [if] a `$derived` reading it does not re-run on a clear [then ⛔️] broken
 *     [if] re-writing the same value writes the signal (a clear every frame
 *          inside a derived would throw `state_unsafe_mutation`) [then ⛔️] broken
 */

const _cell = $state<{ message: string | null }>({ message: null });

/** Durable failure state for the latest presentation tick, read reactively. */
export function presentationTickFailure(): string | null {
	return _cell.message;
}

/** Write the failure; a no-op when unchanged, so a per-frame clear is free. */
export function setPresentationTickFailure(message: string | null): void {
	if (_cell.message === message) return;
	_cell.message = message;
}
