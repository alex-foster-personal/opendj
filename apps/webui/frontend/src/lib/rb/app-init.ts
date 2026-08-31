/**
 * Instruments that must run exactly once per page load.
 *
 * These live here rather than inline in +layout.svelte so they are unit
 * testable. That is not decoration: installPerfEventLogGlobal() was
 * exported and never called by anything in production, so
 * window.__mdtPerfLog did not exist at runtime and the e2e latency floor
 * in tests/e2e/performance-controls.spec.ts failed with "the latency
 * instrument is missing". A unit test on the installer could not catch
 * that, because the installer was fine; nobody ran it. A test on THIS
 * module can, and does.
 */

import { installPerfEventLogGlobal } from './perf-event-log';
import { startUsageHeartbeat } from './usage-heartbeat';

/**
 * Start the page-lifetime instruments. Returns the teardown, which the
 * caller owns (the root layout hands it back from onMount).
 */
export function startAppInstruments(): () => void {
	// DevTools + e2e read the client's own timing ring through these:
	// __mdtPerfLog() for the full ring, __mdtLastLoads() for deck loads.
	installPerfEventLogGlobal();
	// Tell the engine this page exists, so "is the app open" is a question
	// it can answer on its own instead of anyone having to ask a human.
	const stopUsageHeartbeat = startUsageHeartbeat();

	return () => {
		stopUsageHeartbeat();
	};
}
