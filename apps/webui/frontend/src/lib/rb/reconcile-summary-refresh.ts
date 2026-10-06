/**
 * The browser panel's reconcile summary reads (HEALTH-15).
 *
 * The engine answers `GET /reconcile/summary?cached=true` from its last
 * library scan in milliseconds and never scans for the request; its scan took
 * 39 to 61 s on the silver preview (Mon 5 Oct 2026) beside the analysis
 * drain, past the browser's 30 s timeout. This module turns each read into
 * what the panel paints, and drives the re-asks through the shared coverage
 * refresh, so one read is in flight at a time and a scan still running is
 * asked again on RECONCILE_RECHECK_DELAYS_MS instead of being waited on.
 *
 * Kept out of BrowserPanel.svelte, which sits at the frontend file-size cap.
 */
import { getReconcileSummary, ReconcileSummaryWarming } from './api-rb';
import {
	createCoverageRefresh,
	RECONCILE_RECHECK_DELAYS_MS,
	type CoverageOutcome,
	type CoverageRefresh
} from './library-health-dots';

/** What one settled read tells the panel. `counts` is null when the read
 * failed: the counts on hand are kept, and `error` says why they are stale. */
export type ReconcileSummaryRead = {
	counts: { nonBroken: number; broken: number; availability: unknown } | null;
	error: string | null;
};

/** Read the summary once. A first scan still running applies nothing. */
export async function readReconcileSummaryOnce(
	apply: (read: ReconcileSummaryRead) => void
): Promise<CoverageOutcome> {
	try {
		const summary = await getReconcileSummary();
		const refreshError = summary.refresh_error ?? null;
		apply({
			counts: {
				nonBroken: summary.total_tracks - summary.total_broken,
				broken: summary.total_broken,
				availability: summary.availability ?? 'unknown'
			},
			// A failed rescan leaves the last good counts, which are then
			// unchecked: the light says so instead of quoting them as current.
			error: refreshError === null ? null : `the last library scan failed (${refreshError})`
		});
		return { ok: true, refreshing: summary.refreshing === true, refresh_error: refreshError };
	} catch (error: unknown) {
		if (error instanceof ReconcileSummaryWarming) {
			return { ok: true, refreshing: true, refresh_error: null };
		}
		apply({ counts: null, error: error instanceof Error ? error.message : String(error) });
		return { ok: false };
	}
}

export function createReconcileSummaryRefresh(
	apply: (read: ReconcileSummaryRead) => void
): CoverageRefresh {
	return createCoverageRefresh(() => readReconcileSummaryOnce(apply), RECONCILE_RECHECK_DELAYS_MS);
}
