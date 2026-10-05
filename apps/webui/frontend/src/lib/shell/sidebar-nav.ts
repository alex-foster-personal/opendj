/**
 * The app shell's sidebar links and header-count tooltips (routes/+layout.svelte).
 *
 * V1 polish (JIK, Thu 1 Oct 2026):
 *   - Admin, the progress ledger (/progress-tree) and Queues are developer
 *     pages: listed only while the "Show developer pages" pref (show_dev_ui,
 *     lib/rb/dev-ui-prefs.ts) is on. The routes themselves still work by URL.
 *   - The two Settings entries ("Settings (daemon)" and "Settings (Cmd+,)")
 *     are one link to /settings, which hosts the Cmd+, overlay opener.
 *   - The link for the page on screen is marked current (aria-current="page").
 *
 * Pure (no prefs or $page access) so the list and the current-page rule are
 * unit testable.
 */

export interface SidebarNavLink {
	href: string;
	label: string;
	/** Developer-only: listed only while show_dev_ui is on. */
	dev?: boolean;
	/** Hover title; omitted where the label says it all. */
	title?: string;
}

export const SIDEBAR_NAV_LINKS: readonly SidebarNavLink[] = [
	{ href: '/', label: 'Library' },
	{ href: '/pairings', label: 'Pairings' },
	{ href: '/smartlists', label: 'Smartlists' },
	{ href: '/queues', label: 'Queues', dev: true },
	{ href: '/reconcile', label: 'Missing tracks' },
	{ href: '/dedup', label: 'Dedup Review' },
	{ href: '/performance', label: 'Performance' },
	{ href: '/play-analytics', label: 'Play analytics' },
	{ href: '/library-wheel', label: 'Library wheel' },
	{ href: '/sets', label: 'Sessions / REC' },
	{ href: '/cloudsync', label: 'CloudSync' },
	{ href: '/progress-tree', label: 'Progress', dev: true },
	{ href: '/admin', label: 'Admin', dev: true },
	{
		href: '/settings',
		label: 'Settings',
		title: 'Settings - Cmd+, (Ctrl+, on Windows/Linux) opens the quick settings overlay from anywhere'
	}
];

/** The links to render: developer pages only while showDev is on. */
export function sidebarNavLinks(showDev: boolean): SidebarNavLink[] {
	return SIDEBAR_NAV_LINKS.filter((l) => showDev || l.dev !== true);
}

/** Whether `href` is the page on screen. '/' matches only itself; any other
 * link also matches its sub-routes (/settings matches /settings/x) but never
 * a sibling that merely shares a prefix (/sets does not match /settings). */
export function isCurrentNavLink(href: string, pathname: string): boolean {
	if (href === '/') return pathname === '/';
	return pathname === href || pathname.startsWith(`${href}/`);
}

/** Header status-strip count tooltips: say what the number counts, not the
 * number again. Denominators per `GET /api/v1/health` state_db
 * (sqlite_backend.stats): every track row, and non-deleted playlists. */
export function headerTrackCountTitle(n: number, playable = 0): string {
	return `${n} tracks: every track row in the library database (state.db), including tracks whose audio file is missing. ${playable} playable: track_availability present or streaming on this machine.`;
}

export function headerPlaylistCountTitle(n: number): string {
	return `${n} playlists: playlists in the library database (state.db), not counting deleted ones`;
}
