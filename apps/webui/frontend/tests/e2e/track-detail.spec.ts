import { test, expect, type Locator, type Page } from '@playwright/test';

import { spendBootLanding } from './support/boot-landing';

async function filledStarCount(page: Page, scope: Locator) {
	await page.mouse.move(0, 0);
	return scope.locator('.rb-stars [role="radio"][aria-checked="true"]').count();
}

test.describe('CAT-05a track detail page', () => {
	// `page.goto('/')` below is a cold open and would race PERFMODE-11's one-shot
	// redirect into Gig; support/boot-landing.ts has the full account.
	test.beforeEach(async ({ page }) => {
		await spendBootLanding(page);
	});

	test('renders provenance + rating control', async ({ page }) => {
		await page.goto('/');
		// LIBWS-01: the Crate Desk inspector links to the full track page.
		await page.locator('[data-testid="track-row"]').first().locator('td.c-title').click();
		await page.getByRole('link', { name: 'Open full track page' }).click();
		await expect(page).toHaveURL(/\/track\//);
		await expect(page.locator('h2')).toBeVisible();
		await expect(page.getByRole('radio', { name: /^Set rating [1-5]$/ })).toHaveCount(5);
	});

	test('list and detail show the same filled star count (issue #2572 AC1)', async ({ page }) => {
		await page.goto('/');
		const row = page.locator('[data-testid="track-row"]').first();
		const listStars = row.locator('td.c-rating');
		const listCount = await filledStarCount(page, listStars);
		await row.locator('td.c-title').click();
		await page.getByRole('link', { name: 'Open full track page' }).click();
		await expect(page).toHaveURL(/\/track\//);
		const detailCount = await filledStarCount(page, page.locator('.rb-stars').first());
		expect(detailCount).toBe(listCount);
		expect(listCount).toBeGreaterThanOrEqual(0);
		expect(listCount).toBeLessThanOrEqual(5);
	});

	test('detail rating PATCH matches list after refresh (issue #2572 AC3)', async ({ page }) => {
		test.fixme(true, 'three Set rating radios, strict-mode violation, see #13526');
		await page.goto('/');
		const row = page.locator('[data-testid="track-row"]').first();
		const listStars = row.locator('td.c-rating');
		const initial = await filledStarCount(page, listStars);
		await row.locator('td.c-title').click();
		await page.getByRole('link', { name: 'Open full track page' }).click();
		const url = page.url();
		const target = initial === 0 ? 3 : initial === 5 ? 2 : initial + 1;
		await page.getByRole('radio', { name: `Set rating ${target}` }).click();
		await expect(page.getByText('Saved')).toBeVisible({ timeout: 30_000 });
		await page.getByRole('link', { name: 'back to library', exact: false }).click();
		await expect(row).toBeVisible({ timeout: 30_000 });
		const listAfter = await filledStarCount(page, listStars);
		expect(listAfter).toBe(target);
		await page.goto(url);
		const detailAfter = await filledStarCount(page, page.locator('.rb-stars').first());
		expect(detailAfter).toBe(target);
	});
});
