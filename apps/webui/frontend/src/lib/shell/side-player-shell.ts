/**
 * The Spotify side player and the Chrome sign-in import (SPOT-6555b, SPOT-6555c),
 * over the Electron bridge. Kept apart from native-shell.ts so only the lazily
 * loaded Spotify panel pays for it, not the library first paint.
 */

import {
	type Scope,
	type SideLoginImportOutcome,
	electronBridge,
	nativeShellKind,
	openExternal
} from './native-shell';

export type { SideLoginImportOutcome };

/** The Spotify web player opens docked beside Open DJ, not in a whole new browser. */
export const SPOTIFY_PLAYER_ORIGIN = 'https://open.spotify.com';

const PLAYER_TARGET_PATH = /^\/(playlist|album|track|artist)\/[A-Za-z0-9]+\/?$/;

/**
 * The SPOT-6555b target contract, the same one the Electron main process enforces
 * (side-player-layout.ts isSidePlayerOpenTarget): an https open.spotify.com
 * playlist, album, track or artist page with a nonempty id. Checked here too so
 * the browser and Tauri fallback cannot open what Electron refuses.
 */
export function isSpotifyPlayerTarget(url: string): boolean {
	let parsed: URL;
	try {
		parsed = new URL(url);
	} catch {
		return false;
	}
	return (
		parsed.protocol === 'https:' &&
		parsed.hostname === 'open.spotify.com' &&
		PLAYER_TARGET_PATH.test(parsed.pathname)
	);
}

/** True when this shell can dock the Spotify side player (Electron 2+). */
export function hasSidePlayer(scope: Scope = globalThis): boolean {
	return electronBridge(scope)?.sidePlayer !== undefined;
}

/**
 * Open a Spotify URL in the side player. In Electron it docks beside the
 * window (SPOT-6555b). Tauri, and an Electron shell older than the side player,
 * open it in the system browser through their opener, because neither lets
 * window.open through. A browser tab opens a pop-up with no
 * link back to Open DJ.
 */
export async function openSpotifySidePlayer(
	url: string,
	options: { shrinkMain?: boolean } = {},
	scope: Scope = globalThis,
	external: (url: string, scope: Scope) => Promise<void> = openExternal
): Promise<void> {
	if (!isSpotifyPlayerTarget(url)) {
		throw new Error(
			`side player only opens ${SPOTIFY_PLAYER_ORIGIN} playlist, album, track or artist pages`
		);
	}
	const sidePlayer = electronBridge(scope)?.sidePlayer;
	if (sidePlayer !== undefined) return sidePlayer.open(url, options);
	// Every other shell (Tauri, or an Electron shell from before the side
	// player) refuses or swallows window.open, so it opens the page in the
	// system browser through its own opener.
	if (nativeShellKind(scope) !== null) return external(url, scope);
	const opener = (
		scope as { open?: (u: string, name: string, features: string) => PlayerWindow | null }
	).open;
	if (typeof opener !== 'function') throw new Error('no window.open in this scope');
	// Open the window blank, still under the click, and cut its opener before
	// pointing it at Spotify: a player page that navigated window.opener could
	// otherwise rewrite the Open DJ tab (reverse
	// tabnabbing). The cost is reuse. Measured in Chromium on Sat 17 Oct 2026: once
	// disowned, the window is no longer found by name, and the Open DJ tab may
	// neither navigate nor close it, so each play opens a fresh window.
	// 'noopener' cannot be used instead: it returns null, so a blocked window
	// could not be told apart from an opened one.
	// Called on its Window: Web IDL rejects window.open on any other receiver.
	const player = opener.call(scope, '', '_blank', 'popup,width=420,height=860');
	// A blocked pop-up returns null; say so instead of pretending it opened.
	if (player == null) {
		throw new Error(
			'the browser blocked the player window; allow pop-ups for Open DJ and try again'
		);
	}
	player.opener = null;
	player.location.href = url;
}

/** The parts of a pop-up's WindowProxy the fallback touches. */
interface PlayerWindow {
	opener: unknown;
	location: { href: string };
}

/** Copy Spotify/store sign-ins from the user's Chrome profile into the side player. */
export async function importBrowserLogins(
	scope: Scope = globalThis
): Promise<SideLoginImportOutcome> {
	const sidePlayer = electronBridge(scope)?.sidePlayer;
	if (sidePlayer === undefined) {
		return { kind: 'refused', reason: 'Only the Open DJ desktop app can import browser sign-ins.' };
	}
	return sidePlayer.importBrowserLogins();
}
