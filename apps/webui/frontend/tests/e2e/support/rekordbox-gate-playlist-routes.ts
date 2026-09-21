import type { Page, Route } from '@playwright/test';

/** Match fast + strict playlist list reads (PERF-UI-05 boot + deferred refresh). */
export function stubPlaylistsRoute(
	page: Page,
	fulfill: (route: Route) => Promise<void> | void
): Promise<void> {
	return page.route(/\/api\/v1\/playlists(?:\?.*)?$/, fulfill);
}
