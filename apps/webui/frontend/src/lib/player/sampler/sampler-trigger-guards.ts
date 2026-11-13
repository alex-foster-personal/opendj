/**
 * Click guards for an edge tab's triggers (spec 7.2, EDGESAMP-11872g): a click counts only on
 * a tab wide enough to read (CLICK_MIN_W) and once the edge has been awake CLICK_SETTLE_MS, and
 * a second click on the same tab within TAB_DEBOUNCE_MS is dropped, so a sweep across a waking
 * edge or a bouncing mouse never fires a sample. Pure: the caller passes the clock.
 */
import { clickAllowed } from './edge-wave';

/** A second click on the same tab inside this window is a bounce, not a new press. */
export const TAB_DEBOUNCE_MS = 120;

export type TriggerVerdict = 'ok' | 'too-narrow-or-settling' | 'debounced';

export interface TriggerClick {
	/** the tab clicked (its slot id); every control of one tab shares the key */
	key: string;
	widthPx: number;
	msSinceWake: number;
	/** the caller's clock, ms */
	now: number;
}

/** Why a click on a tab is refused, or 'ok'; `lastAt` is the last accepted click on that tab. */
export function triggerVerdict(c: TriggerClick, lastAt: number | undefined): TriggerVerdict {
	if (!clickAllowed(c.widthPx, c.msSinceWake)) return 'too-narrow-or-settling';
	if (lastAt !== undefined && c.now - lastAt < TAB_DEBOUNCE_MS) return 'debounced';
	return 'ok';
}

export interface TriggerGuard {
	/** true when the click counts; an accepted click starts that tab's debounce window */
	accept(c: TriggerClick): boolean;
}

/** One guard per edge: remembers the last accepted click per tab. */
export function createTriggerGuard(): TriggerGuard {
	const lastAt = new Map<string, number>();
	return {
		accept(c) {
			if (triggerVerdict(c, lastAt.get(c.key)) !== 'ok') return false;
			lastAt.set(c.key, c.now);
			return true;
		}
	};
}
