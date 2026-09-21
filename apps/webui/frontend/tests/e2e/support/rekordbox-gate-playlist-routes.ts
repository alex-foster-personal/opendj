import type { Page, Route } from '@playwright/test';

/** Match fast + strict playlist list reads (PERF-UI-05 boot + deferred refresh). */
export async function stubPlaylistsRoute(
	page: Page,
	fulfill: (route: Route) => Promise<void> | void
): Promise<void> {
	// page.route() resolves to a Disposable in this Playwright; callers only
	// await the registration, so the handle is deliberately dropped here.
	await page.route(/\/api\/v1\/playlists(?:\?.*)?$/, fulfill);
}
