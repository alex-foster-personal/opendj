/**
 * The /performance review-todo panel (FeedbackPanel.svelte) is loaded on its
 * first open, not with the route (PR #4094 moved it behind a dynamic import to
 * keep the route under its bundle budget). Proven against the real backend and
 * the real widget: nothing in the app is stubbed, and the failure case aborts
 * the chunk request at the network layer, which is where a stale deploy or a
 * dropped connection fails it.
 *
 * Regression lines:
 * - if the panel module is fetched before the first open then the deferral
 *   is gone and the route pays for the panel again
 * - if the first click stops opening the panel then deferring it broke it
 * - if a close then reopen loses the panel then it is being unmounted, and
 *   its position and drafts no longer persist across a close
 * - if a failed chunk leaves no trace then the chevron lights over nothing
 */
import { expect, test, type Page } from '@playwright/test';

const PANEL_MODULE = /FeedbackPanel/;

async function openPerformanceWithFeedback(page: Page) {
	await page.goto('/performance');
	const chevron = page.getByRole('button', { name: 'Review todos panel' });
	// The chevron is enabled while availability is still 'unknown' (a click
	// then only re-probes), so wait for the real probe's settled title.
	await expect(chevron).toHaveAttribute('title', /^Review todos - \d+ open/, { timeout: 75_000 });
	return chevron;
}

test('the review panel loads on its first open, then closes and reopens', async ({ page }) => {
	const requested: string[] = [];
	page.on('request', (request) => {
		if (PANEL_MODULE.test(request.url())) requested.push(request.url());
	});
	const chevron = await openPerformanceWithFeedback(page);
	const panel = page.getByRole('dialog', { name: 'Review todos' });
	expect(requested, 'the panel module must not load with the route').toEqual([]);
	await expect(panel).toHaveCount(0);

	await chevron.click();
	await expect(panel).toBeVisible({ timeout: 10_000 });
	expect(requested.length, 'the first open fetches the panel module').toBeGreaterThan(0);

	await page.getByRole('button', { name: 'Close review panel' }).click();
	await expect(panel).toHaveCount(0);
	await chevron.click();
	await expect(panel).toBeVisible();
});

test('a panel chunk that fails to load closes the panel and says so on the chevron', async ({
	page
}) => {
	await page.route(PANEL_MODULE, (route) => route.abort('failed'));
	const chevron = await openPerformanceWithFeedback(page);
	await chevron.click();
	await expect(chevron).toHaveAttribute('title', /the panel failed to load .*reload the page to retry/, {
		timeout: 10_000
	});
	await expect(chevron).toHaveAttribute('aria-expanded', 'false');
	await expect(page.getByRole('dialog', { name: 'Review todos' })).toHaveCount(0);
});
