/**
 * Settle library rows whose disk truth was still pending when they loaded
 * (PERF-RB-03, pin cba7bf1dbb05).
 *
 * A listing request stats at most 16 paths (PERF-RB-01). Every other cold row
 * comes back AVAILABILITY_PENDING and the server probes it in the background.
 * Pending rows stay visible under hide-broken (PERF-RB-02), and nothing asked
 * again, so a mostly-missing playlist showed dozens of rows while the tree
 * counted four. This re-asks on a short backoff and writes the settled answer
 * onto the rows already on screen.
 *
 * Requirements:
 *   ✔︎ Pending rows settle without a reload.
 *     [if] a re-ask answers present or absent for a pending row [then] that
 *       row takes the answer in place ⛔️
 *   ✔︎ Nothing is guessed.
 *     [if] the re-ask still says pending, or omits the row [then] it stays
 *       pending ⛔️
 *     [if] a held row already settled [then] no later answer changes it ⛔️
 *   ✔︎ The loop is bounded.
 *     [if] rows never settle [then] it stops after the last delay ⛔️
 *     [if] it is stopped [then] no timer is left and a late answer writes
 *       nothing ⛔️
 */

import type { FileAvailabilityStatus } from '$lib/rb/file-availability';

//-----------------------------------------------------------------------------
// config
//-----------------------------------------------------------------------------

/** Waits before each re-ask. The background probe usually lands inside the
 * first; the rest cover a slow or sleeping volume. */
export const PENDING_SETTLE_DELAYS_MS: readonly number[] = [1500, 3000, 6000, 12000];

const _PENDING: FileAvailabilityStatus = 'AVAILABILITY_PENDING';

//-----------------------------------------------------------------------------
// pure
//-----------------------------------------------------------------------------

export interface AvailabilityRow {
	stable_id: string;
	file_exists: boolean | null;
	file_availability: FileAvailabilityStatus;
}

function _isPending(row: AvailabilityRow): boolean {
	return row.file_availability === _PENDING;
}

export function hasPendingAvailability(rows: readonly AvailabilityRow[]): boolean {
	return rows.some(_isPending);
}

/** STATE-14 503 / sqlite lock on a settle re-ask. The loop already retries;
 * console.error is sampled into Sentry (OPEN-DJ-BE-8Y). */
export function isTransientListingBusy(exc: unknown): boolean {
	const text = String(exc).toLowerCase();
	return (
		text.includes('state store is busy') ||
		text.includes('state_store_busy') ||
		text.includes('database is locked')
	);
}

/** Copy settled answers from `fresh` onto the pending rows of `held`.
 * Returns a new row array and how many held rows are still pending. */
export function applySettledAvailability<T extends AvailabilityRow>(
	held: readonly T[],
	fresh: readonly AvailabilityRow[]
): { rows: T[]; stillPending: number } {
	const settled = new Map<string, AvailabilityRow>();
	for (const row of fresh) {
		if (!_isPending(row)) settled.set(row.stable_id, row);
	}
	let stillPending = 0;
	const rows = held.map((row) => {
		if (!_isPending(row)) return row;
		const answer = settled.get(row.stable_id);
		if (answer === undefined) {
			stillPending += 1;
			return row;
		}
		return {
			...row,
			file_exists: answer.file_exists,
			file_availability: answer.file_availability
		};
	});
	return { rows, stillPending };
}

//-----------------------------------------------------------------------------
// loop
//-----------------------------------------------------------------------------

export interface PendingSettleArgs {
	/** The rows on screen now. Read again at every step. */
	rows: () => readonly AvailabilityRow[];
	/** Publish merged rows after a re-ask (required for raw pane row arrays). */
	publishRows: (rows: readonly AvailabilityRow[]) => void;
	/** One fresh listing of the same rows. */
	fetchRows: () => Promise<readonly AvailabilityRow[]>;
	onError: (exc: unknown) => void;
	setTimer?: (fn: () => Promise<void>, ms: number) => unknown;
	clearTimer?: (handle: unknown) => void;
}

/** Start re-asking for the pending rows. Returns the stop function. */
export function startPendingSettle(args: PendingSettleArgs): () => void {
	const setTimer = args.setTimer ?? ((fn, ms) => setTimeout(() => void fn(), ms));
	const clearTimer =
		args.clearTimer ?? ((handle) => clearTimeout(handle as ReturnType<typeof setTimeout>));
	let stopped = false;
	let handle: unknown = null;

	const arm = (attempt: number): void => {
		if (stopped || attempt >= PENDING_SETTLE_DELAYS_MS.length) return;
		if (!hasPendingAvailability(args.rows())) return;
		handle = setTimer(async () => {
			handle = null;
			if (stopped) return;
			try {
				const fresh = await args.fetchRows();
				if (stopped) return;
				const { rows: next } = applySettledAvailability(args.rows(), fresh);
				args.publishRows(next);
			} catch (exc: unknown) {
				args.onError(exc);
			}
			arm(attempt + 1);
		}, PENDING_SETTLE_DELAYS_MS[attempt]);
	};
	arm(0);

	return () => {
		stopped = true;
		if (handle !== null) clearTimer(handle);
		handle = null;
	};
}
