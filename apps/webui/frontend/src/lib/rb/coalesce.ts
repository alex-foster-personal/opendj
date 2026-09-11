/**
 * In-flight + trailing coalescing for background refetches driven by the WS
 * invalidation bus (`$lib/api/events-bus`).
 *
 * The bus deliberately over-fires. A frame that reveals a seq gap invalidates
 * BOTH its own topic and everything else, so a consumer wired to
 * `subscribeKind('tracks')` and `subscribeResync` is called twice for one
 * frame. That is correct at the bus layer (a missed invalidation is a
 * correctness bug, a duplicate one is a wasted round trip), but a consumer that
 * answers each call with its own unguarded refetch turns it into two
 * CONCURRENT full library reads racing to write the same panes.
 *
 * The contract here is the standard one for this shape:
 * - idle trigger        -> run now
 * - trigger while running -> mark trailing, run exactly ONCE more afterwards,
 *                            however many triggers arrived in the meantime
 *
 * Trailing rather than dropping, because the invalidation that arrived mid-run
 * may describe a change the in-flight fetch had already read past. Dropping it
 * would leave the UI stale, which is the failure this whole bus exists to
 * prevent.
 */

/**
 * Wrap an async refresh so overlapping calls collapse into one run plus at most
 * one trailing run.
 *
 * A rejection from `run` propagates to whichever triggers are awaiting that run
 * and cancels the trailing pass. Nothing is swallowed: a refresh that throws is
 * a real failure and the caller decides what to do with it. Internal state is
 * cleared on that path too, so one rejection cannot wedge every later trigger.
 */
export function coalesce(run: () => Promise<void>): () => Promise<void> {
	let active: Promise<void> | null = null;
	let trailing = false;

	async function _drain(): Promise<void> {
		try {
			for (;;) {
				await run();
				if (!trailing) return;
				// Cleared BEFORE the rerun, so a trigger that lands during the
				// trailing pass books one more pass rather than being absorbed
				// by the pass it arrived too late for.
				trailing = false;
			}
		} finally {
			active = null;
			trailing = false;
		}
	}

	return function trigger(): Promise<void> {
		if (active !== null) {
			trailing = true;
			return active;
		}
		active = _drain();
		return active;
	};
}

/** Latest-value variant for continuous controls. The first physical input is
 * sent promptly, inputs while it is in flight collapse to the latest value,
 * and the final physical value is never dropped. */
export interface LatestCoalescer<T> {
	readonly pending: boolean;
	request(value: T): Promise<void>;
	cancel(): void;
}

export function coalesceLatest<T>(run: (value: T) => Promise<void>): LatestCoalescer<T> {
	let active: Promise<void> | null = null;
	let hasLatest = false;
	let latest!: T;
	let cancelled = false;

	async function _drain(): Promise<void> {
		try {
			while (hasLatest && !cancelled) {
				const value = latest;
				hasLatest = false;
				await run(value);
			}
		} finally {
			hasLatest = false;
			cancelled = false;
		}
	}

	return {
		get pending(): boolean {
			return active !== null || hasLatest;
		},
		request(value: T): Promise<void> {
			latest = value;
			hasLatest = true;
			if (active !== null) return active;
			const drain = _drain();
			const tracked = drain.finally(() => {
				if (active === tracked) active = null;
			});
			active = tracked;
			return tracked;
		},
		cancel(): void {
			if (active === null) return;
			hasLatest = false;
			cancelled = true;
		}
	};
}
