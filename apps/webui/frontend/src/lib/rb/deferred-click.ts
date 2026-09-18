/** Match TrackTable's DBLCLICK_GUARD_MS: longer than the platform double-click interval. */
export const KNOB_SINGLE_CLICK_DELAY_MS = 500;

export interface DeferredClickScheduler {
	schedule: (callback: () => void, delayMs: number) => number;
	cancel: (handle: number) => void;
}

const defaultScheduler: DeferredClickScheduler = {
	schedule: (callback, delayMs) => window.setTimeout(callback, delayMs),
	cancel: (handle) => window.clearTimeout(handle)
};

export interface DeferredClickGuard {
	schedule: (callback: () => void) => void;
	cancel: () => void;
	dispose: () => void;
}

/** Delay a single-click callback so a double-click can cancel it first. */
export function createDeferredClickGuard(
	scheduler: DeferredClickScheduler = defaultScheduler,
	delayMs = KNOB_SINGLE_CLICK_DELAY_MS
): DeferredClickGuard {
	let pending: number | null = null;

	return {
		schedule(callback) {
			if (pending !== null) {
				scheduler.cancel(pending);
			}
			pending = scheduler.schedule(() => {
				pending = null;
				callback();
			}, delayMs);
		},
		cancel() {
			if (pending === null) return;
			scheduler.cancel(pending);
			pending = null;
		},
		dispose() {
			if (pending === null) return;
			scheduler.cancel(pending);
			pending = null;
		}
	};
}
