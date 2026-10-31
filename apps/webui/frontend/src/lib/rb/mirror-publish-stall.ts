/** Pure rules for detecting a missed ui-mirror publish interval. */

export const MIRROR_STALL_MS = 5000;

export function classifyMirrorPublishGap(gapMs: number): 'ok' | 'stall' {
	if (!Number.isFinite(gapMs) || gapMs < 0) {
		throw new RangeError(`gapMs must be a non-negative finite number, got ${gapMs}`);
	}
	return gapMs > MIRROR_STALL_MS ? 'stall' : 'ok';
}

/**
 * What the page saw between two publishes, so a stall can be told apart
 * (issue #5887): hidden for most of the gap means a throttled tab; long tasks
 * for most of it means a busy main thread; neither means the process itself
 * was starved (host swap or load). `longTaskMs` is null where the engine has
 * no long-task observer (WebKit), which is unmeasured, never zero.
 */
export interface MirrorGapEvidence {
	hiddenMs: number;
	longTaskMs: number | null;
}

export function mirrorStallMessage(gapMs: number, evidence?: MirrorGapEvidence): string {
	const base = `ui-mirror publish missed for ${Math.round(gapMs)}ms`;
	if (evidence === undefined) return base;
	const longTasks =
		evidence.longTaskMs === null ? 'long tasks unmeasured' : `long tasks ${Math.round(evidence.longTaskMs)}ms`;
	return `${base}; hidden ${Math.round(evidence.hiddenMs)}ms of it; ${longTasks}`;
}

/** One long task on the page's clock (`performance.now()` milliseconds). */
export interface LongTaskSpan {
	startMs: number;
	durationMs: number;
}

export interface LongTaskSubscription {
	/** Deliver any entries the engine has queued but not yet handed to the listener. */
	flush(): void;
	stop(): void;
}

export interface MirrorGapEnv {
	/** Must be the clock long-task `startMs` is on (`performance.now()` in a page). */
	now(): number;
	isHidden(): boolean;
	/** Subscribe to visibility changes; returns the unsubscribe. */
	onVisibilityChange(listener: () => void): () => void;
	/** Report each long task as it is delivered; null when the engine has no long-task observer. */
	observeLongTasks(listener: (task: LongTaskSpan) => void): LongTaskSubscription | null;
}

export interface MirrorGapTracker {
	/** Evidence since the previous take (or since creation), then start a new window. */
	take(): MirrorGapEvidence;
	dispose(): void;
}

/**
 * Long tasks are attributed by WHEN they ran, not by when the observer
 * callback happened to arrive: the engine delivers entries asynchronously, so
 * after a block the overdue publish can run before the callback. `take()`
 * first drains the observer's queue, then counts only the part of each task
 * that overlaps the window; a task still running past the window's end keeps
 * its remainder for the next one.
 */
export function createMirrorGapTracker(env: MirrorGapEnv): MirrorGapTracker {
	let windowStart = env.now();
	let hiddenMs = 0;
	let hiddenSince: number | null = env.isHidden() ? windowStart : null;
	// Only tasks still running when delivered are held; finished ones are folded
	// into `doneMs` at once, so a tab that never publishes keeps no list that
	// grows with every long task it logs (Sol P2 on #8045).
	let tasks: LongTaskSpan[] = [];
	let doneMs = 0;
	const stopVisibility = env.onVisibilityChange(() => {
		const now = env.now();
		if (env.isHidden()) {
			hiddenSince ??= now;
		} else if (hiddenSince !== null) {
			hiddenMs += now - hiddenSince;
			hiddenSince = null;
		}
	});
	const longTasks = env.observeLongTasks((task) => {
		const end = task.startMs + task.durationMs;
		if (end > env.now()) tasks.push(task);
		else doneMs += Math.max(0, end - Math.max(task.startMs, windowStart));
	});
	return {
		take() {
			longTasks?.flush();
			const now = env.now();
			let longTaskMs = doneMs;
			const carried: LongTaskSpan[] = [];
			for (const task of tasks) {
				const end = task.startMs + task.durationMs;
				longTaskMs += Math.max(0, Math.min(end, now) - Math.max(task.startMs, windowStart));
				if (end > now) carried.push(task);
			}
			const evidence: MirrorGapEvidence = {
				hiddenMs: hiddenMs + (hiddenSince === null ? 0 : now - hiddenSince),
				longTaskMs: longTasks === null ? null : longTaskMs
			};
			tasks = carried;
			doneMs = 0;
			windowStart = now;
			hiddenMs = 0;
			if (hiddenSince !== null) hiddenSince = now;
			return evidence;
		},
		dispose() {
			stopVisibility();
			longTasks?.stop();
		}
	};
}

/** The browser's long-task observer, or null where `longtask` is not supported. */
export function browserLongTaskObserver(
	listener: (task: LongTaskSpan) => void
): LongTaskSubscription | null {
	if (typeof PerformanceObserver === 'undefined') return null;
	if (!(PerformanceObserver.supportedEntryTypes ?? []).includes('longtask')) return null;
	const deliver = (entries: PerformanceEntryList) => {
		for (const entry of entries) listener({ startMs: entry.startTime, durationMs: entry.duration });
	};
	const observer = new PerformanceObserver((list) => deliver(list.getEntries()));
	observer.observe({ type: 'longtask', buffered: false });
	return {
		flush: () => deliver(observer.takeRecords()),
		stop: () => observer.disconnect()
	};
}
