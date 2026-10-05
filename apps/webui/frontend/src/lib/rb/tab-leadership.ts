/**
 * AGENT-18: exactly one /performance tab per engine drives the agent-order bus
 * and the UI mirror.
 *
 * WHY. Every open /performance tab used to poll `GET /api/v1/commands/next`
 * every 50 ms and `PUT /api/v1/state/ui-mirror` every second. With the maintainer's
 * Chrome plus two agent browser panes open on one engine (Mon 5 Oct 2026), an
 * order ran in whichever tab claimed it first, and the engine's single view of
 * deck and transport state (DeckGate, autoplay, watchdogs) flipped between tabs
 * every second, so a stopped tab overwrote a playing one.
 *
 * TWO LAYERS, because they cover different scopes:
 * - Web Locks elect one leader among tabs of ONE browser. The lock is held for
 *   the tab's lifetime and the browser hands it to the next queued tab the
 *   moment the leader closes or crashes. "Take control" steals it.
 * - The server-side lease on `PUT /state/ui-mirror` (see
 *   `apps/webui/server/routes/state.py`) arbitrates between BROWSERS (Chrome vs
 *   an agent's Electron pane vs the desktop WKWebView), which share no locks.
 *   A 409 from it demotes this tab until the lease is free again.
 *
 * Where Web Locks are absent (an insecure origin such as a LAN IP over http),
 * the tab counts as holding the local lock and the server lease is the only
 * arbiter. That is deliberate, not a silent fallback: the lease alone still
 * gives one writer per engine.
 *
 * This module is the pure controller; the reactive view for the banner lives
 * in `tab-leadership.svelte.ts`, and `ui-mirror.ts` wires both to the network.
 */

export const PERFORMANCE_LEADER_LOCK = 'opendj-performance-leader';

export type TabRole = 'pending' | 'leader' | 'follower';
/** Why this tab is a follower: a sibling tab in this browser, or another browser's lease. */
export type FollowerReason = 'another-tab' | 'another-browser';

export interface TabLeadershipSnapshot {
	role: TabRole;
	reason: FollowerReason | null;
	/** The server lease holder's client id while another browser leads. */
	leaseHolder: string | null;
}

/** The slice of the Web Locks `LockManager` this controller uses. */
export interface LockManagerLike {
	request(
		name: string,
		options: { ifAvailable?: boolean; steal?: boolean; signal?: AbortSignal },
		callback: (lock: unknown) => unknown
	): Promise<unknown>;
}

export interface TabLeadership {
	snapshot(): TabLeadershipSnapshot;
	/** True only when this tab holds the local lock AND no other browser holds the lease. */
	isLeader(): boolean;
	/** True when this tab holds the local lock (it may still be lease-blocked). */
	holdsLocalLock(): boolean;
	/** The operator pressed "Take control": steal the lock and the lease. */
	takeControl(): void;
	/** Returns true once after `takeControl`: the next mirror PUT asks for takeover. */
	consumeTakeover(): boolean;
	/** The engine refused our mirror PUT because `holder` holds the lease. */
	noteLeaseConflict(holder: string): void;
	/** The engine's lease is free, expired, or ours. */
	noteLeaseFree(): void;
	dispose(): void;
}

function _isAbort(error: unknown): boolean {
	return error instanceof Error && error.name === 'AbortError';
}

export function createTabLeadership(deps: {
	locks: LockManagerLike | null;
	onChange: (snapshot: TabLeadershipSnapshot) => void;
	lockName?: string;
}): TabLeadership {
	const lockName = deps.lockName ?? PERFORMANCE_LEADER_LOCK;
	const locks = deps.locks;
	let hasLock = locks === null;
	let decided = locks === null;
	let leaseHolder: string | null = null;
	let takeoverPending = false;
	let disposed = false;
	let releaseHeld: (() => void) | null = null;
	let queued: AbortController | null = null;

	const snapshot = (): TabLeadershipSnapshot => {
		if (!decided) return { role: 'pending', reason: null, leaseHolder: null };
		if (!hasLock) return { role: 'follower', reason: 'another-tab', leaseHolder: null };
		if (leaseHolder !== null) return { role: 'follower', reason: 'another-browser', leaseHolder };
		return { role: 'leader', reason: null, leaseHolder: null };
	};

	let last = JSON.stringify(snapshot());
	const emit = (): void => {
		if (disposed) return;
		const next = snapshot();
		const key = JSON.stringify(next);
		if (key === last) return;
		last = key;
		deps.onChange(next);
	};

	/** Lock callback: hold the lock until released by dispose, or stolen. */
	const hold = (lock: unknown): unknown => {
		if (lock === null) return undefined;
		if (disposed) return undefined;
		hasLock = true;
		decided = true;
		queued = null;
		emit();
		return new Promise<void>((resolve) => {
			releaseHeld = resolve;
		});
	};

	/** A held lock's request settles when it is released or stolen. */
	const onHeldSettled = (error: unknown): void => {
		releaseHeld = null;
		if (disposed) return;
		if (error !== undefined && !_isAbort(error)) throw error;
		// Stolen by another tab's "Take control" (AbortError), so queue to get it back.
		hasLock = false;
		decided = true;
		emit();
		queue();
	};

	const queue = (): void => {
		if (locks === null || disposed) return;
		const controller = new AbortController();
		queued = controller;
		locks
			.request(lockName, { signal: controller.signal }, hold)
			.then(
				() => onHeldSettled(undefined),
				(error: unknown) => {
					if (controller.signal.aborted) return;
					onHeldSettled(error);
				}
			);
	};

	if (locks !== null) {
		// First ask without waiting, so a second tab learns it is a follower now
		// rather than sitting in "pending" behind a lock that may never free.
		locks
			.request(lockName, { ifAvailable: true }, (lock) => {
				if (lock === null) {
					decided = true;
					emit();
					queue();
					return undefined;
				}
				return hold(lock);
			})
			.then(
				() => {
					if (releaseHeld === null && hasLock) onHeldSettled(undefined);
				},
				(error: unknown) => onHeldSettled(error)
			);
	}

	return {
		snapshot,
		isLeader: () => decided && hasLock && leaseHolder === null,
		holdsLocalLock: () => decided && hasLock,
		takeControl() {
			if (disposed) return;
			takeoverPending = true;
			leaseHolder = null;
			if (locks !== null && !hasLock) {
				queued?.abort();
				queued = null;
				locks.request(lockName, { steal: true }, hold).then(
					() => onHeldSettled(undefined),
					(error: unknown) => onHeldSettled(error)
				);
			}
			emit();
		},
		consumeTakeover() {
			const pending = takeoverPending;
			takeoverPending = false;
			return pending;
		},
		noteLeaseConflict(holder: string) {
			leaseHolder = holder;
			emit();
		},
		noteLeaseFree() {
			leaseHolder = null;
			emit();
		},
		dispose() {
			disposed = true;
			queued?.abort();
			queued = null;
			releaseHeld?.();
			releaseHeld = null;
		}
	};
}
