import { test, expect } from '@playwright/test';

/**
 * Mirrors BOOT_LANDING_SESSION_KEY in src/lib/rb/boot-landing.ts. Spelled here
 * rather than imported because this suite runs against the dev server, not the
 * app bundle.
 */
const BOOT_LANDING_SESSION_KEY = 'mdt.boot-landing.applied.v1';

test.describe('CAT-05a library page', () => {
	/**
	 * PERFMODE-11 (67f19c2ee) made a COLD open of `/` land in Gig: the root
	 * layout redirects to /performance once per browser session, while
	 * LIBRARY_MODE_SHIPPED is false. Every Playwright test gets a fresh
	 * context, so every `page.goto('/')` here is a cold open and races that
	 * redirect - two of these four tests happened to win the race on CI and
	 * lose it locally, which is the worst of both answers.
	 *
	 * The redirect is deliberately one-shot and session-gated precisely so
	 * that navigating to the library AFTER first open is untouched. Spending
	 * the session flag up front is exactly that state, so these tests exercise
	 * the library page rather than the landing rule (which
	 * tests/unit/boot-landing.test.mjs owns).
	 */
	test.beforeEach(async ({ page }) => {
		await page.addInitScript((key) => {
			window.sessionStorage.setItem(key, '1');
		}, BOOT_LANDING_SESSION_KEY);
	});

	test('lists tracks and navigates to detail', async ({ page }) => {
		await page.goto('/');
		await expect(page.locator('table.library')).toBeVisible();
		await page.locator('table.library tbody tr').first().click();
		await expect(page).toHaveURL(/\/track\//);
	});

	test('filter narrows the list', async ({ page }) => {
		await page.goto('/');
		await page.fill('input[placeholder="Search title/artist"]', 'midnight');
		await page.waitForTimeout(300);
		const rows = await page.locator('table.library tbody tr').count();
		expect(rows).toBeLessThan(10);
	});

	// PREF-01: right-click a track's BPM cell to set a preferred tempo plus a
	// min/max playable range, persist it, and read it back through the same
	// popover on reopen.
	//
	// This suite runs under the root playwright.config.ts, which starts only
	// Vite and proxies /api to the developer's OWN running backend (not an
	// isolated test database) - a Save here mutates a real library track. So
	// this test snapshots the first row's tempo_pref before touching it and
	// restores it in `finally`, regardless of pass/fail, so repeat runs never
	// leave the developer's library permanently changed.
	test('right-click BPM opens tempo-pref editor and round-trips a range', async ({ page }) => {
		await page.goto('/');
		const row = page.locator('table.library tbody tr').first();
		const bpmCell = row.locator('td.c-bpm');
		await expect(bpmCell).toBeVisible();

		const stableId = await row.getAttribute('data-stable-id');
		if (!stableId) throw new Error('first row is missing data-stable-id');

		const before = await page.request.get(`/api/v1/tracks/${stableId}`);
		expect(before.ok()).toBeTruthy();
		const originalTempoPref = (await before.json()).tempo_pref ?? null;

		try {
			await bpmCell.click({ button: 'right' });
			const popover = page.locator('.tempo-pref-popover');
			await expect(popover).toBeVisible();

			const regularInput = popover.locator('.tp-regular input');
			const minInput = popover.locator('.tp-minmax').nth(0).locator('input');
			const maxInput = popover.locator('.tp-minmax').nth(1).locator('input');

			await regularInput.fill('140');
			await minInput.fill('138');
			await maxInput.fill('142');
			await popover.getByRole('button', { name: 'Save' }).click();
			await expect(popover).not.toBeVisible();

			await bpmCell.click({ button: 'right' });
			await expect(popover).toBeVisible();
			await expect(regularInput).toHaveValue('140');
			await expect(minInput).toHaveValue('138');
			await expect(maxInput).toHaveValue('142');
			await page.keyboard.press('Escape');
			await expect(popover).not.toBeVisible();
		} finally {
			// Re-GET for a fresh etag: the Save above already advanced it, so the
			// etag read before the test would be stale and fail the If-Match CAS.
			const fresh = await page.request.get(`/api/v1/tracks/${stableId}`);
			const freshEtag = fresh.headers()['etag'];
			const restore = await page.request.patch(`/api/v1/tracks/${stableId}`, {
				headers: { 'If-Match': freshEtag },
				data: { tempo_pref: originalTempoPref }
			});
			expect(restore.ok()).toBeTruthy();
		}
	});

	test('tempo-pref editor rejects min >= max', async ({ page }) => {
		await page.goto('/');
		const bpmCell = page.locator('table.library tbody tr').first().locator('td.c-bpm');
		await bpmCell.click({ button: 'right' });
		const popover = page.locator('.tempo-pref-popover');
		await expect(popover).toBeVisible();

		await popover.locator('.tp-minmax').nth(0).locator('input').fill('150');
		await popover.locator('.tp-minmax').nth(1).locator('input').fill('140');
		await expect(popover.getByText('min must be less than max')).toBeVisible();
		await expect(popover.getByRole('button', { name: 'Save' })).toBeDisabled();
		await page.keyboard.press('Escape');
	});
});
