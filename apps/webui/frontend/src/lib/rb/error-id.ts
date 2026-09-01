/**
 * One counter, one format, for every id a person can read off the screen and
 * then search for in a log.
 *
 * ORIGIN. The deck error banner rendered a bare error string. It never fades
 * and it has its own dismiss control, so it can sit on a deck for an entire
 * set, and yet there was no way to tie the words on screen to any row in any
 * log: two identical failures ten minutes apart produced two identical lines
 * and nothing distinguished them. `pushToast` had already solved the same
 * problem for toasts by minting an id before it wrote its logs, so the banner
 * reuses that machinery rather than growing a second, parallel scheme.
 *
 * WHY ONE COUNTER RATHER THAN ONE PER SURFACE. A toast and a banner can report
 * the SAME underlying failure, and a reader holding an id must never have to
 * ask which surface it came from before they can search for it. A single
 * monotonic source makes any id unambiguous across the whole client.
 *
 * PURE ON PURPOSE. Nothing here reads a store, a rune, the DOM or the network,
 * so both the counter and the format are testable by execution rather than by
 * reading the source.
 *
 * KNOWN LIMIT, and the follow-up it is waiting on. The sequence restarts at 1
 * on every page load, so `t-3` in a week of logs matches the third id of every
 * session. PR #621 (`toast-report.ts`) adds a per-load random session token and
 * the `t-<token>-<seq>` format that fixes exactly this; it was open and
 * conflicting when this landed, so this module deliberately mints the same `t-`
 * prefix it will use. When #621 merges, `formatErrorId` should take that
 * token and both surfaces inherit the fix at once.
 *
 * Requirements (mini-PRD):
 *   ✔︎ ✅ 🎯 mintErrorId: strictly increasing, never repeats within a page load.
 *     [if] two calls return the same number [then ⛔️] broken
 *     [if] a later call returns a lower number than an earlier one [then ⛔️] broken
 *   ✔︎ ✅ 🎯 formatErrorId: a greppable string, not a bare integer.
 *     [if] the rendered id is a bare number, so searching a log for it matches
 *          every unrelated occurrence of that digit [then ⛔️] broken
 *     [if] a non-positive or non-integer sequence is formatted rather than
 *          rejected [then ⛔️] broken
 */

/**
 * Prefix every id carries, so `t-` alone is enough to find one in a log.
 *
 * Deliberately the same letter `toast-report.ts` uses on PR #621, so the two
 * formats converge instead of colliding when that branch lands.
 */
export const ERROR_ID_PREFIX = 't';

let _sequence = 0;

/**
 * The next id in this page load.
 *
 * Returns the raw number rather than the formatted string because `pushToast`
 * stores it as the numeric handle it matches on when the dismiss timer fires,
 * and reports it to the server as `toast_id`. Callers that PRINT an id format
 * it; callers that match on one do not.
 */
export function mintErrorId(): number {
	_sequence += 1;
	return _sequence;
}

/** `t-7`. What a person reads off the screen and types into a search box. */
export function formatErrorId(sequence: number): string {
	if (!Number.isInteger(sequence) || sequence < 1) {
		throw new RangeError(`formatErrorId: sequence must be a positive integer, got ${sequence}`);
	}
	return `${ERROR_ID_PREFIX}-${sequence}`;
}
