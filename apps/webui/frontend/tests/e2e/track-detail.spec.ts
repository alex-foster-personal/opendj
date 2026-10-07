import { test, expect } from '@playwright/test';

import { spendBootLanding } from './support/boot-landing';

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
});
