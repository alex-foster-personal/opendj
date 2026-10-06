/**
 * AGENT-18 reactive view of this tab's leadership, for the follower banner,
 * and the one place the /performance route creates its leadership.
 *
 * The route owns the leadership because more than the UI mirror hangs off it:
 * session restore, the rescue ring, the play counter and the deck observer
 * all run only while this tab leads (`leader-only-writers.ts`).
 */
import {
	createTabLeadership,
	type LockManagerLike,
	type TabLeadership,
	type TabLeadershipSnapshot
} from './tab-leadership';

const PENDING: TabLeadershipSnapshot = { role: 'pending', reason: null, leaseHolder: null };

export const tabLeadershipView = $state<{ current: TabLeadershipSnapshot }>({ current: PENDING });

let _active: TabLeadership | null = null;

function _browserLocks(): LockManagerLike | null {
	const locks = (globalThis.navigator as Navigator | undefined)?.locks;
	return locks === undefined ? null : (locks as unknown as LockManagerLike);
}

/** Create this tab's leadership and bind the banner to it. Dispose on unmount. */
export function installTabLeadership(): { leadership: TabLeadership; dispose: () => void } {
	const leadership = createTabLeadership({
		locks: _browserLocks(),
		onChange: (snapshot) => {
			tabLeadershipView.current = snapshot;
		}
	});
	_active = leadership;
	tabLeadershipView.current = leadership.snapshot();
	return {
		leadership,
		dispose: () => {
			leadership.dispose();
			if (_active === leadership) _active = null;
			tabLeadershipView.current = PENDING;
		}
	};
}

/** The banner's "Take control" button. */
export function takeTabControl(): void {
	if (_active === null) throw new Error('take control pressed with no /performance leadership installed');
	_active.takeControl();
}
