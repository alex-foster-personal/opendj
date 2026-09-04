/**
 * PERFMODE-07 app-mode selector contract.
 *
 * A mode transition is a same-origin route, rather than private component
 * state. That makes the human menu and an agent's HTTP navigation identical.
 * A mode whose real feature contract does not exist stays fail-closed.
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
		label: 'Performance',
		href: '/performance',
		thumbnail: 'decks',
		description: 'Four-deck performance surface with the audio engine and live controls.',
		available: true
	},
	{
		id: 'library-management',
		label: 'Library Management',
		href: '/',
		thumbnail: 'library',
		description: 'Manage the local library and leave background workflows available.',
		available: true
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

export function appModeForPath(pathname: string): AppMode {
	const mode = APP_MODES.find((candidate) => candidate.href === pathname);
	if (mode === undefined) {
		throw new Error(`app mode has no route for pathname: ${pathname}`);
	}
	return mode;
}
