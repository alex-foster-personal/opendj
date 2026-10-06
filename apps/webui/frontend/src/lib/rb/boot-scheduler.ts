/**
 * The boot request window: what may talk to the daemon while the page is
 * still starting, and what has to wait its turn.
 *
 * WHY THIS EXISTS (measured, PERF-R6, Mon 31 Aug 2026). An A/B bench against
 * the 18 Aug build convicted the startup burst, not steady state: a deck
 * loaded while the app was still booting cost 2.1x more (fetchWall median
 * 3793 -> 8038ms, the four small metadata calls 3.5-4.7x), while a deck
 * loaded on a settled page was unchanged at 86ms. The cause is arithmetic,
 * not mystery. Today's page opens a set of endpoint families at mount that
 * the old build never called at all, which stretched the boot burst from
 * 1187ms to 3579ms. The daemon is single-worker by design (workers=1 is
 * LOCKED), and the browser gives one origin six connections. Extra boot
 * requests therefore do two things at once: they queue behind each other on
 * the server, and they hold connections the deck load needs. The deck load
 * is the one request a DJ can feel.
 *
 * WHAT MOVES, counted rather than reasoned about (measured on the bench
 * fixture, Mon 1 Sep 2026): EIGHT requests leave the burst -- auth/me x2,
 * build-info, feedback/todos + comments + general, jobs?limit=200 and the
 * first telemetry/heartbeat. They now land together at ~3.2s instead of
 * inside the first ~350ms.
 *
 * WHAT STAYS, and why the burst is still not empty: the health probe
 * (everything gates on it), entitlements (planRefusal() is read
 * synchronously by controls all over the app), setup/status (it decides
 * whether the app sits behind the first-run wizard), and the library's own
 * tracks?limit=500, which is the page's primary content rather than
 * background chatter. The same measurement also found settings, smartlists,
 * playlists, ui-prefs x2, stems/tiers, client-events and FOUR health calls
 * in that window, none of them named in the original finding -- see the
 * performance register for that flag.
 *
 * WHAT IT DOES. Nothing here makes a request faster or smaller. It moves the
 * requests nobody is waiting on out of the window where somebody is. A call
 * handed to `defer()` runs unchanged -- same function, same arguments, same
 * error handling -- only later:
 *
 *   1. after a quiet period (BOOT_QUIET_MS) from the first deferral, and
 *   2. after the browser reports an idle frame, and
 *   3. after any in-flight deck load has settled, and
 *   4. after the boot All Tracks walk has settled (LIBM-138): the listing is
 *      the page's primary content and every deferred request runs on the
 *      same single-worker engine, so a request let through mid-walk slows
 *      the rows a person is watching fill.
 *
 * NOTHING IS EVER DROPPED. This is a scheduler, not a filter. Every deferred
 * task runs: the deck-load yield has a ceiling (DECK_LOAD_YIELD_MAX_MS) so a
 * stuck load cannot strand the queue, a platform with no idle callback falls
 * back to the timer, and a task deferred after the window has closed runs
 * immediately rather than waiting for a window that will not come again.
 * A task that throws is rethrown asynchronously, exactly as the `void task()`
 * it replaced would have surfaced, so a failure stays as loud as it was and
 * the rest of the queue still runs.
 *
 * DRY (D5): the perf logic lives here as a pure module, and feature code
 * calls into it. No call site does its own timing arithmetic.
 *
 * Requirements (mini-PRD):
 *   OK  defer() before release queues; the task must not run synchronously.
 *       [if a deferred task runs during mount then the boot burst is
 *        unchanged and this module is pointless]
 *   OK  release happens only after the quiet period AND an idle frame.
 *       [if the queue drains on the timer alone then it can land inside the
 *        very frame the deck load is decoding in]
 *   OK  an in-flight deck load holds the queue back until it settles.
 *       [if deferred work fires while a deck is loading then it is competing
 *        for the same six connections and this is broken]
 *   OK  the yield has a ceiling; the queue always drains.
 *       [if a load that never settles strands the queue then work is being
 *        dropped silently, which the house rules ban]
 *   OK  defer() after release runs the task immediately.
 *       [if a panel opened at minute ten waits three seconds then the
 *        scheduler has become a latency source of its own]
 */

/** How long the boot burst is given to clear before deferred work starts.
 * Sits above the 1187ms burst the old build had and below the felt-lag
 * threshold for work nobody is looking at. */
export const BOOT_QUIET_MS = 3_000;

/** Ceiling on waiting for an idle frame. A page that never goes idle still
 * has to let its deferred work through. */
export const BOOT_IDLE_TIMEOUT_MS = 1_000;

/** How often the yield re-checks whether the decks are free. */
export const DECK_LOAD_YIELD_POLL_MS = 250;

/** Ceiling on the deck-load yield. Deck loads at boot were measured at
 * 350-850ms typical and 2.8s at the worst outlier, so this is generous
 * enough to cover a real load and short enough that a load which never
 * settles cannot strand the queue. */
export const DECK_LOAD_YIELD_MAX_MS = 10_000;

/**
 * The platform, injected so the scheduler is testable without a browser and
 * without real time passing.
 */
export interface BootHost {
	setTimer: (run: () => void, ms: number) => number;
	clearTimer: (handle: number) => void;
	/** The browser's idle callback, or null where the platform has none. */
	whenIdle: ((run: () => void, timeoutMs: number) => void) | null;
	/** Where the ceiling's WARN goes. Defaults to console.warn. */
	warn?: (message: string) => void;
}

export interface BootScheduler {
	/** Run `task` once the boot window has closed, or now if it already has.
	 * `label` names the work in a stack trace when the task throws. */
	defer: (label: string, task: () => void) => void;
	/** Tell the scheduler a deck load has begun. Call the returned function
	 * when it settles, success or failure. Calling it twice is harmless. */
	deckLoadStarted: () => () => void;
	/** Tell the scheduler the boot All Tracks walk has begun. Call the returned
	 * function when it ends: last page, failure, or a boot that opens another
	 * pane. Shares the deck-load yield and its ceiling, so an abandoned walk
	 * cannot strand the queue. Calling it twice is harmless. */
	listingWalkStarted: () => () => void;
	/** Open the window explicitly and return its teardown. `defer` also opens
	 * it, because child components mount before the root layout does. */
	start: () => () => void;
}

interface QueuedTask {
	label: string;
	task: () => void;
}

/**
 * Build a scheduler over one platform. Exported for tests, which drive a
 * fake host; the application uses the `bootScheduler` singleton below.
 */
export function createBootScheduler(host: BootHost): BootScheduler {
	const queue: QueuedTask[] = [];
	let released = false;
	let armed = false;
	let timer: number | null = null;
	// Deck loads and the boot listing walk, counted together: either one in
	// flight means deferred work would compete with something a person feels.
	let holds = 0;
	let yieldedMs = 0;

	function _hold(): () => void {
		holds += 1;
		let settled = false;
		return () => {
			if (settled) return;
			settled = true;
			holds -= 1;
		};
	}

	function _cancelTimer(): void {
		if (timer === null) return;
		host.clearTimer(timer);
		timer = null;
	}

	function _run(entry: QueuedTask): void {
		try {
			entry.task();
		} catch (error) {
			// Rethrown out of band so it surfaces exactly as the plain
			// `void task()` this replaced would have, instead of being
			// swallowed here -- and so one bad task cannot eat the queue.
			host.setTimer(() => {
				throw new Error(`[boot-scheduler] deferred task '${entry.label}' threw`, {
					cause: error
				});
			}, 0);
		}
	}

	function _release(): void {
		released = true;
		_cancelTimer();
		// Arrival order. A task that defers more work sees `released` already
		// true and goes straight through, so the loop cannot spin.
		while (queue.length > 0) _run(queue.shift() as QueuedTask);
	}

	function _releaseOnceDecksAreFree(): void {
		timer = null;
		if (holds === 0 || yieldedMs >= DECK_LOAD_YIELD_MAX_MS) {
			// The ceiling is the fail-safe that keeps deferred work from being
			// starved. Firing it means a hold (a deck load or the boot listing
			// walk) never settled, which is a defect upstream, so say so loudly
			// instead of quietly paying 10 s on every boot (the #5549 case).
			if (holds > 0) {
				(host.warn ?? console.warn)(
					`[boot-scheduler] WARN released ${queue.length} deferred boot task(s) at the ` +
						`${DECK_LOAD_YIELD_MAX_MS} ms ceiling with ${holds} hold(s) never settled ` +
						`(a deck load or the boot listing walk): ${queue.map((entry) => entry.label).join(', ')}`
				);
			}
			_release();
			return;
		}
		yieldedMs += DECK_LOAD_YIELD_POLL_MS;
		timer = host.setTimer(_releaseOnceDecksAreFree, DECK_LOAD_YIELD_POLL_MS);
	}

	function _onQuietElapsed(): void {
		timer = null;
		if (host.whenIdle === null) {
			_releaseOnceDecksAreFree();
			return;
		}
		host.whenIdle(_releaseOnceDecksAreFree, BOOT_IDLE_TIMEOUT_MS);
	}

	function _arm(): void {
		if (armed || released) return;
		armed = true;
		timer = host.setTimer(_onQuietElapsed, BOOT_QUIET_MS);
	}

	return {
		defer(label: string, task: () => void): void {
			if (released) {
				_run({ label, task });
				return;
			}
			queue.push({ label, task });
			_arm();
		},

		deckLoadStarted: _hold,
		listingWalkStarted: _hold,

		start(): () => void {
			_arm();
			return () => {
				// The page that owns this queue is going away with it, so
				// there is nothing left for the queued work to serve. Cancel
				// the timer rather than fire a burst into a dying document.
				_cancelTimer();
				armed = false;
			};
		}
	};
}

interface IdleScope {
	requestIdleCallback?: (run: () => void, options?: { timeout: number }) => number;
}

function _browserHost(): BootHost {
	const scope = globalThis as unknown as IdleScope;
	const requestIdle =
		typeof scope.requestIdleCallback === 'function'
			? scope.requestIdleCallback.bind(globalThis)
			: null;
	return {
		setTimer: (run, ms) => setTimeout(run, ms) as unknown as number,
		clearTimer: (handle) => clearTimeout(handle),
		whenIdle:
			requestIdle === null
				? null
				: (run, timeoutMs) => {
						requestIdle(run, { timeout: timeoutMs });
					}
	};
}

/** The page's one boot scheduler. */
export const bootScheduler: BootScheduler = createBootScheduler(_browserHost());
