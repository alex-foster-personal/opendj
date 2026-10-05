/**
 * AGENT-18: the UI-mirror publish, gated on tab leadership and the engine's
 * mirror lease. Split out of `ui-mirror.ts` (which reads the live audio engine
 * and cannot load under node:test) so the gating itself is unit tested.
 *
 * - A tab that does not hold the local Web Lock sends NOTHING to the engine.
 * - A tab holding the lock but refused the lease (409 `lease_held`, another
 *   browser leads) stops PUTting and instead reads `GET .../ui-mirror/lease`
 *   every LEASE_RECHECK_MS, so it never earns a stream of 409 console errors.
 *   When the lease is free or lapsed it resumes publishing on its own.
 * - Every PUT names its writer in `x-opendj-lease`; the first PUT after
 *   "Take control" also carries `x-opendj-lease-takeover: 1`.
 */
import type { TabLeadership } from './tab-leadership';

export const MIRROR_PATH = '/api/v1/state/ui-mirror';
export const LEASE_PATH = '/api/v1/state/ui-mirror/lease';
export const LEASE_HEADER = 'x-opendj-lease';
export const LEASE_TAKEOVER_HEADER = 'x-opendj-lease-takeover';
/** How often a lease-blocked tab asks whether the other browser has let go. */
export const LEASE_RECHECK_MS = 2000;

interface MirrorLeaseState {
	held: boolean;
	holder: string | null;
}

export interface LeasedMirrorPublisher {
	/** One publish tick: PUT as leader, recheck the lease when blocked, else nothing. */
	publish(): void;
	/** True only while the engine has accepted this tab's latest mirror PUT. */
	isRegistered(): boolean;
	/** The engine answered 409 to an order poll: it no longer has this page on record. */
	forget(): void;
}

export function createLeasedMirrorPublisher(deps: {
	leadership: TabLeadership;
	clientId: string;
	build: () => Record<string, unknown>;
	/** Called just before each leader PUT (the stall detector hangs off it). */
	beforePublish?: (nowMs: number) => void;
	now?: () => number;
}): LeasedMirrorPublisher {
	const now = deps.now ?? Date.now;
	let registered = false;
	let leaseCheckInFlight = false;
	let lastLeaseCheckAtMs = Number.NEGATIVE_INFINITY;

	const checkLease = (): void => {
		const nowMs = now();
		if (leaseCheckInFlight || nowMs - lastLeaseCheckAtMs < LEASE_RECHECK_MS) return;
		leaseCheckInFlight = true;
		lastLeaseCheckAtMs = nowMs;
		void fetch(LEASE_PATH)
			.then(async (response) => {
				if (!response.ok) {
					console.error(`ui-mirror lease read failed: ${response.status}`);
					return;
				}
				const lease = (await response.json()) as MirrorLeaseState;
				if (!lease.held || lease.holder === deps.clientId) deps.leadership.noteLeaseFree();
			})
			.catch(() => {
				// Engine down: stay demoted and ask again on a later tick.
			})
			.finally(() => {
				leaseCheckInFlight = false;
			});
	};

	const publish = (): void => {
		if (!deps.leadership.holdsLocalLock()) {
			registered = false;
			return;
		}
		if (!deps.leadership.isLeader()) {
			registered = false;
			checkLease();
			return;
		}
		deps.beforePublish?.(now());
		const headers: Record<string, string> = {
			'content-type': 'application/json',
			[LEASE_HEADER]: deps.clientId
		};
		if (deps.leadership.consumeTakeover()) headers[LEASE_TAKEOVER_HEADER] = '1';
		void fetch(MIRROR_PATH, { method: 'PUT', headers, body: JSON.stringify(deps.build()) })
			.then(async (response) => {
				registered = response.ok && deps.leadership.isLeader();
				if (response.status !== 409) return;
				const refusal = (await response.json()) as { reason?: string; holder?: unknown };
				if (refusal.reason === 'lease_held' && typeof refusal.holder === 'string') {
					deps.leadership.noteLeaseConflict(refusal.holder);
				} else if (refusal.reason === 'stale_snapshot') {
					// Our own slow PUT overtaken by a newer one: the engine kept the
					// newer snapshot, which is the point. Nothing to demote.
					console.info('ui-mirror: an out-of-order publish was dropped by the engine');
				} else {
					// Not the lease: a real defect, logged rather than absorbed.
					console.error(`ui-mirror publish refused: ${JSON.stringify(refusal)}`);
				}
			})
			.catch(() => {
				// Engine down / WebKit `Load failed`: do not become an
				// unhandledrejection (Sentry OPEN-DJ-FE-F). Forget registration
				// until a later PUT is accepted.
				registered = false;
			});
	};

	return {
		publish,
		isRegistered: () => registered && deps.leadership.isLeader(),
		forget: () => {
			registered = false;
		}
	};
}
