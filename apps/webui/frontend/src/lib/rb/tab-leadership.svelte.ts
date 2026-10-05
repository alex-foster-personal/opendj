/**
 * AGENT-18 reactive view of this tab's leadership, for the follower banner.
 *
 * `ui-mirror.ts` owns the controller (it is the thing the leadership gates);
 * this file only holds what the banner renders and the one action it offers.
 */
import type { TabLeadership, TabLeadershipSnapshot } from './tab-leadership';

const PENDING: TabLeadershipSnapshot = { role: 'pending', reason: null, leaseHolder: null };

export const tabLeadershipView = $state<{ current: TabLeadershipSnapshot }>({ current: PENDING });

let _active: TabLeadership | null = null;

export function bindTabLeadership(leadership: TabLeadership | null): void {
	_active = leadership;
	tabLeadershipView.current = leadership === null ? PENDING : leadership.snapshot();
}

export function publishTabLeadership(snapshot: TabLeadershipSnapshot): void {
	tabLeadershipView.current = snapshot;
}

/** The banner's "Take control" button. */
export function takeTabControl(): void {
	if (_active === null) throw new Error('take control pressed with no /performance leadership installed');
	_active.takeControl();
}
