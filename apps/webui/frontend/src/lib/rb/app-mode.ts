/**
 * PERFMODE-07 / PERFMODE-13 app-mode selector contract.
 *
 * `/performance` remains the rekordbox Performance-mode UI route. The chooser
 * label for that route is Gig (issue #2039). Route rename is out of scope.
 *
 * A mode transition is a same-origin route, rather than private component
 * state. That makes the human menu and an agent's HTTP navigation identical.
 *
 * PERFMODE-13 (#2699) advertises four selectable modes with gain/lose copy.
 * Removed app_mode.show_unbuildable in #2699; all modes advertised.
 *
 * v1 (issue #2041) adds per-mode feature-flag maps: performance keeps
 * usb.export and local_stems.executor in-scope; other modes are empty.
 * Maps cannot set APP_MODES.available.
 */

import type { AppModeIconId } from './app-mode-icons';

export type AppModeId = 'performance' | 'library-management' | 'library' | 'music-player';

export interface AppMode {
	id: AppModeId;
	label: string;
	href: string;
	iconId: AppModeIconId;
	gain: string;
	lose: string;
	available: boolean;
}

export const APP_MODES: readonly AppMode[] = [
	{
		id: 'performance',
		label: 'Gig',
		href: '/performance',
		iconId: 'gig',
		gain: 'Four-deck live surface with the full audio engine',
		lose: 'Highest RAM and CPU',
		available: true
	},
	{
		id: 'library-management',
		label: 'Prep',
		href: '/prep',
		iconId: 'prep',
		gain: 'Analysis, stems, tagging, and playlist work with background workers on',
		lose: 'No live decks',
		available: true
	},
	{
		id: 'library',
		label: 'Library',
		href: '/',
		iconId: 'library',
		gain: 'Browse and search the library',
		lose: 'No decks, no workers; lowest RAM and CPU',
		available: true
	},
	{
		id: 'music-player',
		label: 'Trackify',
		href: '/music-player',
		iconId: 'trackify',
		gain: 'Single-deck listening with autoplay; runs unsupervised',
		lose: 'No mixer, no stems',
		available: true
	}
];

export const APP_MODE_FEATURE_FLAG_IDS: Readonly<Record<AppModeId, readonly string[]>> = {
	performance: ['usb.export', 'local_stems.executor'],
	'library-management': [],
	library: [],
	'music-player': []
};

export const LOCAL_STEMS_EXECUTOR_FLAG_ID = 'local_stems.executor';

export function modeFeatureEnabled(modeId: AppModeId, flagId: string): boolean {
	const known = new Set<string>();
	for (const ids of Object.values(APP_MODE_FEATURE_FLAG_IDS)) {
		for (const id of ids) known.add(id);
	}
	if (!known.has(flagId)) {
		throw new Error(
			`undeclared feature flag ${flagId}: ` +
				'add a FlagDef to apps/feature_flags/store.FLAGS before reading it.'
		);
	}
	return APP_MODE_FEATURE_FLAG_IDS[modeId].includes(flagId);
}

export function appModeForPath(pathname: string): AppMode {
	const mode = APP_MODES.find((candidate) => candidate.href === pathname);
	if (mode === undefined) {
		throw new Error(`app mode has no route for pathname: ${pathname}`);
	}
	return mode;
}
