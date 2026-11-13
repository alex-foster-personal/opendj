/**
 * Pure decision step behind quickload-dblclick-guard.spec.ts's
 * `_waitForDeckLoaded` (#6242, (private path)).
 *
 * Kept free of Playwright so tests/unit/quickload-deck2-settle.test.mjs can
 * pin the fail-fast contract without a browser: a deck that SETTLES on a
 * different stable id is a wrong-track load and must fail at once, naming the
 * expected id, the actual id and a board roster; console `deck-error` lines
 * seen during the wait ride both that failure and the poll timeout.
 */
import type { PerformanceState } from '../../../src/lib/rb/performance-ipc.svelte';

type DeckId = 1 | 2 | 3 | 4;
type DeckSnapshot = Pick<
	PerformanceState['decks'][DeckId],
	'stable_id' | 'playing' | 'transport_pending'
>;

/** The slice of a board snapshot the settle step reads. */
export interface DeckSettleBoard {
	decks: Record<DeckId, DeckSnapshot>;
}

/** One poll tick: the value `expect.poll` compares, plus a fail-fast error. */
export interface DeckSettleTick {
	value: string | null;
	settledWrong?: Error;
}

/** Short `deckN[sid=... playing=... pending=...]` roster for a failure message. */
export function boardRoster(board: DeckSettleBoard): string {
	return ([1, 2, 3, 4] as const)
		.map((id) => {
			const d = board.decks[id];
			return (
				`deck${id}[sid=${d.stable_id?.slice(0, 8) ?? 'null'} ` +
				`playing=${d.playing} pending=${d.transport_pending}]`
			);
		})
		.join(' ');
}

/** The last three collected deck-error lines, as a message suffix ('' if none). */
export function deckErrorTail(deckErrors: readonly string[]): string {
	if (deckErrors.length === 0) return '';
	return `; deck-error: ${deckErrors.slice(-3).join(' | ')}`;
}

/** Classify one board snapshot while waiting for `deck` to hold `stableId`.
 *
 * A settled wrong id returns the EXPECTED id as the poll value so the poll
 * stops this tick, with the error the caller must throw right after. */
export function deckSettleTick(
	board: DeckSettleBoard,
	deck: DeckId,
	stableId: string,
	deckErrors: readonly string[]
): DeckSettleTick {
	const state = board.decks[deck];
	if (state.transport_pending) return { value: `${state.stable_id} (pending)` };
	if (typeof state.stable_id === 'string' && state.stable_id !== stableId) {
		return {
			value: stableId,
			settledWrong: new Error(
				`deck ${deck} settled on ${state.stable_id}, expected ${stableId}; board: ${boardRoster(board)}${deckErrorTail(deckErrors)}`
			)
		};
	}
	return { value: state.stable_id };
}

/** The error the wait throws when the poll itself failed (usually its timeout). */
export function deckSettleFailure(
	err: unknown,
	settledWrong: Error | undefined,
	deckErrors: readonly string[]
): unknown {
	if (settledWrong) return settledWrong;
	const tail = deckErrorTail(deckErrors);
	if (tail === '') return err;
	return new Error(`${err instanceof Error ? err.message : String(err)}${tail}`);
}
