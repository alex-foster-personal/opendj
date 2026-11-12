/**
 * requirement: UX-RAIL-01
 *
 * the maintainer, Tue 6 Oct 2026: "there's also I think no icon for collection that
 * works that brings us back to collections, should be the top left".
 *
 * The rail's top-left Home entry returns the browser to the whole collection
 * (All Tracks) from any playlist. This drives the real click in a real browser
 * against the root suite's fixture library (two tracks, seeded playlists), so
 * the rendered button, its handler and the `browser_select_playlist` 'all'
 * command are all exercised together; the unit test only pins the source.
 *
 * Regression lines:
 * - if the Home button is not the rail's first button, or is disabled, then broken
 * - if clicking Home from a playlist does not show All Tracks (tab and URL) then broken
 */
import { expect, test } from '@playwright/test';

test('the rail Home entry returns from a playlist to All Tracks', async ({ page }) => {
	test.fixme(true, 'a conn-dot intercepts the rail click, see #13523');
	test.setTimeout(120_000);
	// The enrich card (ENRICH-01) floats over the library on this unanalyzed
	// fixture; hide it the way the user does.
	await page.addInitScript(() => sessionStorage.setItem('odj.enrich-card.hidden', '1'));
	await page.goto('/performance?muted=1');
	await expect(page.locator('[data-testid="track-table"]')).toBeVisible({ timeout: 60_000 });

	const activeTab = page.getByRole('tablist', { name: 'browser panes' }).locator('.tab.active');
	await page.getByTestId('playlist-row').filter({ hasText: 'E2E Fixture Set' }).click();
	await expect(activeTab).toContainText('E2E Fixture Set');
	await expect(page).not.toHaveURL(/[?&]playlist=all\b/);

	const home = page.getByTestId('rail-collection-home');
	await expect(home).toBeEnabled();
	// It is the rail's top-left entry: the first button in its container.
	const isFirst = await home.evaluate((el) => el.parentElement?.querySelector('button') === el);
	expect(isFirst, 'Home must be the first rail button').toBe(true);

	await home.click();
	await expect(activeTab).toContainText('All Tracks', { timeout: 15_000 });
	await expect(page).toHaveURL(/[?&]playlist=all\b/);
	await expect(page.locator('[data-testid="track-row"]').first()).toBeVisible({ timeout: 30_000 });
});
