/** Match TrackTable's DBLCLICK_GUARD_MS: longer than the platform double-click interval. */
export const KNOB_SINGLE_CLICK_DELAY_MS = 500;

export interface DeferredClickScheduler {
	schedule: (callback: () => void, delayMs: number) => number;
	cancel: (handle: number) => void;
}

export interface DeferredClickGuard {
	schedule: (callback: () => void) => void;
	cancel: () => void;
	dispose: () => void;
}

/** Delay a single-click callback so a double-click can cancel it first. */
export function createDeferredClickGuard(
	scheduler?: DeferredClickScheduler,
	delayMs = KNOB_SINGLE_CLICK_DELAY_MS
): DeferredClickGuard {
	const resolvedScheduler = scheduler ?? {
		schedule: (callback, delayMs) => window.setTimeout(callback, delayMs),
		cancel: (handle) => window.clearTimeout(handle)
	};
	let pending: number | null = null;

	return {
		schedule(callback) {
			if (pending !== null) {
				resolvedScheduler.cancel(pending);
			}
			pending = resolvedScheduler.schedule(() => {
				pending = null;
				callback();
			}, delayMs);
		},
		cancel() {
			if (pending === null) return;
			resolvedScheduler.cancel(pending);
			pending = null;
		},
		dispose() {
			if (pending === null) return;
			resolvedScheduler.cancel(pending);
			pending = null;
		}
	};
}
