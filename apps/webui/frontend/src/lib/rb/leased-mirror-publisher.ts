/**
 * AGENT-18: the UI-mirror publish, gated on tab leadership and the engine's
 * mirror lease. Split out of `ui-mirror.ts` (which reads the live audio engine
 * and cannot load under node:test) so the gating itself is unit tested.
 *
 * - Only the leader PUTs. Every PUT names its writer in `x-opendj-lease`; the
 *   first PUT after "Take control" (or a fresh user gesture) also carries
 *   `x-opendj-lease-takeover: 1`.
 * - A follower never PUTs blind. Every LEASE_RECHECK_MS, unless it is hidden
 *   and silent, it reads `GET .../ui-mirror/lease` and CLAIMS leadership when
 *   the right tab is not leading (Mon 5 Oct 2026: an idle, backgrounded Chrome
 *   tab held the lease while an agent pane played the set, so the mirror said
 *   nothing was playing):
 *     - the lease is free (a lock holder only, so siblings never race at open);
 *     - the holder is hidden and silent (`holder_yieldable`) and this tab is visible;
 *     - this tab is playing and the holder is not (`holder_playing` false);
 *     - the operator touched this tab in the last GESTURE_CLAIM_MS and the
 *       holder is not playing (claimed with takeover).
 *   A claim that the engine refuses (409) demotes the tab again.
 */
import type { TabLeadership } from './tab-leadership';

export const MIRROR_PATH = '/api/v1/state/ui-mirror';
export const LEASE_PATH = '/api/v1/state/ui-mirror/lease';
export const LEASE_HEADER = 'x-opendj-lease';
export const LEASE_TAKEOVER_HEADER = 'x-opendj-lease-takeover';
/** How often a follower asks the engine who leads and whether to claim. */
export const LEASE_RECHECK_MS = 2000;
/** A user gesture this recent makes this tab the operator's tab. */
export const GESTURE_CLAIM_MS = 10_000;

export interface MirrorLeaseState {
	held: boolean;
	holder: string | null;
	holder_playing?: boolean | null;
	holder_yieldable?: boolean | null;
}

export type ClaimDecision = { claim: false } | { claim: true; takeover: boolean; why: string };

/** Pure: should this follower claim leadership, given what the engine says? */
export function decideFollowerClaim(input: {
	lease: MirrorLeaseState;
	selfId: string;
	holdsLocalLock: boolean;
	visible: boolean;
	playing: boolean;
	gestureAgeMs: number | null;
}): ClaimDecision {
	const { lease } = input;
	if (lease.held && lease.holder === input.selfId) {
		// Our own lease outlived a lock a sibling just took: only a lock holder resumes.
		return input.holdsLocalLock ? { claim: true, takeover: false, why: 'ours' } : { claim: false };
	}
	if (!lease.held) {
		return input.holdsLocalLock ? { claim: true, takeover: false, why: 'free' } : { claim: false };
	}
	const holderPlaying = lease.holder_playing === true;
	if (input.playing && !holderPlaying) return { claim: true, takeover: false, why: 'audible' };
	if (input.visible && lease.holder_yieldable === true) return { claim: true, takeover: false, why: 'holder-hidden-idle' };
	if (input.gestureAgeMs !== null && input.gestureAgeMs <= GESTURE_CLAIM_MS && !holderPlaying) {
		return { claim: true, takeover: true, why: 'gesture' };
	}
	return { claim: false };
}

export interface LeasedMirrorPublisher {
	/** One publish tick: PUT as leader, or read the lease and maybe claim as follower. */
	publish(): void;
	/** True only while the engine has accepted this tab's latest mirror PUT. */
	isRegistered(): boolean;
	/** The engine answered 409 to an order poll: it no longer has this page on record. */
	forget(): void;
	/** A user gesture landed in this tab: recheck the lease on the next tick. */
	noteGesture(): void;
}

export function createLeasedMirrorPublisher(deps: {
	leadership: TabLeadership;
	clientId: string;
	build: () => Record<string, unknown>;
	/** Any deck audibly playing in THIS tab. */
	isPlaying: () => boolean;
	/** `document.visibilityState === 'visible'`. */
	isVisible: () => boolean;
	/** Called just before each leader PUT (the stall detector hangs off it). */
	beforePublish?: (nowMs: number) => void;
	now?: () => number;
}): LeasedMirrorPublisher {
	const now = deps.now ?? Date.now;
	let registered = false;
	let leaseCheckInFlight = false;
	let lastLeaseCheckAtMs = Number.NEGATIVE_INFINITY;
	let lastGestureAtMs: number | null = null;

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
				const decision = decideFollowerClaim({
					lease,
					selfId: deps.clientId,
					holdsLocalLock: deps.leadership.holdsLocalLock(),
					visible: deps.isVisible(),
					playing: deps.isPlaying(),
					gestureAgeMs: lastGestureAtMs === null ? null : now() - lastGestureAtMs
				});
				if (!decision.claim) return;
				console.info(`ui-mirror: claiming leadership (${decision.why})`);
				if (decision.takeover) lastGestureAtMs = null;
				deps.leadership.claim({ takeover: decision.takeover });
			})
			.catch(() => {
				// Engine down: stay a follower and ask again on a later tick.
			})
			.finally(() => {
				leaseCheckInFlight = false;
			});
	};

	const publish = (): void => {
		if (!deps.leadership.isLeader()) {
			registered = false;
			// A hidden, silent follower stays completely quiet toward the engine.
			if (deps.isVisible() || deps.isPlaying()) checkLease();
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
		},
		noteGesture: () => {
			lastGestureAtMs = now();
			lastLeaseCheckAtMs = Number.NEGATIVE_INFINITY;
		}
	};
}
