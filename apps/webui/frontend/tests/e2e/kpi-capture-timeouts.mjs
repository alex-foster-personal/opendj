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

/** Slack between the spec's own deadline and Playwright killing the test. */
export const KPI_CAPTURE_HEADROOM_MS = 60_000;

/** Floor, so a tiny --timeout-s still leaves room for browser startup. */
export const KPI_CAPTURE_MIN_TEST_TIMEOUT_MS = 120_000;

/**
 * Ceiling. An unbounded capture budget is its own fail-fast violation: a
 * fat-fingered value would hang a lane instead of failing it. An hour is far
 * beyond any real login capture, which finishes in seconds.
 */
export const KPI_CAPTURE_MAX_TIMEOUT_S = 3600;

/**
 * Parse KPI_CAPTURE_TIMEOUT_S, refusing anything that is not a positive
 * integer number of seconds.
 *
 * There is no default here on purpose. `capture_s13.py` always sets this
 * variable (argparse supplies its own default), and the spec already refuses
 * to run without `KPI_CAPTURE_RESULT`, so there is no legitimate standalone
 * invocation for a fallback to serve -- it could only hide a typo and run the
 * capture under a budget nobody chose. An unparseable value used to become
 * NaN, which Playwright reads as "no timeout".
 */
export function kpiCaptureTimeoutS(raw) {
	if (raw === undefined || raw === null || String(raw).trim() === '') {
		throw new Error('KPI_CAPTURE_TIMEOUT_S is required (positive integer seconds)');
	}
	const text = String(raw).trim();
	if (!/^[0-9]+$/.test(text)) {
		throw new Error(
			`KPI_CAPTURE_TIMEOUT_S must be a positive integer number of seconds, got ${JSON.stringify(text)}`
		);
	}
	const parsed = Number.parseInt(text, 10);
	// A long enough run of digits passes the regex and parses to Infinity,
	// which survives a `<= 0` guard and reaches Playwright as a timeout of
	// Infinity -- an unbounded run from a value that looked validated.
	if (!Number.isSafeInteger(parsed)) {
		throw new Error(
			`KPI_CAPTURE_TIMEOUT_S is not a representable number of seconds, got ${JSON.stringify(text)}`
		);
	}
	if (parsed <= 0) {
		throw new Error(`KPI_CAPTURE_TIMEOUT_S must be greater than zero, got ${parsed}`);
	}
	if (parsed > KPI_CAPTURE_MAX_TIMEOUT_S) {
		throw new Error(
			`KPI_CAPTURE_TIMEOUT_S must be at most ${KPI_CAPTURE_MAX_TIMEOUT_S}s, got ${parsed}`
		);
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
