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

import { bootScheduler, type BootScheduler } from './boot-scheduler';
import { pressureIsElevated, readMachinePressure, startMachinePressurePolling } from './machine-pressure';
import { installPerfEventLogGlobal } from './perf-event-log';
import { anyDeckPlaying, startBackgroundDemandShed } from './playing-gate';
import { installReloadCountdown } from './reload-countdown';
import { readXrunSessionCounter } from './xrun-sentinel';
import { pushToast } from '$lib/stores.svelte';
import { startUsageHeartbeat } from './usage-heartbeat';

/**
 * Start the page-lifetime instruments. Returns the teardown, which the
 * caller owns (the root layout hands it back from onMount).
 *
 * The scheduler is a parameter so the unit suite can drive the boot window
 * by hand instead of waiting BOOT_QUIET_MS of real time. Production passes
 * nothing and gets the page's one scheduler.
 */
export function startAppInstruments(scheduler: BootScheduler = bootScheduler): () => void {
	// DevTools + e2e read the client's own timing ring through these:
	// __mdtPerfLog() for the full ring, __mdtLastLoads() for deck loads.
	installPerfEventLogGlobal();
	// The boot request window (PERF-R6): everything nobody is waiting on
	// queues behind the deck load instead of racing it for the daemon's
	// single worker and the origin's six connections. See boot-scheduler.
	const stopBootScheduler = scheduler.start();
	// Tell the engine this page exists, so "is the app open" is a question
	// it can answer on its own instead of anyone having to ask a human.
	const stopUsageHeartbeat = startUsageHeartbeat(scheduler);
	// REFRESH-01 (#891): a full reload while the maintainer is looking at the tab gets a
	// 10 s on-top countdown first. Also installs __mdtScheduleReload for agents.
	const stopReloadCountdown = installReloadCountdown();
	// PERF-CONTEXT: keep the machine's load/memory pressure in memory so a deck
	// load can stamp the conditions it was measured under without the load path
	// itself awaiting anything. Polls only while the page is visible. The first
	// poll goes through the same boot scheduler as the heartbeat above, so it
	// does not compete with a boot-time deck load either (#1658 review).
	const stopMachinePressurePolling = startMachinePressurePolling(scheduler);
	const stopBackgroundDemandShed = startBackgroundDemandShed({
		isPlaying: anyDeckPlaying,
		pressureElevated: () => pressureIsElevated(readMachinePressure()),
		readXruns: () => readXrunSessionCounter().xruns,
		notify: (suggestion) => pushToast(suggestion.message, 'warn'),
		jobs: []
	});

	return () => {
		stopBackgroundDemandShed();
		stopMachinePressurePolling();
		stopReloadCountdown();
		stopUsageHeartbeat();
		stopBootScheduler();
	};
}
