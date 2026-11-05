/**
 * UX-COPY-02, public-launch sweep (Wed 7 Oct 2026): on a library that has
 * never recorded a set, /play-analytics showed the raw
 * `GET /api/play-analytics failed: 503 {"detail":"event store does not exist: <path>"}`.
 *
 * Real engine, real route, real page: the root suite's fixture library has no
 * sets store, so the engine answers the genuine missing-store 503 and the page
 * must turn exactly that into its empty state. Nothing is stubbed.
 *
 * Regression lines:
 * - if the page shows the raw 503 text again then the empty state is gone
 * - if the engine stops answering a missing store with this 503 then the
 *   first assertion fails loudly, rather than the empty state passing over
 *   some other response
 */
import { expect, test } from '@playwright/test';

test('a library with no recorded sets shows the empty state, not the raw 503', async ({ page }) => {
	const analytics = page.waitForResponse((response) =>
		response.url().includes('/api/play-analytics')
	);
	await page.goto('/play-analytics');
	const response = await analytics;
	expect(response.status(), 'the fixture library must have no sets store').toBe(503);
	expect((await response.json()).detail).toMatch(/^event store does not exist: /);

	const empty = page.getByTestId('play-analytics-no-sets');
	await expect(empty).toBeVisible();
	await expect(empty).toContainText('No sets recorded yet');
	await expect(empty.getByRole('link', { name: 'Sessions / REC' })).toHaveAttribute(
		'href',
		'/sets'
	);
	await expect(page.getByText(/failed: 503/)).toHaveCount(0);
	await expect(page.getByText(/event store does not exist/)).toHaveCount(0);
});
