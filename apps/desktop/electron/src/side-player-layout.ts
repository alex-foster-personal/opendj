// Spotify side player geometry (SPOT-6555b). Pure, so node:test covers it.
//
// The player docks to the right edge of the Open DJ window, same top and
// height. Expanded, it is PLAYER_W wide and Open DJ shrinks to make room when
// the screen has none (unless the user turned that off). Collapsed, it is a
// STRIP_W column and Open DJ grows back to the width it had before it shrank.

export interface Rect {
	x: number;
	y: number;
	width: number;
	height: number;
}

export const PLAYER_W = 420;
/** the maintainer, Tue 13 Oct 2026: "Collapses into side col (10px wide)". */
export const STRIP_W = 10;
/** Open DJ never shrinks below this to make room; the player overlaps instead. */
export const MAIN_MIN_W = 900;

export interface SidePlayerLayoutInput {
	main: Rect;
	/** The work area of the display the main window is on. */
	workArea: Rect;
	collapsed: boolean;
	/** Settings: "Shrink Open DJ to make room for the side player" (default on). */
	shrinkMain: boolean;
	/** Open DJ's width before the player shrank it; null when it never did. */
	restoreWidth: number | null;
	playerWidth?: number;
}

export interface SidePlayerLayout {
	main: Rect;
	player: Rect;
	/** What to pass back in as restoreWidth next time. */
	restoreWidth: number | null;
}

const right = (r: Rect): number => r.x + r.width;

export function sidePlayerLayout(input: SidePlayerLayoutInput): SidePlayerLayout {
	const { workArea, collapsed, shrinkMain } = input;
	const playerW = collapsed ? STRIP_W : Math.max(STRIP_W, Math.round(input.playerWidth ?? PLAYER_W));
	let main: Rect = { ...input.main };
	let restoreWidth = input.restoreWidth;

	// Collapsing (or turning the shrink off) gives back the width taken earlier,
	// never more than fits beside the strip. Collapsing keeps the original
	// width as the target, so collapse/expand cycles never ratchet Open DJ
	// narrower and closing still restores it in full; only turning the shrink
	// off forgets it.
	if (restoreWidth !== null && (collapsed || !shrinkMain)) {
		const room = right(workArea) - (collapsed ? STRIP_W : 0) - main.x;
		main = { ...main, width: Math.max(main.width, Math.min(restoreWidth, room)) };
		if (!shrinkMain) restoreWidth = null;
	}

	const overflow = right(main) + playerW - right(workArea);
	if (shrinkMain && overflow > 0) {
		const shrunk = Math.max(MAIN_MIN_W, main.width - overflow);
		if (shrunk < main.width) {
			restoreWidth = restoreWidth ?? main.width;
			main = { ...main, width: shrunk };
		}
	}

	// Dock at Open DJ's right edge; if the screen still has no room (shrink off,
	// or Open DJ at its minimum), keep the player on screen over Open DJ's edge.
	const playerX = Math.min(right(main), right(workArea) - playerW);
	return {
		main,
		player: { x: playerX, y: main.y, width: playerW, height: main.height },
		restoreWidth
	};
}

/**
 * Open DJ's width once the player closes: the width it lent back, but never
 * past the right edge of the display it is on now, which may be narrower than
 * the one it shrank on.
 */
export function closedMainWidth(main: Rect, workArea: Rect, restoreWidth: number): number {
	return Math.max(main.width, Math.min(restoreWidth, right(workArea) - main.x));
}

/** Only open.spotify.com (and its sign-in pages) load in the player. */
const PLAYER_HOSTS = [
	'open.spotify.com',
	'accounts.spotify.com',
	'www.spotify.com',
	'challenge.spotify.com',
	// "Continue with Apple / Facebook" stay in the player. Google refuses
	// embedded sign-in, which is what the Chrome login import is for.
	'appleid.apple.com',
	'www.facebook.com',
	'm.facebook.com'
];

export function isSidePlayerUrl(url: string): boolean {
	let parsed: URL;
	try {
		parsed = new URL(url);
	} catch {
		return false;
	}
	return parsed.protocol === 'https:' && PLAYER_HOSTS.includes(parsed.hostname);
}

const OPEN_TARGET_PATH = /^\/(playlist|album|track|artist)\/[A-Za-z0-9]+\/?$/;

/**
 * Only playlist/album/track/artist pages, each with a nonempty id, may be
 * opened from Open DJ itself; every other open.spotify.com path is refused.
 */
export function isSidePlayerOpenTarget(url: string): boolean {
	if (!isSidePlayerUrl(url)) return false;
	const parsed = new URL(url);
	return parsed.hostname === 'open.spotify.com' && OPEN_TARGET_PATH.test(parsed.pathname);
}

/**
 * Electron's default user agent minus its own tokens ("Electron/x", the app
 * name), so Spotify sees plain Chrome. Spotify's sign-in shows "unsupported
 * browser" for the default one.
 */
export function plainChromeUserAgent(electronUa: string): string {
	return electronUa
		.split(' ')
		.filter((token) => !/^Electron\//.test(token) && !/^(opendj-desktop|Open|DJ|open-dj)[\w.-]*\//i.test(token))
		.join(' ');
}

/**
 * The side-open IPC arguments, checked exactly. A wrong type is bridge drift
 * and fails loudly rather than falling back to a default.
 */
export function sideOpenArgs(url: unknown, options: unknown): { url: string; shrinkMain: boolean | undefined } {
	if (typeof url !== 'string') throw new Error('side player needs a URL string');
	if (options === undefined) return { url, shrinkMain: undefined };
	if (typeof options !== 'object' || options === null) throw new Error('side player options must be an object');
	const shrinkMain = (options as { shrinkMain?: unknown }).shrinkMain;
	if (shrinkMain !== undefined && typeof shrinkMain !== 'boolean') throw new Error('shrinkMain must be a boolean');
	return { url, shrinkMain };
}

/** The side-collapse IPC argument; anything but a boolean is refused. */
export function sideCollapsedArg(collapsed: unknown): boolean {
	if (typeof collapsed !== 'boolean') throw new Error('collapsed must be a boolean');
	return collapsed;
}

/**
 * Why the side player cannot dock on this display server, or null when it can.
 * Docking moves and resizes windows with setBounds, which Wayland does not let
 * an app do (Electron docs, BrowserWindow.setBounds), so a native Wayland
 * session refuses openly instead of leaving the player at its default
 * geometry. Under X11 or Xwayland it docks.
 *
 * The backend is decided the way Electron decides it, so only a session that
 * runs natively on Wayland is refused: an explicit
 * --ozone-platform wins outright, whatever the environment says, except
 * --ozone-platform=auto, which leaves the choice to the session. Otherwise
 * --ozone-platform-hint x11 means X11 (Xwayland), and Electron's own default
 * since 38, the "auto" hint, picks Wayland only when the session advertises
 * it through WAYLAND_DISPLAY or XDG_SESSION_TYPE.
 */
export function sideDockUnavailableReason(
	platform: NodeJS.Platform,
	env: Record<string, string | undefined>,
	ozonePlatform: string,
	ozoneHint: string
): string | null {
	if (platform !== 'linux') return null;
	let wayland: boolean;
	if (ozonePlatform !== '' && ozonePlatform !== 'auto') wayland = ozonePlatform === 'wayland';
	else if (ozoneHint === 'x11') wayland = false;
	else wayland = Boolean(env.WAYLAND_DISPLAY) || env.XDG_SESSION_TYPE === 'wayland';
	if (!wayland) return null;
	return 'The side player docks by moving windows, which Wayland does not allow. Start Open DJ with --ozone-platform=x11 to use it.';
}
