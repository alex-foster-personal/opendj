/**
 * PERFMODE-07 app-mode selector contract.
 *
 * `/performance` remains the rekordbox Performance-mode UI route. The chooser
 * label for that route is Gig (issue #2039). Route rename is out of scope.
 *
 * A mode transition is a same-origin route, rather than private component
 * state. That makes the human menu and an agent's HTTP navigation identical.
 * A mode whose real feature contract does not exist stays fail-closed.
 *
 * v1 (issue #2040) does not advertise unbuilt modes on the chooser unless
 * app_mode.show_unbuildable is on; the rows stay in APP_MODES fail-closed.
 */

export type AppModeId = 'performance' | 'library-management' | 'music-player';

export interface AppMode {
	id: AppModeId;
	label: string;
	href: string;
	thumbnail: 'decks' | 'library' | 'player';
	description: string;
	available: boolean;
	unavailableReason?: string;
}

export const APP_MODES: readonly AppMode[] = [
	{
		id: 'performance',
		label: 'Gig',
		href: '/performance',
		thumbnail: 'decks',
		description: 'Four-deck live surface with the audio engine and live controls.',
		available: true
	},
	{
		id: 'library-management',
		label: 'Library Management',
		href: '/',
		thumbnail: 'library',
		description: 'Planned: manage the local library while background workflows continue.',
		available: false,
		unavailableReason: 'Library Management mode is not implemented. The existing library page is not this mode.'
	},
	{
		id: 'music-player',
		label: 'Music Player',
		href: '/music-player',
		thumbnail: 'player',
		description: 'Low-resource listening with autoplay and streaming-service control.',
		available: false,
		unavailableReason: 'Music Player is unavailable until its real playback contract is implemented.'
	}
];

export const SHOW_UNBUILDABLE_APP_MODES_FLAG_ID = 'app_mode.show_unbuildable';

export function showUnbuildableAppModes(
	flagsLoaded: boolean,
	flag: { enabled: boolean } | null
): boolean {
	if (!flagsLoaded) return false;
	if (flag === null) {
		throw new Error(
			`undeclared feature flag ${SHOW_UNBUILDABLE_APP_MODES_FLAG_ID}: ` +
				'add a FlagDef to apps/feature_flags/store.FLAGS before reading it.'
		);
	}
	return flag.enabled;
}

export function chooserAppModes(showUnbuildable: boolean): readonly AppMode[] {
	if (showUnbuildable) return APP_MODES;
	return APP_MODES.filter((mode) => mode.available);
}

export function appModeForPath(pathname: string): AppMode {
	const mode = APP_MODES.find((candidate) => candidate.href === pathname);
	if (mode === undefined) {
		throw new Error(`app mode has no route for pathname: ${pathname}`);
	}
	return mode;
}
