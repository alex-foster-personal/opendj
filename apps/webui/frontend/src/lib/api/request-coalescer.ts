/**
 * One request per endpoint per boot, shared by every caller that wants it.
 *
 * WHY THIS EXISTS (measured, Wed 9 Sep 2026, this worktree, production
 * bundle served by the engine). The /performance route opened
 * `GET /api/v1/health` FOUR times and `GET /api/v1/ui-prefs` TWICE inside the
 * app's startup wave, from six distinct call sites:
 *
 *   health     capabilities.probe()              (+layout mount)
 *   health     refreshHealth() -> getHealth()    (+layout mount)
 *   ui-prefs   hydrateConfirmPrefsFromDisk()     (+layout mount)
 *   health     BrowserPanel._init() -> getHealth()
 *   ui-prefs   hydrateConfirmPrefsFromDisk()     (BrowserPanel)
 *   health     pingHealth() (liveness dot, NOT coalesced -- below)
 *
 * Each caller genuinely needs the answer, so deleting call sites would be
 * deleting behavior. What they do not need is to each pay for it. The
 * daemon is single-worker by design (workers=1 is LOCKED) and the browser
 * gives one origin six connections, so a redundant boot request both queues
 * on the server and holds a connection the deck load wants. See
 * `src/lib/rb/boot-scheduler.ts` for the ordering half of the same problem;
 * this module is the deduplication half.
 *
 * WHY A TTL RATHER THAN A PLAIN IN-FLIGHT SHARE. Only the two mount waves
 * overlap exactly; the two waves themselves are 77-149ms apart (five runs),
 * so an in-flight-only coalescer would catch two of the three duplicates
 * and miss the wave boundary entirely. The window therefore has to outlive
 * a settled response by a little. It must ALSO be far below the shortest
 * legitimate re-read cadence, or it stops being a coalescer and becomes a
 * cache that silently pins stale data: `refreshHealth` polls every 30s and
 * the library dot pings every 2.5s. `BOOT_COALESCE_TTL_MS` sits an order of
 * magnitude above the widest measured wave gap and an order of magnitude
 * below the fastest of those cadences.
 *
 * WHAT IS DELIBERATELY NOT COALESCED. `pingHealth()` (the Backend liveness
 * dot) is a LIVENESS probe, not a body read: it sets `cache: 'no-store'`,
 * carries its own abort timeout, and exists to notice that the daemon has
 * stopped answering. Handing it a response from up to TTL ago would make
 * the dot report a daemon that is already gone, which is exactly the
 * correctness-for-a-request trade this project has paid for once already
 * (a deferred recorder check cost ~15s of a live set's observation). One
 * request is cheaper than a wrong green dot.
 *
 * A FAILED CALL IS NEVER SHARED FORWARD. A rejection drops the entry
 * immediately, so the next caller retries rather than inheriting a verdict
 * of "we never found out". This matches the rule the capability probe
 * already carried on its own memo.
 *
 * THE SHARED VALUE IS SHARED, not copied. Every joined caller receives the
 * same resolved object, so callers must treat it as read-only. Both current
 * consumers do: they read fields off it and spread them into their own
 * state.
 *
 * VERIFIED BY A/B, Wed 9 Sep 2026, `just boot-burst-bench` with
 * `BOOT_BURST_LATENCY_MS=150`, 6 measured boots per arm after a discarded
 * warmup, production bundle served by the real single-worker engine. The
 * bench counts requests to these endpoints over the app's own startup wave
 * (`boot-burst.spec.ts`, `BOOT_WINDOW_MS`):
 *
 *   arm       health@boot   ui-prefs@boot
 *   before        4               2
 *   after         3               2
 *
 * Identical in every boot of both arms, so the count is the mechanism and not
 * the machine.
 *
 * RE-MEASURED Wed 9 Sep 2026, review round 5, after fixing the two BLOCKING
 * threads a follow-up round raised against "after" above: health@boot moved
 * from 3 to 4, and the reason is not a regression of the coalescing this
 * module does. Thread 1 required that a consumer who already consumed a
 * shared, settled zero-track snapshot get refreshed the moment the bus first
 * opens, not only on a reconnect (events-bus.ts now fires a resync reason
 * `'initial-connect'` for exactly this). BrowserPanel's own resync handler
 * answers that by re-running its library refresh, which reads health with
 * `fresh: true` -- deliberately bypassing this coalescer (see the comment on
 * `getHealth` in api.ts: "correctness beats one request", the same rule
 * `_refreshLibraryRowsOnce`'s three other triggers already pay). So the
 * fourth request is a DIFFERENT request doing different work, not the
 * duplicate this module was built to remove: the mount-wave duplication this
 * module coalesces is still 4 -> 3, unchanged. The one hazard this ordering
 * created -- an in-flight health entry force-reissuing on that same
 * first-open resync, intermittently costing a FIFTH request depending on
 * timing -- is closed by `forceInFlight: false` (see `invalidate` below and
 * its call site in api.ts), which is why health@boot is exactly 4 on every
 * boot rather than 4-or-5.
 *
 * WHAT SHIPPED IS SMALLER THAN WHAT WAS TRIED, and the difference is the
 * interesting part. Two of the four health reads join here. The other two do
 * not, each for its own reason:
 *
 *   - `pingHealth()` is a LIVENESS probe (see above). One request is cheaper
 *     than a wrong green dot.
 *   - the capability probe reads the RAW BYTES to tell a legacy daemon from
 *     an engine one, and carries its own memoization with its own rules.
 *     Joining it was tried and reverted: a 2s TTL changes what "the daemon is
 *     legacy" means, because a daemon whose identity changed inside the window
 *     would keep reporting the identity it had at the start of it.
 *     `daemon-capabilities.test.mjs` refuses it, correctly.
 *
 * `/api/v1/ui-prefs` is NOT coalesced yet either, though it duplicates the
 * same way (2 reads, from the layout and the browser panel). It is blocked on
 * the test harness rather than on the design: `load-typescript.mjs` bundles
 * each module independently, so every `loadTypeScriptModule` call gets its own
 * singleton and a test cannot drop the shared read the way a write does. Doing
 * it properly needs an injected coalescer at that seam, which is its own
 * change. Queued rather than forced.
 *
 * WHAT THIS DID NOT DO, stated so nobody reads a request count as a latency
 * win: fetchWall did not move (before 233ms; after 218ms, and 270/235ms on
 * earlier runs of a larger version of this change), i.e. run-to-run spread,
 * not an effect. The bench fixture is two generated tracks, so its whole boot
 * burst does not establish latency improvement on a real library. One
 * fewer request against a single-worker daemon is the banked result.
 *
 * Requirements (mini-PRD):
 *   OK  two concurrent callers of one key issue ONE underlying request.
 *       [if each concurrent caller runs its own request then nothing has
 *        been deduplicated and this module is pointless]
 *   OK  a caller arriving within the TTL of a settled call reuses it.
 *       [if it re-requests then the two mount waves stay two requests and
 *        the measured duplicate survives]
 *   OK  a caller arriving after the TTL runs a FRESH request.
 *       [if a stale promise is reused forever then this is a cache with no
 *        invalidation, which is a bug waiting for its first stale read]
 *   OK  a rejected call is not retained.
 *       [if a failure is cached then a daemon that was down for one request
 *        stays down for every caller inside the window]
 *   OK  invalidate(key) drops the entry, so a write can drop its own read.
 *       [if a write cannot drop the read it just invalidated then a caller
 *        can read back the value it has already replaced]
 *   OK  distinct keys never share a request.
 *       [if two endpoints collide on one entry then a caller is handed
 *        another endpoint's body, typed as its own]
 *   OK  invalidate(key, { forceInFlight: false }) leaves an in-flight
 *       request to settle and cache normally instead of forcing a second,
 *       redundant request.
 *       [if it forced a re-issue anyway then a reason that carries no risk
 *        of staleness (the bus's first-ever open, PR #1656 review round 5)
 *        would cost a request racing the one already in flight, on some
 *        boots and not others depending on timing -- exactly the per-boot
 *        nondeterminism review round 4's Thread 2 was about]
 */

/** How long a settled response may be reused.
 *
 * Derivation, not taste: the widest measured gap between the two mount
 * waves was 149ms, and the fastest legitimate re-read of a coalesced
 * endpoint is the 2500ms library liveness cadence. 2000ms clears the first
 * by 13x while staying under the second, so no polling caller can ever be
 * served a response it was polling to replace. */
export const BOOT_COALESCE_TTL_MS = 2_000;

/** The clock, injected so tests drive time instead of spending it. */
export interface CoalesceHost {
	now: () => number;
}

export interface Coalescer {
	/**
	 * Run `request` under `key`, or join the call already running or
	 * recently settled under it.
	 *
	 * `ttlMs` is how long a SETTLED result stays joinable; an in-flight call
	 * is always joinable regardless of it.
	 */
	share: <T>(key: string, ttlMs: number, request: () => Promise<T>) => Promise<T>;
	/**
	 * Drop `key`, so the next `share` issues a fresh request. Call this
	 * after a write that changes what a read of `key` would answer.
	 *
	 * `forceInFlight` (default true) governs an entry that is still in
	 * flight when this fires: true forces its eventual answer to be
	 * discarded in favor of a fresh request (the answer may already be
	 * stale against whatever this invalidation is reacting to). Pass false
	 * when the reason for invalidating carries no such risk -- the in-flight
	 * request was already asking the question this invalidation would
	 * otherwise force it to ask again -- so it is left to settle and cache
	 * normally instead of paying for a second, redundant request.
	 */
	invalidate: (key: string, options?: { forceInFlight?: boolean }) => void;
}

interface Entry {
	promise: Promise<unknown>;
	/** null while in flight; the clock reading at resolution afterwards. */
	settledAt: number | null;
	/** Set by invalidate() while this entry is still in flight, so its own
	 * eventual answer (captured before whatever changed) is discarded in
	 * favor of a fresh request rather than handed to its existing waiters. */
	invalidated: boolean;
}

/** Build a coalescer over one clock. Exported for tests, which drive a fake
 * one; the application uses the `requestCoalescer` singleton below. */
export function createCoalescer(host: CoalesceHost): Coalescer {
	const entries = new Map<string, Entry>();

	function _joinable(entry: Entry, ttlMs: number): boolean {
		if (entry.settledAt === null) return true;
		return host.now() - entry.settledAt < ttlMs;
	}

	function share<T>(key: string, ttlMs: number, request: () => Promise<T>): Promise<T> {
		const existing = entries.get(key);
		if (existing !== undefined && _joinable(existing, ttlMs)) {
			return existing.promise as Promise<T>;
		}
		const entry: Entry = { promise: request(), settledAt: null, invalidated: false };
		entries.set(key, entry);
		entry.promise = entry.promise.then(
			(value) => {
				if (entry.invalidated) {
					// The world changed while this specific request was still in
					// flight, so its answer was already stale the moment it
					// arrived: every waiter holding THIS promise (not just the
					// next caller) gets a fresh request's result instead.
					if (entries.get(key) === entry) entries.delete(key);
					return share(key, ttlMs, request);
				}
				entry.settledAt = host.now();
				return value;
			},
			(err) => {
				// Identity-checked so a late failure cannot evict a newer
				// entry that has already replaced this one.
				if (entries.get(key) === entry) entries.delete(key);
				throw err;
			}
		);
		return entry.promise as Promise<T>;
	}

	function invalidate(key: string, options: { forceInFlight?: boolean } = {}): void {
		const entry = entries.get(key);
		if (entry === undefined) return;
		if (entry.settledAt === null) {
			if (options.forceInFlight === false) return;
			entry.invalidated = true;
		}
		entries.delete(key);
	}

	return { share, invalidate };
}

/** The page's one request coalescer. */
export const requestCoalescer: Coalescer = createCoalescer({
	now: () => Date.now()
});
