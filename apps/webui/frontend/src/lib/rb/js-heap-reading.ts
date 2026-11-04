/**
 * The JS heap reading the sampler and the meter both take (PERF-R5 Q10).
 *
 * Kept out of memory-meter-model so the initial load does not carry the
 * meter's thresholds and hover copy just to read one number.
 */

/**
 * True when this webview exposes the Chromium-only `performance.memory`.
 *
 * Call ONCE at init and keep the answer: the API cannot appear part way
 * through a session, and re-probing it every 2 s only invites the caller to
 * treat a missing reading as a transient zero.
 */
export function hasJsHeapApi(perf: unknown): boolean {
	return typeof perf === 'object' && perf !== null && 'memory' in perf;
}

/**
 * usedJSHeapSize in MiB, or null when there is no reading to be had.
 *
 * Null covers all three no-reading cases - no `performance`, no
 * `performance.memory` (WKWebView), and a `memory` object without the field -
 * because every one of them must render as "unavailable", not as 0 MB.
 */
export function readJsHeapMB(perf: unknown): number | null {
	if (!hasJsHeapApi(perf)) return null;
	const used = (perf as { memory?: { usedJSHeapSize?: number } }).memory?.usedJSHeapSize;
	if (typeof used !== 'number' || !Number.isFinite(used)) return null;
	return Math.round(used / (1024 * 1024));
}
