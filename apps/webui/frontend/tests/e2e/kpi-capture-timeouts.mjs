/**
 * Timeout arithmetic shared by the S13 login KPI capture spec and its config.
 *
 * The spec's own budget (KPI_CAPTURE_TIMEOUT_S, set by `--timeout-s`) and the
 * Playwright test timeout used to be independent numbers, so an operator who
 * raised --timeout-s to the config's own 120s killed the browser context out
 * from under the spec's diagnostic path and got a crash instead of a clean
 * withhold reason. Deriving one from the other removes the footgun: the test
 * timeout is always the capture budget plus headroom, so the spec always wins
 * the race and always writes KPI_CAPTURE_RESULT.
 */

export const DEFAULT_KPI_CAPTURE_TIMEOUT_S = 90;

/** Slack between the spec's own deadline and Playwright killing the test. */
export const KPI_CAPTURE_HEADROOM_MS = 60_000;

/** Floor, so a tiny --timeout-s still leaves room for browser startup. */
export const KPI_CAPTURE_MIN_TEST_TIMEOUT_MS = 120_000;

/**
 * Parse KPI_CAPTURE_TIMEOUT_S. Unset, unparseable or non-positive all fall
 * back to the default rather than producing NaN, which Playwright reads as
 * "no timeout" and which is how a typo used to become an unbounded run.
 */
export function kpiCaptureTimeoutS(raw) {
	const parsed = Number.parseInt(raw ?? '', 10);
	if (!Number.isFinite(parsed) || parsed <= 0) {
		return DEFAULT_KPI_CAPTURE_TIMEOUT_S;
	}
	return parsed;
}

/** Playwright test timeout that is always safely above the capture budget. */
export function kpiCaptureTestTimeoutMs(raw) {
	return Math.max(
		KPI_CAPTURE_MIN_TEST_TIMEOUT_MS,
		kpiCaptureTimeoutS(raw) * 1000 + KPI_CAPTURE_HEADROOM_MS
	);
}
