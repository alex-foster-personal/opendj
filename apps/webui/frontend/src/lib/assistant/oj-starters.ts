/**
 * OJ starter chips (issue #14040, epic #9374): what an empty agent sidebar suggests asking,
 * keyed by route so /performance leads with deck and transition prompts and the library leads
 * with library prompts. Pure, no DOM and no timers, so tests and agents can read the list.
 * A chip fills the composer and never sends; its entry stagger runs only when ojMotionPolicy
 * keeps motion on (none under reduced motion, none while a deck plays on /performance).
 */
import { ojMotionAnimates } from '$lib/components/assistant/oj-glass-motion';
import type { OjMotion } from '$lib/components/assistant/oj-motion-policy';

export type OjStarterRoute = 'performance' | 'library';
export type OjStarterKind = 'deck' | 'transition' | 'set' | 'library';

export interface OjStarter {
	id: string;
	kind: OjStarterKind;
	text: string;
}

const SET_FROM_PLAYLIST: OjStarter = {
	id: 'set-from-playlist',
	kind: 'set',
	text: 'Build a 60 minute set from this playlist'
};
const LIKE_DECK_ONE: OjStarter = {
	id: 'like-deck-1',
	kind: 'deck',
	text: 'Find tracks like the one on deck 1'
};
const TRANSITION_ONE_TO_TWO: OjStarter = {
	id: 'transition-1-to-2',
	kind: 'transition',
	text: 'Suggest a transition from deck 1 into deck 2'
};
const TAG_NEWEST: OjStarter = {
	id: 'tag-newest',
	kind: 'library',
	text: 'Tag my newest 20 tracks'
};
const DODGY_GRIDS: OjStarter = {
	id: 'dodgy-beatgrids',
	kind: 'library',
	text: 'Which tracks have dodgy beatgrids'
};

/** Starters per route, in display order; each list holds three to five chips. */
export const OJ_STARTERS: Readonly<Record<OjStarterRoute, readonly OjStarter[]>> = Object.freeze({
	performance: Object.freeze([LIKE_DECK_ONE, TRANSITION_ONE_TO_TWO, SET_FROM_PLAYLIST]),
	library: Object.freeze([SET_FROM_PLAYLIST, TAG_NEWEST, DODGY_GRIDS, LIKE_DECK_ONE])
});

/** Gap between one chip's entry and the next; the entry itself is the log's 180 ms fade-up. */
export const OJ_STARTER_STAGGER_MS = 60;

/** Which starter list a pathname gets, read the way the container reads /performance for motion. */
export function ojStarterRoute(pathname: string): OjStarterRoute {
	return pathname.startsWith('/performance') ? 'performance' : 'library';
}

/** The chips to show: the route's list while the conversation is empty, none once it has a message. */
export function ojStartersFor(pathname: string, items: readonly unknown[]): readonly OjStarter[] {
	return items.length === 0 ? OJ_STARTERS[ojStarterRoute(pathname)] : [];
}

/** Entry delay step per chip: the stagger when motion animates, 0 (no entry animation) otherwise. */
export function ojStarterStaggerMs(motion: OjMotion): number {
	return ojMotionAnimates(motion) ? OJ_STARTER_STAGGER_MS : 0;
}
