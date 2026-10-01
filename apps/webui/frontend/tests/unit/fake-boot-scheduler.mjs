/**
 * Boot schedulers for tests that are about something other than the boot
 * window.
 *
 * `src/lib/rb/boot-scheduler.ts` holds deferred work for BOOT_QUIET_MS plus
 * an idle frame, which is exactly right in a browser and useless in a unit
 * test: a suite that waited it out would spend seconds per case proving
 * something boot-scheduler.test.mjs already proves with a fake clock.
 *
 * So the modules that defer take their scheduler as a parameter, and the
 * tests that are not about deferral pass one of these instead.
 */

/** Runs every deferred task the moment it is handed over. Use this in a test
 * whose subject is what the task DOES, not when it runs. */
export function immediateBootScheduler() {
	return {
		defer: (_label, task) => task(),
		deckLoadStarted: () => () => {},
		listingWalkStarted: () => () => {},
		start: () => () => {}
	};
}

/** Holds every deferred task until `release()` is called, so a test can
 * assert both halves: nothing ran at mount, and it ran afterwards. */
export function manualBootScheduler() {
	const queued = [];
	return {
		scheduler: {
			defer: (_label, task) => queued.push(task),
			deckLoadStarted: () => () => {},
			listingWalkStarted: () => () => {},
			start: () => () => {}
		},
		pending: () => queued.length,
		release: () => {
			while (queued.length > 0) queued.shift()();
		}
	};
}
