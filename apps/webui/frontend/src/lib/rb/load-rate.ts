/**
 * LIBUX-37: the library load indicator's rows/s figure. Pure.
 *
 * The figure is measured over a window that opens at a BASELINE (the first
 * progress report of a load: when it arrived and how many rows it already
 * carried) and counts only rows that arrived after it. Counting the first
 * page's rows without the time they took is what printed "178571 rows/s":
 * 500 rows over the 2.8 ms between two updates.
 */

/** No figure is shown until the window is at least this long. A page landed
 * about once a second on the measured library (9,000 rows in 17.6 s), so a
 * shorter window is one page divided by timing jitter. */
export const LOAD_RATE_MIN_WINDOW_MS = 1000;

export type LoadRateBaseline = { atMs: number; loaded: number };

/** Rows per second since the baseline, or null while it cannot be measured
 * (window too short, or no rows arrived in it). Never a guess, never 0. */
export function loadRowsPerSecond(
	baseline: LoadRateBaseline,
	nowMs: number,
	loaded: number
): number | null {
	const windowMs = nowMs - baseline.atMs;
	const rows = loaded - baseline.loaded;
	if (windowMs < LOAD_RATE_MIN_WINDOW_MS || rows <= 0) return null;
	return rows / (windowMs / 1000);
}
